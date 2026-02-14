"""Tests for the candidate strategy system (Sessions T + U).

Tests CandidateRunner paper simulation, CandidateManager lifecycle,
and candidate observability (notifications, heartbeat, stats persistence).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shell.config import load_config
from src.shell.contract import Action, Intent, RiskLimits, Signal, SymbolData
from src.shell.database import Database

# Shared risk limits for tests
RISK_LIMITS = RiskLimits(
    max_trade_pct=0.10,
    default_trade_pct=0.05,
    max_positions=5,
    max_daily_loss_pct=0.05,
    max_drawdown_pct=0.20,
    max_position_pct=0.25,
    max_daily_trades=20,
    rollback_consecutive_losses=15,
)

VALID_STRATEGY_CODE = '''
from src.shell.contract import StrategyBase, Signal, RiskLimits, Action, Intent

class Strategy(StrategyBase):
    """Test strategy."""
    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None:
        pass
    def analyze(self, markets, portfolio, timestamp):
        return []
'''

SIGNAL_STRATEGY_CODE = '''
from src.shell.contract import StrategyBase, Signal, RiskLimits, Action, Intent

class Strategy(StrategyBase):
    """Strategy that generates a BUY signal for BTC/USD."""
    def initialize(self, risk_limits: RiskLimits, symbols: list[str]) -> None:
        pass
    def analyze(self, markets, portfolio, timestamp):
        return [Signal(
            symbol="BTC/USD",
            action=Action.BUY,
            size_pct=0.05,
            intent=Intent.SWING,
            confidence=0.8,
            reasoning="test buy",
        )]
'''


# --- CandidateRunner Tests ---


def test_runner_paper_fills():
    """BUY signal creates position, SELL signal closes with P&L."""
    from src.candidates.runner import CandidateRunner
    import pandas as pd

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0005, maker_fee_pct=0.25, taker_fee_pct=0.40,
    )

    # BUY signal
    buy_signal = Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.SWING, confidence=0.8, reasoning="test buy",
    )
    result = runner._execute_signal(buy_signal, 50000.0)
    assert result is not None
    assert result["action"] == "BUY"
    assert result["qty"] > 0
    assert len(runner._positions) == 1

    # Cash decreased
    assert runner._cash < 1000.0

    # SELL signal (close oldest for symbol)
    tag = list(runner._positions.keys())[0]
    sell_signal = Signal(
        symbol="BTC/USD", action=Action.SELL, size_pct=1.0,
        intent=Intent.SWING, confidence=0.8, reasoning="test sell",
    )
    result = runner._execute_signal(sell_signal, 51000.0)
    assert result is not None
    assert result["action"] == "SELL"
    assert result["pnl"] is not None
    assert len(runner._positions) == 0
    assert len(runner._trades) == 1


def test_runner_sl_tp():
    """Stop loss triggers at correct price."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0005, maker_fee_pct=0.25, taker_fee_pct=0.40,
    )

    # Create a position with SL
    buy_signal = Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.SWING, confidence=0.8, reasoning="test buy",
        stop_loss=49000.0, take_profit=55000.0,
    )
    runner._execute_signal(buy_signal, 50000.0)
    assert len(runner._positions) == 1

    # Price above SL — no trigger
    results = runner.check_sl_tp({"BTC/USD": 49500.0})
    assert len(results) == 0
    assert len(runner._positions) == 1

    # Price at SL — triggers
    results = runner.check_sl_tp({"BTC/USD": 48900.0})
    assert len(results) == 1
    assert results[0]["close_reason"] == "stop_loss"
    assert len(runner._positions) == 0


