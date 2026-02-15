"""Tests for the Institutional Learning System (Session W).

Tests: database schema (new tables + migrations), MAE tracking (fund + candidate),
candidate signal capture + daily snapshots, prediction storage, reflection system,
pruning, REST endpoints.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
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


# --- Helper: create a temporary DB ---
async def _make_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    db = Database(db_path)
    await db.connect()
    return db, db_path


# --- Phase 1: Database Schema Tests ---

@pytest.mark.asyncio
async def test_new_tables_exist():
    """All 4 new tables from Session W exist."""
    db, db_path = await _make_db()
    try:
        rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [r["name"] for r in rows]

        for t in ("predictions", "strategy_doc_versions", "candidate_signals", "candidate_daily_performance"):
            assert t in tables, f"Missing table: {t}"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_new_migration_columns():
    """All 7 new migration columns exist."""
    db, db_path = await _make_db()
    try:
        # orchestrator_observations: strategy_version, doc_flag, flag_reason
        cursor = await db.execute("PRAGMA table_info(orchestrator_observations)")
        obs_cols = [row[1] for row in await cursor.fetchall()]
        assert "strategy_version" in obs_cols
        assert "doc_flag" in obs_cols
        assert "flag_reason" in obs_cols

        # positions: max_adverse_excursion
        cursor = await db.execute("PRAGMA table_info(positions)")
        pos_cols = [row[1] for row in await cursor.fetchall()]
        assert "max_adverse_excursion" in pos_cols

        # candidate_positions: max_adverse_excursion
        cursor = await db.execute("PRAGMA table_info(candidate_positions)")
        cand_pos_cols = [row[1] for row in await cursor.fetchall()]
        assert "max_adverse_excursion" in cand_pos_cols

        # trades: max_adverse_excursion
        cursor = await db.execute("PRAGMA table_info(trades)")
        trade_cols = [row[1] for row in await cursor.fetchall()]
        assert "max_adverse_excursion" in trade_cols

        # candidate_trades: max_adverse_excursion
        cursor = await db.execute("PRAGMA table_info(candidate_trades)")
        cand_trade_cols = [row[1] for row in await cursor.fetchall()]
        assert "max_adverse_excursion" in cand_trade_cols

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_predictions_table_operations():
    """Predictions table CRUD operations work correctly."""
    db, db_path = await _make_db()
    try:
        # Insert
        await db.execute(
            """INSERT INTO predictions
               (cycle_id, claim, evidence, falsification, confidence,
                evaluation_timeframe, category)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("cycle_001", "BTC will hold above 50k", "strong support level",
             "BTC drops below 48k", "medium", "7 days", "market"),
        )
        await db.commit()

        # Read
        row = await db.fetchone("SELECT * FROM predictions WHERE cycle_id = 'cycle_001'")
        assert row is not None
        assert row["claim"] == "BTC will hold above 50k"
        assert row["confidence"] == "medium"
        assert row["graded_at"] is None
        assert row["grade"] is None

        # Grade
        await db.execute(
            """UPDATE predictions SET graded_at = datetime('now', 'utc'),
               grade = 'confirmed', grade_evidence = 'held at 52k',
               grade_learning = 'support levels work' WHERE id = ?""",
            (row["id"],),
        )
        await db.commit()

        graded = await db.fetchone("SELECT * FROM predictions WHERE id = ?", (row["id"],))
        assert graded["grade"] == "confirmed"
        assert graded["graded_at"] is not None

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_strategy_doc_versions_table():
    """Strategy doc versions table stores and retrieves versions."""
    db, db_path = await _make_db()
    try:
        await db.execute(
            "INSERT INTO strategy_doc_versions (version, content, reflection_cycle_id) VALUES (?, ?, ?)",
            (1, "# Doc v1\nContent here", "cycle_001"),
        )
        await db.execute(
            "INSERT INTO strategy_doc_versions (version, content, reflection_cycle_id) VALUES (?, ?, ?)",
            (2, "# Doc v2\nUpdated content", "cycle_014"),
        )
        await db.commit()

        rows = await db.fetchall("SELECT * FROM strategy_doc_versions ORDER BY version")
        assert len(rows) == 2
        assert rows[0]["version"] == 1
        assert rows[1]["version"] == 2

        # MAX version helper
        max_row = await db.fetchone("SELECT COALESCE(MAX(version), 0) as max_ver FROM strategy_doc_versions")
        assert max_row["max_ver"] == 2

        await db.close()
    finally:
        os.unlink(db_path)


# --- Phase 2: MAE Tracking Tests ---

@pytest.mark.asyncio
async def test_mae_tracking_fund_positions():
    """MAE starts at 0, increases on drawdown, doesn't decrease on recovery."""
    from src.shell.portfolio import PortfolioTracker

    db, db_path = await _make_db()
    try:
        config = load_config()
        kraken = MagicMock()
        tracker = PortfolioTracker(config, db, kraken)
        tracker._cash = 1000.0
        tracker._starting_cash = 1000.0
        tracker._daily_start_value = 1000.0

        # Create a position manually
        now = datetime.now(timezone.utc).isoformat()
        tracker._positions["test_btc"] = {
            "symbol": "BTC/USD", "tag": "test_btc", "side": "long",
            "qty": 0.01, "avg_entry": 50000.0, "current_price": 50000.0,
            "entry_fee": 0.5, "stop_loss": None, "take_profit": None,
            "intent": "DAY", "strategy_version": "v1", "opened_at": now,
            "updated_at": now, "max_adverse_excursion": 0.0,
        }

        # Price at entry — MAE stays 0
        tracker.refresh_prices({"BTC/USD": 50000.0})
        assert tracker._positions["test_btc"]["max_adverse_excursion"] == 0.0

        # Price drops to 49000 — MAE = 2%
        tracker.refresh_prices({"BTC/USD": 49000.0})
        mae = tracker._positions["test_btc"]["max_adverse_excursion"]
        assert abs(mae - 0.02) < 0.0001

        # Price drops more to 48000 — MAE = 4%
        tracker.refresh_prices({"BTC/USD": 48000.0})
        mae = tracker._positions["test_btc"]["max_adverse_excursion"]
        assert abs(mae - 0.04) < 0.0001

        # Price recovers to 49500 — MAE stays at 4% (doesn't decrease)
        tracker.refresh_prices({"BTC/USD": 49500.0})
        mae = tracker._positions["test_btc"]["max_adverse_excursion"]
        assert abs(mae - 0.04) < 0.0001

        # Price goes above entry — MAE still 4%
        tracker.refresh_prices({"BTC/USD": 51000.0})
        mae = tracker._positions["test_btc"]["max_adverse_excursion"]
        assert abs(mae - 0.04) < 0.0001

        await db.close()
    finally:
        os.unlink(db_path)


def test_mae_tracking_candidate_positions():
    """MAE tracking works for candidate positions via _build_portfolio()."""
    from src.candidates.runner import CandidateRunner

    # Create a simple strategy mock
    strategy = MagicMock()
    strategy.analyze.return_value = []

    runner = CandidateRunner(
        slot=1, strategy=strategy, version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
    )

    # Manually add a position
    runner._positions["c1_BTCUSD_001"] = {
        "symbol": "BTC/USD", "tag": "c1_BTCUSD_001", "side": "long",
        "qty": 0.01, "avg_entry": 50000.0, "current_price": 50000.0,
        "unrealized_pnl": 0.0, "entry_fee": 0.5, "stop_loss": None,
        "take_profit": None, "intent": "DAY", "strategy_version": "v_test",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "max_adverse_excursion": 0.0,
    }

    # Build portfolio with lower price — should update MAE
    runner._build_portfolio({"BTC/USD": 48000.0})
    pos = runner._positions["c1_BTCUSD_001"]
    assert abs(pos["max_adverse_excursion"] - 0.04) < 0.0001

    # Recovery — MAE stays
    runner._build_portfolio({"BTC/USD": 51000.0})
    assert abs(pos["max_adverse_excursion"] - 0.04) < 0.0001