def test_runner_risk_limits():
    """Oversized signals clamped to risk limits."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0005, maker_fee_pct=0.25, taker_fee_pct=0.40,
    )

    # Signal with 50% size — should be clamped to max_trade_pct (10%)
    big_signal = Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.50,
        intent=Intent.DAY, confidence=0.9, reasoning="big buy",
    )
    result = runner._execute_signal(big_signal, 50000.0)
    assert result is not None
    # Cost should be ~10% of 1000 = ~$100 (not 50% = $500)
    cost = 1000.0 - runner._cash
    assert cost < 150.0  # Allow slippage/fee margin


def test_runner_portfolio_snapshot():
    """Portfolio built correctly from runner state."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0005, maker_fee_pct=0.25, taker_fee_pct=0.40,
    )

    # Buy something
    buy_signal = Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.SWING, confidence=0.8, reasoning="test",
    )
    runner._execute_signal(buy_signal, 50000.0)

    portfolio = runner._build_portfolio({"BTC/USD": 51000.0})
    assert portfolio.cash == runner._cash
    assert portfolio.total_value > 0
    assert len(portfolio.positions) == 1
    assert portfolio.positions[0].symbol == "BTC/USD"


def test_runner_modify_signal():
    """MODIFY signal updates SL/TP without closing position."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0005, maker_fee_pct=0.25, taker_fee_pct=0.40,
    )

    # Create position
    buy_signal = Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.DAY, confidence=0.8, reasoning="test",
        stop_loss=48000.0,
    )
    result = runner._execute_signal(buy_signal, 50000.0)
    tag = result["tag"]

    # Modify SL/TP
    modify_signal = Signal(
        symbol="BTC/USD", action=Action.MODIFY, size_pct=0.0,
        intent=Intent.SWING, confidence=1.0, reasoning="tighten SL",
        tag=tag, stop_loss=49000.0, take_profit=55000.0,
    )
    result = runner._execute_signal(modify_signal, 50000.0)
    assert result is not None
    assert result["action"] == "MODIFY"

    pos = runner._positions[tag]
    assert pos["stop_loss"] == 49000.0
    assert pos["take_profit"] == 55000.0
    assert pos["intent"] == "SWING"


def test_runner_get_status():
    """Status dict includes correct win/loss/PnL counts."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0, maker_fee_pct=0.0, taker_fee_pct=0.0,
    )

    # Win trade
    runner._execute_signal(Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.DAY, confidence=0.8, reasoning="buy",
    ), 50000.0)
    runner._execute_signal(Signal(
        symbol="BTC/USD", action=Action.SELL, size_pct=1.0,
        intent=Intent.DAY, confidence=0.8, reasoning="sell",
    ), 51000.0)

    status = runner.get_status()
    assert status["slot"] == 1
    assert status["version"] == "v_test"
    assert status["trade_count"] == 1
    assert status["wins"] == 1
    assert status["losses"] == 0
    assert status["pnl"] > 0


# --- CandidateManager Tests ---