def test_mae_carried_to_candidate_trade():
    """MAE is carried from position to trade on close."""
    from src.candidates.runner import CandidateRunner

    strategy = MagicMock()
    strategy.analyze.return_value = []

    runner = CandidateRunner(
        slot=1, strategy=strategy, version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
    )

    # Add position with some MAE
    runner._positions["c1_test"] = {
        "symbol": "BTC/USD", "tag": "c1_test", "side": "long",
        "qty": 0.01, "avg_entry": 50000.0, "current_price": 50000.0,
        "unrealized_pnl": 0.0, "entry_fee": 0.5, "stop_loss": None,
        "take_profit": None, "intent": "DAY", "strategy_version": "v_test",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "max_adverse_excursion": 0.035,
    }

    # Close the position
    trade = runner._close_position("c1_test", 51000.0, "signal")
    assert trade is not None
    assert trade["max_adverse_excursion"] == 0.035


# --- Phase 3: Candidate Signal Capture Tests ---

def test_candidate_signal_capture():
    """run_scan() captures signals in _pending_signals."""
    from src.candidates.runner import CandidateRunner

    strategy = MagicMock()
    strategy.analyze.return_value = [
        Signal(symbol="BTC/USD", action=Action.BUY, size_pct=0.05,
               intent=Intent.DAY, confidence=0.7, reasoning="test buy"),
    ]
    strategy.regime = "trending"

    runner = CandidateRunner(
        slot=1, strategy=strategy, version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
    )

    # Build SymbolData
    df = pd.DataFrame({
        "open": [50000], "high": [51000], "low": [49000],
        "close": [50500], "volume": [100],
    }, index=pd.DatetimeIndex([datetime.now(timezone.utc)]))

    markets = {
        "BTC/USD": SymbolData(
            symbol="BTC/USD", current_price=50000.0,
            candles_5m=df, candles_1h=df, candles_1d=df,
            spread=10.0, volume_24h=1000.0,
            maker_fee_pct=0.25, taker_fee_pct=0.40,
        ),
    }

    results = runner.run_scan(markets, datetime.now(timezone.utc))
    assert len(results) == 1  # BUY executed

    # Check signals captured
    signals = runner.get_new_signals()
    assert len(signals) == 1
    assert signals[0]["symbol"] == "BTC/USD"
    assert signals[0]["action"] == "BUY"
    assert signals[0]["acted_on"] == 1
    assert signals[0]["strategy_regime"] == "trending"
    assert signals[0]["rejected_reason"] is None

    # get_new_signals clears
    assert len(runner.get_new_signals()) == 0


def test_candidate_signal_capture_rejected():
    """Rejected signals are captured with reason."""
    from src.candidates.runner import CandidateRunner

    strategy = MagicMock()
    strategy.analyze.return_value = [
        Signal(symbol="INVALID/USD", action=Action.BUY, size_pct=0.05,
               intent=Intent.DAY, confidence=0.7, reasoning="bad symbol"),
    ]

    runner = CandidateRunner(
        slot=1, strategy=strategy, version="v_test",
        initial_cash=1000.0, initial_positions=[],
        risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
    )

    df = pd.DataFrame({
        "open": [50000], "high": [51000], "low": [49000],
        "close": [50500], "volume": [100],
    }, index=pd.DatetimeIndex([datetime.now(timezone.utc)]))

    markets = {
        "BTC/USD": SymbolData(
            symbol="BTC/USD", current_price=50000.0,
            candles_5m=df, candles_1h=df, candles_1d=df,
            spread=10.0, volume_24h=1000.0,
            maker_fee_pct=0.25, taker_fee_pct=0.40,
        ),
    }

    results = runner.run_scan(markets, datetime.now(timezone.utc))
    assert len(results) == 0

    signals = runner.get_new_signals()
    assert len(signals) == 1
    assert signals[0]["rejected_reason"] == "invalid_symbol"
    assert signals[0]["acted_on"] == 0


# --- Phase 4: Prediction Storage Tests ---

@pytest.mark.asyncio
async def test_store_predictions_valid():
    """_store_predictions() stores valid predictions."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        ai = MagicMock()
        reporter = MagicMock()
        data_store = MagicMock()

        orch = Orchestrator(config, db, ai, reporter, data_store)
        orch._cycle_id = "test_cycle_001"

        decision = {
            "predictions": [
                {
                    "claim": "BTC will break 60k",
                    "evidence": "strong momentum",
                    "falsification": "fails to reach 58k within 7 days",
                    "confidence": "high",
                    "evaluation_timeframe": "7 days",
                },
                {
                    "claim": "Candidate v2 will outperform",
                    "evidence": "better entry criteria",
                    "falsification": "lower total return after 14 days",
                    "confidence": "medium",
                },
            ]
        }

        await orch._store_predictions(decision)

        rows = await db.fetchall("SELECT * FROM predictions ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["claim"] == "BTC will break 60k"
        assert rows[0]["confidence"] == "high"
        assert rows[1]["claim"] == "Candidate v2 will outperform"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_store_predictions_empty():
    """_store_predictions() handles empty/missing predictions gracefully."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())
        orch._cycle_id = "test_cycle"

        # No predictions key
        await orch._store_predictions({})
        rows = await db.fetchall("SELECT * FROM predictions")
        assert len(rows) == 0

        # Empty list
        await orch._store_predictions({"predictions": []})
        rows = await db.fetchall("SELECT * FROM predictions")
        assert len(rows) == 0

        # Invalid prediction (missing falsification)
        await orch._store_predictions({"predictions": [{"claim": "x"}]})
        rows = await db.fetchall("SELECT * FROM predictions")
        assert len(rows) == 0

        await db.close()
    finally:
        os.unlink(db_path)


# --- Phase 5: Reflection System Tests ---