@pytest.mark.asyncio
async def test_manager_create():
    """Candidate created in DB, runner active."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        runner = await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_test",
            description="Test candidate", evaluation_duration_days=7,
        )

        assert runner is not None
        assert 1 in mgr.get_active_slots()

        # DB has the row
        row = await db.fetchone("SELECT * FROM candidates WHERE slot = 1")
        assert row is not None
        assert row["status"] == "running"
        assert row["strategy_version"] == "v_test"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_cancel():
    """Candidate canceled, DB updated, runner removed."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_test",
        )
        assert 1 in mgr.get_active_slots()

        await mgr.cancel_candidate(1, "test cancel")
        assert 1 not in mgr.get_active_slots()

        row = await db.fetchone("SELECT * FROM candidates WHERE slot = 1")
        assert row["status"] == "canceled"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_promote():
    """Promote returns code, cancels all candidates."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v1",
        )
        await mgr.create_candidate(
            slot=2, code=VALID_STRATEGY_CODE, version="v2",
        )
        assert len(mgr.get_active_slots()) == 2

        code = await mgr.promote_candidate(1)
        assert "class Strategy" in code
        assert len(mgr.get_active_slots()) == 0

        # Promoted candidate marked as promoted
        row = await db.fetchone("SELECT * FROM candidates WHERE slot = 1")
        assert row["status"] == "promoted"

        # Other candidate canceled
        row2 = await db.fetchone("SELECT * FROM candidates WHERE slot = 2")
        assert row2["status"] == "canceled"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_recover():
    """Running candidates recovered from DB on init."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        # Create a candidate, then simulate restart by creating new manager
        mgr1 = CandidateManager(config, db)
        await mgr1.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_recover",
        )
        assert 1 in mgr1.get_active_slots()

        # New manager — should recover from DB
        mgr2 = CandidateManager(config, db)
        await mgr2.initialize()
        assert 1 in mgr2.get_active_slots()

        runner = mgr2.get_runner(1)
        assert runner is not None
        assert runner.version == "v_recover"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_replace_slot():
    """Cancels existing, creates new in same slot."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_old",
        )

        # Replace with new
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_new",
        )

        runner = mgr.get_runner(1)
        assert runner.version == "v_new"

        # INSERT OR REPLACE overwrites the slot row (UNIQUE constraint), so only new one exists
        row = await db.fetchone("SELECT * FROM candidates WHERE slot = 1")
        assert row["status"] == "running"
        assert row["strategy_version"] == "v_new"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_persist_state():
    """Positions and trades written to DB after persist."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        runner = await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_test",
        )

        # Manually create a position in the runner
        runner._positions["test_tag"] = {
            "symbol": "BTC/USD",
            "tag": "test_tag",
            "side": "long",
            "qty": 0.001,
            "avg_entry": 50000.0,
            "current_price": 51000.0,
            "unrealized_pnl": 1.0,
            "entry_fee": 0.2,
            "stop_loss": 48000.0,
            "take_profit": 55000.0,
            "intent": "SWING",
            "strategy_version": "v_test",
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        await mgr.persist_state()

        # Check DB
        positions = await db.fetchall(
            "SELECT * FROM candidate_positions WHERE candidate_slot = 1"
        )
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USD"
        assert positions[0]["tag"] == "test_tag"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_context_for_orchestrator():
    """Context includes running + empty slot info."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_ctx",
            description="Test context",
        )

        context = await mgr.get_context_for_orchestrator()
        assert len(context) == config.orchestrator.max_candidates

        # Slot 1 is running
        slot1 = [c for c in context if c.get("slot") == 1][0]
        assert slot1["status"] == "running"
        assert slot1["version"] == "v_ctx"

        # Other slots are empty
        for c in context:
            if c.get("slot") != 1:
                assert c["status"] == "empty"

        await db.close()
    finally:
        os.unlink(config.db_path)


# --- Session U: Stats Bug + Observability ---


def test_runner_stats_survive_persist():
    """get_status() stays accurate after get_new_trades() clears persist buffer."""
    from src.candidates.runner import CandidateRunner

    runner = CandidateRunner(
        slot=1, strategy=MagicMock(), version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        slippage_factor=0.0, maker_fee_pct=0.0, taker_fee_pct=0.0,
    )

    # Win trade
    runner._execute_signal(Signal(
        symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
        intent=Intent.DAY, confidence=0.8, reasoning="buy",
    ), 50000.0)
    runner._execute_signal(Signal(
        symbol="BTC/USD", action=Action.SELL, size_pct=1.0,
        intent=Intent.DAY, confidence=0.8, reasoning="sell",
    ), 51000.0)

    status_before = runner.get_status()
    assert status_before["trade_count"] == 1
    assert status_before["pnl"] > 0

    # Simulate persist — clears _trades
    new_trades = runner.get_new_trades()
    assert len(new_trades) == 1
    assert len(runner._trades) == 0  # persist buffer cleared

    # Stats should still be intact
    status_after = runner.get_status()
    assert status_after["trade_count"] == 1
    assert status_after["pnl"] == status_before["pnl"]
    assert status_after["wins"] == 1

    # Portfolio total_pnl should also survive
    portfolio = runner._build_portfolio({"BTC/USD": 50000.0})
    assert portfolio.total_pnl == status_before["pnl"]


@pytest.mark.asyncio
async def test_manager_notifies_on_trade():
    """CandidateManager dispatches candidate_trade_executed on trades."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        runner = await mgr.create_candidate(
            slot=1, code=SIGNAL_STRATEGY_CODE, version="v_sig",
        )

        # Wire mock notifier
        mock_notifier = AsyncMock()
        mgr.set_notifier(mock_notifier)

        # Build minimal markets
        import pandas as pd
        import numpy as np
        dates = pd.date_range("2024-01-01", periods=100, freq="5min")
        prices = 50000 + np.cumsum(np.random.randn(100) * 10)
        df = pd.DataFrame({
            "open": prices, "high": prices + 20, "low": prices - 20,
            "close": prices, "volume": np.random.uniform(10, 100, 100),
        }, index=dates)

        markets = {
            "BTC/USD": SymbolData(
                symbol="BTC/USD", current_price=50000.0,
                candles_5m=df, candles_1h=df, candles_1d=df,
                spread=10.0, volume_24h=1000.0,
                maker_fee_pct=0.25, taker_fee_pct=0.40,
            ),
        }

        await mgr.run_scans(markets, datetime.now(timezone.utc))

        # Should have dispatched candidate_trade_executed
        mock_notifier.candidate_trade_executed.assert_called()
        call_args = mock_notifier.candidate_trade_executed.call_args
        assert call_args[0][0] == 1  # slot
        assert call_args[0][1]["action"] == "BUY"
        assert call_args[0][1]["symbol"] == "BTC/USD"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_notifies_on_sl_tp():
    """CandidateManager dispatches both stop_triggered and trade_executed on SL/TP."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        runner = await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_sl",
        )

        # Manually add a position with stop loss
        runner._positions["c1_test_001"] = {
            "symbol": "BTC/USD",
            "tag": "c1_test_001",
            "side": "long",
            "qty": 0.001,
            "avg_entry": 50000.0,
            "current_price": 50000.0,
            "unrealized_pnl": 0.0,
            "entry_fee": 0.2,
            "stop_loss": 49000.0,
            "take_profit": 55000.0,
            "intent": "DAY",
            "strategy_version": "v_sl",
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        mock_notifier = AsyncMock()
        mgr.set_notifier(mock_notifier)

        # Trigger stop loss
        await mgr.check_sl_tp({"BTC/USD": 48500.0})

        mock_notifier.candidate_stop_triggered.assert_called_once()
        mock_notifier.candidate_trade_executed.assert_called_once()

        stop_args = mock_notifier.candidate_stop_triggered.call_args
        assert stop_args[0][0] == 1  # slot
        assert stop_args[0][1]["close_reason"] == "stop_loss"

        await db.close()
    finally:
        os.unlink(config.db_path)


@pytest.mark.asyncio
async def test_manager_heartbeat_logging():
    """CandidateManager emits candidate.heartbeat structlog every 10 scans."""
    from src.candidates.manager import CandidateManager

    config = load_config()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        config.db_path = f.name

    try:
        db = Database(config.db_path)
        await db.connect()

        mgr = CandidateManager(config, db)
        await mgr.create_candidate(
            slot=1, code=VALID_STRATEGY_CODE, version="v_hb",
        )

        # Build minimal markets
        import pandas as pd
        import numpy as np
        dates = pd.date_range("2024-01-01", periods=50, freq="5min")
        prices = 50000 + np.cumsum(np.random.randn(50) * 10)
        df = pd.DataFrame({
            "open": prices, "high": prices + 20, "low": prices - 20,
            "close": prices, "volume": np.random.uniform(10, 100, 50),
        }, index=dates)

        markets = {
            "BTC/USD": SymbolData(
                symbol="BTC/USD", current_price=50000.0,
                candles_5m=df, candles_1h=df, candles_1d=df,
                spread=10.0, volume_24h=1000.0,
                maker_fee_pct=0.25, taker_fee_pct=0.40,
            ),
        }

        heartbeat_logged = False
        with patch("src.candidates.manager.log") as mock_log:
            for _ in range(10):
                await mgr.run_scans(markets, datetime.now(timezone.utc))

            # Check structlog calls for heartbeat
            for call in mock_log.info.call_args_list:
                if call[0][0] == "candidate.heartbeat":
                    heartbeat_logged = True
                    assert call[1]["slot"] == 1
                    assert call[1]["scans"] == 10
                    break

        assert heartbeat_logged, "Expected candidate.heartbeat log after 10 scans"

        await db.close()
    finally:
        os.unlink(config.db_path)