@pytest.mark.asyncio
async def test_should_reflect_never_reflected():
    """_should_reflect() returns True when never reflected and enough observations."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())

        # No observations, never reflected → False
        assert await orch._should_reflect() is False

        # Add 7 observations
        for i in range(7):
            await db.execute(
                "INSERT INTO orchestrator_observations (date, cycle_id, market_summary) VALUES (?, ?, ?)",
                (f"2026-01-{i+1:02d}", f"cycle_{i}", f"summary_{i}"),
            )
        await db.commit()

        # 7 observations, never reflected → True
        assert await orch._should_reflect() is True

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_should_reflect_interval():
    """_should_reflect() respects configurable reflection_interval_days (default 7)."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        # Default is 7 days
        assert config.orchestrator.reflection_interval_days == 7
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())

        # Set last reflection to 5 days ago — should NOT reflect
        five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        await db.execute(
            "INSERT INTO system_meta (key, value) VALUES ('last_reflection_date', ?)",
            (five_days_ago,),
        )
        await db.commit()
        assert await orch._should_reflect() is False

        # Set to 8 days ago — should reflect
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%d")
        await db.execute(
            "UPDATE system_meta SET value = ? WHERE key = 'last_reflection_date'",
            (eight_days_ago,),
        )
        await db.commit()
        assert await orch._should_reflect() is True

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_should_reflect_manual_trigger():
    """_should_reflect() returns True when reflect_tonight flag is set."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())

        # Set last reflection to 1 day ago — should NOT reflect normally
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        await db.execute(
            "INSERT INTO system_meta (key, value) VALUES ('last_reflection_date', ?)",
            (yesterday,),
        )
        await db.commit()
        assert await orch._should_reflect() is False

        # Set manual trigger
        await db.execute(
            "INSERT INTO system_meta (key, value) VALUES ('reflect_tonight', '1')"
        )
        await db.commit()
        assert await orch._should_reflect() is True

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_archive_strategy_doc():
    """_archive_strategy_doc() archives current doc to strategy_doc_versions."""
    from src.orchestrator.orchestrator import Orchestrator, STRATEGY_DOC_PATH

    db, db_path = await _make_db()
    try:
        config = load_config()
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())
        orch._cycle_id = "test_cycle"

        # Write a strategy doc to archive
        original = "# Test Strategy Doc\nSome content here."
        with patch.object(type(STRATEGY_DOC_PATH), 'exists', return_value=True):
            with patch.object(type(STRATEGY_DOC_PATH), 'read_text', return_value=original):
                await orch._archive_strategy_doc()

        rows = await db.fetchall("SELECT * FROM strategy_doc_versions")
        assert len(rows) == 1
        assert rows[0]["version"] == 1
        assert rows[0]["content"] == original
        assert rows[0]["reflection_cycle_id"] == "test_cycle"

        # Archive again — version increments
        with patch.object(type(STRATEGY_DOC_PATH), 'exists', return_value=True):
            with patch.object(type(STRATEGY_DOC_PATH), 'read_text', return_value="updated doc"):
                await orch._archive_strategy_doc()

        rows = await db.fetchall("SELECT * FROM strategy_doc_versions ORDER BY version")
        assert len(rows) == 2
        assert rows[1]["version"] == 2

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_reflection_full_flow():
    """Full reflection flow: archive old doc, grade predictions, write new doc, update system_meta."""
    from src.orchestrator.orchestrator import Orchestrator, STRATEGY_DOC_PATH

    db, db_path = await _make_db()
    try:
        config = load_config()
        ai = MagicMock()
        notifier = AsyncMock()

        orch = Orchestrator(config, db, ai, MagicMock(), MagicMock(), notifier=notifier)
        orch._cycle_id = "reflection_test"

        # Insert a prediction to grade
        await db.execute(
            """INSERT INTO predictions
               (cycle_id, claim, evidence, falsification, confidence)
               VALUES ('old_cycle', 'BTC goes up', 'momentum', 'drops 10%', 'medium')""",
        )
        await db.commit()
        pred = await db.fetchone("SELECT id FROM predictions LIMIT 1")
        pred_id = pred["id"]

        # Mock Opus response
        reflection_response = json.dumps({
            "graded_predictions": [
                {"prediction_id": pred_id, "grade": "confirmed",
                 "grade_evidence": "went up 5%", "grade_learning": "momentum works"},
            ],
            "strategy_document": "# Updated Strategy Doc\n\n## 1. Principles\nMomentum works.\n\n## 2. Lineage\nv1.\n\n## 3. Failures\nNone.\n\n## 4. Market\nTrending.\n\n## 5. Scorecard\n1/1.\n\n## 6. Active\nNone.",
            "reflection_summary": "Good period. Momentum confirmed.",
            "predictions": [
                {"claim": "new pred", "evidence": "data", "falsification": "opposite",
                 "confidence": "low", "evaluation_timeframe": "14 days"},
            ],
        })
        ai.ask_opus = AsyncMock(return_value=reflection_response)

        original_doc = "# Old strategy doc"
        written_content = []

        with patch.object(type(STRATEGY_DOC_PATH), 'exists', return_value=True):
            with patch.object(type(STRATEGY_DOC_PATH), 'read_text', return_value=original_doc):
                with patch.object(type(STRATEGY_DOC_PATH), 'write_text', side_effect=lambda c: written_content.append(c)):
                    await orch._reflect()

        # Check prediction was graded
        graded = await db.fetchone("SELECT * FROM predictions WHERE id = ?", (pred_id,))
        assert graded["grade"] == "confirmed"
        assert graded["graded_at"] is not None

        # Check new prediction was created
        new_preds = await db.fetchall("SELECT * FROM predictions WHERE cycle_id = 'reflection_test'")
        assert len(new_preds) == 1
        assert new_preds[0]["claim"] == "new pred"

        # Check strategy doc was archived
        archives = await db.fetchall("SELECT * FROM strategy_doc_versions")
        assert len(archives) == 1
        assert archives[0]["content"] == original_doc

        # Check strategy doc was written
        assert len(written_content) == 1
        assert "Updated Strategy Doc" in written_content[0]

        # Check last_reflection_date was set
        meta = await db.fetchone("SELECT value FROM system_meta WHERE key = 'last_reflection_date'")
        assert meta is not None
        assert meta["value"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check notifier was called
        notifier.reflection_completed.assert_called_once()

        await db.close()
    finally:
        os.unlink(db_path)


# --- Phase 7: Observability Tests ---

@pytest.mark.asyncio
async def test_store_observation_with_new_fields():
    """_store_observation() stores strategy_version, doc_flag, flag_reason."""
    from src.orchestrator.orchestrator import Orchestrator

    db, db_path = await _make_db()
    try:
        config = load_config()
        orch = Orchestrator(config, db, MagicMock(), MagicMock(), MagicMock())
        orch._cycle_id = "test_obs_cycle"

        # Insert a strategy version for _get_current_strategy_version
        await db.execute(
            "INSERT INTO strategy_versions (version, code_hash, deployed_at) VALUES ('v_test', 'abc', datetime('now'))"
        )
        await db.commit()

        decision = {
            "market_observations": "BTC trending up",
            "reasoning": "momentum strategy performing well",
            "cross_reference_findings": "correlation with ETH",
            "doc_flag": 1,
            "flag_reason": "significant momentum shift",
        }

        await orch._store_observation(decision)

        obs = await db.fetchone("SELECT * FROM orchestrator_observations WHERE cycle_id = 'test_obs_cycle'")
        assert obs is not None
        assert obs["strategy_version"] == "v_test"
        assert obs["doc_flag"] == 1
        assert obs["flag_reason"] == "significant momentum shift"

        await db.close()
    finally:
        os.unlink(db_path)


# --- Phase 8: Pruning Tests ---

@pytest.mark.asyncio
async def test_pruning_predictions():
    """Predictions pruned 30 days after grading."""
    from src.shell.data_store import DataStore

    db, db_path = await _make_db()
    try:
        config = load_config()
        data_store = DataStore(db, config.data)

        # Insert graded prediction from 31 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        await db.execute(
            """INSERT INTO predictions (cycle_id, claim, evidence, falsification,
               confidence, graded_at, grade) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("old", "old claim", "ev", "fals", "low", old_date, "confirmed"),
        )
        # Insert recent graded prediction
        await db.execute(
            """INSERT INTO predictions (cycle_id, claim, evidence, falsification,
               confidence, graded_at, grade) VALUES (?, ?, ?, ?, ?, datetime('now', 'utc'), ?)""",
            ("new", "new claim", "ev", "fals", "medium", "refuted"),
        )
        # Insert ungraded prediction (should NOT be pruned)
        await db.execute(
            """INSERT INTO predictions (cycle_id, claim, evidence, falsification,
               confidence) VALUES (?, ?, ?, ?, ?)""",
            ("pending", "pending claim", "ev", "fals", "high"),
        )
        await db.commit()

        await data_store.prune_old_data()

        rows = await db.fetchall("SELECT * FROM predictions")
        assert len(rows) == 2  # old graded one pruned
        claims = {r["claim"] for r in rows}
        assert "old claim" not in claims
        assert "new claim" in claims
        assert "pending claim" in claims

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_pruning_candidate_signals():
    """Candidate signals pruned 30 days after candidate resolved."""
    from src.shell.data_store import DataStore

    db, db_path = await _make_db()
    try:
        config = load_config()
        data_store = DataStore(db, config.data)

        # Insert resolved candidate from 31 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        await db.execute(
            """INSERT INTO candidates
               (slot, strategy_version, code, code_hash, portfolio_snapshot,
                status, created_at, resolved_at)
               VALUES (1, 'v1', 'code', 'hash', '{}', 'canceled', ?, ?)""",
            (old_date, old_date),
        )
        # Signal for that candidate
        await db.execute(
            """INSERT INTO candidate_signals
               (candidate_slot, symbol, action, size_pct) VALUES (1, 'BTC/USD', 'BUY', 0.05)""",
        )
        # Signal for running candidate (should NOT be pruned)
        await db.execute(
            """INSERT INTO candidates
               (slot, strategy_version, code, code_hash, portfolio_snapshot, status, created_at)
               VALUES (2, 'v2', 'code2', 'hash2', '{}', 'running', datetime('now', 'utc'))""",
        )
        await db.execute(
            """INSERT INTO candidate_signals
               (candidate_slot, symbol, action, size_pct) VALUES (2, 'ETH/USD', 'BUY', 0.03)""",
        )
        await db.commit()

        await data_store.prune_old_data()

        rows = await db.fetchall("SELECT * FROM candidate_signals")
        assert len(rows) == 1
        assert rows[0]["candidate_slot"] == 2

        await db.close()
    finally:
        os.unlink(db_path)


# --- Candidate Daily Snapshot Tests ---

@pytest.mark.asyncio
async def test_candidate_daily_snapshot_persist():
    """persist_state() writes daily performance snapshot for candidates."""
    from src.candidates.manager import CandidateManager
    from src.candidates.runner import CandidateRunner

    db, db_path = await _make_db()
    try:
        config = load_config()
        manager = CandidateManager(config, db)

        # Create a mock runner
        strategy = MagicMock()
        strategy.analyze.return_value = []

        runner = CandidateRunner(
            slot=1, strategy=strategy, version="v_snap_test",
            initial_cash=1000.0, initial_positions=[],
            risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        )
        runner._code = "code"
        manager._runners[1] = runner

        # Persist state
        await manager.persist_state()

        # Check daily performance was written
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = await db.fetchone(
            "SELECT * FROM candidate_daily_performance WHERE candidate_slot = 1 AND date = ?",
            (today,),
        )
        assert row is not None
        assert row["portfolio_value"] == 1000.0
        assert row["strategy_version"] == "v_snap_test"

        await db.close()
    finally:
        os.unlink(db_path)


@pytest.mark.asyncio
async def test_candidate_signal_persist():
    """persist_state() writes candidate signals to DB."""
    from src.candidates.manager import CandidateManager
    from src.candidates.runner import CandidateRunner

    db, db_path = await _make_db()
    try:
        config = load_config()
        manager = CandidateManager(config, db)

        strategy = MagicMock()
        strategy.analyze.return_value = []

        runner = CandidateRunner(
            slot=1, strategy=strategy, version="v_sig_test",
            initial_cash=1000.0, initial_positions=[],
            risk_limits=RISK_LIMITS, symbols=["BTC/USD"],
        )
        runner._code = "code"
        manager._runners[1] = runner

        # Manually add pending signals
        runner._pending_signals = [
            {
                "symbol": "BTC/USD", "action": "BUY", "size_pct": 0.05,
                "confidence": 0.8, "intent": "DAY", "reasoning": "test",
                "strategy_regime": "trending", "acted_on": 1,
                "rejected_reason": None, "tag": "c1_BTCUSD_001",
            },
        ]

        await manager.persist_state()

        rows = await db.fetchall("SELECT * FROM candidate_signals WHERE candidate_slot = 1")
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC/USD"
        assert rows[0]["strategy_regime"] == "trending"
        assert rows[0]["acted_on"] == 1

        # Signals should be cleared after persist
        assert len(runner._pending_signals) == 0

        await db.close()
    finally:
        os.unlink(db_path)
