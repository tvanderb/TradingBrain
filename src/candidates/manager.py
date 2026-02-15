"""CandidateManager — lifecycle management for all candidate strategy slots.

Handles creation, cancellation, promotion, persistence, and recovery of
candidate strategies. Each candidate runs in its own CandidateRunner with
isolated paper portfolio state.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.candidates.runner import CandidateRunner
from src.shell.config import Config
from src.shell.contract import RiskLimits
from src.shell.database import Database
from src.strategy.sandbox import validate_strategy

log = structlog.get_logger()


class CandidateManager:
    """Manages all candidate strategy slots (up to max_candidates)."""

    def __init__(self, config: Config, db: Database) -> None:
        self._config = config
        self._db = db
        self._runners: dict[int, CandidateRunner] = {}  # slot -> runner
        self._notifier = None
        self._scan_counts: dict[int, int] = {}  # slot -> scan count since creation

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    async def initialize(self) -> None:
        """Startup recovery — restore running candidates from DB."""
        rows = await self._db.fetchall(
            "SELECT * FROM candidates WHERE status = 'running'"
        )
        for row in rows:
            slot = row["slot"]
            code = row["code"]
            version = row["strategy_version"]
            try:
                strategy = self._load_strategy_from_code(code, f"candidate_slot_{slot}")
                if strategy is None:
                    log.warning("candidate.recovery_failed", slot=slot, reason="strategy load failed")
                    continue

                # Restore positions
                pos_rows = await self._db.fetchall(
                    "SELECT * FROM candidate_positions WHERE candidate_slot = ?",
                    (slot,),
                )
                positions = [dict(r) for r in pos_rows]

                # Restore trades for status computation
                trade_rows = await self._db.fetchall(
                    "SELECT * FROM candidate_trades WHERE candidate_slot = ?",
                    (slot,),
                )

                # Parse portfolio snapshot for initial cash
                snapshot = json.loads(row["portfolio_snapshot"]) if row["portfolio_snapshot"] else {}
                initial_cash = snapshot.get("cash", self._config.paper_balance_usd)

                # Build risk limits
                risk_limits = self._build_risk_limits()

                runner = CandidateRunner(
                    slot=slot,
                    strategy=strategy,
                    version=version,
                    initial_cash=initial_cash,
                    initial_positions=[],  # We'll restore positions directly
                    risk_limits=risk_limits,
                    symbols=self._config.symbols,
                    slippage_factor=self._config.default_slippage_factor,
                    maker_fee_pct=self._config.kraken.maker_fee_pct,
                    taker_fee_pct=self._config.kraken.taker_fee_pct,
                )

                # Directly restore positions into runner (bypass cloning)
                runner._positions = {}
                for pos in positions:
                    tag = pos["tag"]
                    runner._positions[tag] = {
                        "symbol": pos["symbol"],
                        "tag": tag,
                        "side": pos.get("side", "long"),
                        "qty": pos["qty"],
                        "avg_entry": pos["avg_entry"],
                        "current_price": pos.get("current_price", pos["avg_entry"]),
                        "unrealized_pnl": pos.get("unrealized_pnl", 0),
                        "entry_fee": pos.get("entry_fee", 0),
                        "stop_loss": pos.get("stop_loss"),
                        "take_profit": pos.get("take_profit"),
                        "intent": pos.get("intent", "DAY"),
                        "strategy_version": version,
                        "opened_at": pos.get("opened_at", ""),
                        "max_adverse_excursion": pos.get("max_adverse_excursion", 0.0),
                    }

                # Restore completed trades for accurate status
                runner._trades = [dict(t) for t in trade_rows]
                runner._all_trades = [dict(t) for t in trade_rows]

                # Recalculate cash from trades
                # Cash = initial - sum(buys) + sum(sell proceeds)
                # Since we store positions, just recompute cash from snapshot
                # minus current position cost
                pos_cost = sum(p["avg_entry"] * p["qty"] for p in runner._positions.values())
                trade_pnl = sum(t.get("pnl", 0) or 0 for t in runner._trades)
                trade_fees = sum(t.get("fees", 0) or 0 for t in runner._trades)
                runner._cash = initial_cash - pos_cost + trade_pnl + trade_fees
                # Don't let cash go negative from rounding
                runner._cash = max(0, runner._cash)

                runner._code = code
                strategy.initialize(risk_limits, self._config.symbols)

                self._runners[slot] = runner
                log.info("candidate.recovered", slot=slot, version=version,
                         positions=len(runner._positions), trades=len(runner._trades))

            except Exception as e:
                log.error("candidate.recovery_error", slot=slot, error=str(e))

    async def create_candidate(
        self,
        slot: int,
        code: str,
        version: str,
        description: str = "",
        backtest_summary: str = "",
        evaluation_duration_days: int | None = None,
        portfolio_snapshot: dict | None = None,
        initial_positions: list[dict] | None = None,
    ) -> CandidateRunner:
        """Create a new candidate in the given slot.

        If the slot is occupied, the existing candidate is canceled first.
        """
        # Cancel existing candidate in slot if any
        if slot in self._runners:
            await self.cancel_candidate(slot, "replaced by new candidate")

        strategy = self._load_strategy_from_code(code, f"candidate_slot_{slot}")
        if strategy is None:
            raise RuntimeError(f"Failed to load candidate strategy for slot {slot}")

        risk_limits = self._build_risk_limits()
        strategy.initialize(risk_limits, self._config.symbols)

        snapshot = portfolio_snapshot or {"cash": self._config.paper_balance_usd, "positions": [], "total_value": self._config.paper_balance_usd}
        positions = initial_positions or []

        from src.strategy.loader import hash_code_string
        code_hash = hash_code_string(code)

        runner = CandidateRunner(
            slot=slot,
            strategy=strategy,
            version=version,
            initial_cash=snapshot.get("cash", self._config.paper_balance_usd),
            initial_positions=positions,
            risk_limits=risk_limits,
            symbols=self._config.symbols,
            slippage_factor=self._config.default_slippage_factor,
            maker_fee_pct=self._config.kraken.maker_fee_pct,
            taker_fee_pct=self._config.kraken.taker_fee_pct,
        )
        runner._code = code

        # Write to DB
        await self._db.execute(
            """INSERT OR REPLACE INTO candidates
               (slot, strategy_version, code, code_hash, description, backtest_summary,
                portfolio_snapshot, evaluation_duration_days, status, created_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', datetime('now', 'utc'), NULL)""",
            (slot, version, code, code_hash, description, backtest_summary,
             json.dumps(snapshot), evaluation_duration_days),
        )

        # Write initial positions
        for tag, pos in runner.get_positions().items():
            await self._db.execute(
                """INSERT INTO candidate_positions
                   (candidate_slot, symbol, tag, side, qty, avg_entry, current_price,
                    entry_fee, stop_loss, take_profit, intent, strategy_version, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (slot, pos["symbol"], tag, pos.get("side", "long"), pos["qty"],
                 pos["avg_entry"], pos.get("current_price", 0), pos.get("entry_fee", 0),
                 pos.get("stop_loss"), pos.get("take_profit"),
                 pos.get("intent", "DAY"), version, pos.get("opened_at")),
            )

        await self._db.commit()

        self._runners[slot] = runner
        log.info("candidate.created", slot=slot, version=version,
                 eval_days=evaluation_duration_days)
        return runner

    async def cancel_candidate(self, slot: int, reason: str = "") -> None:
        """Cancel a running candidate. Position/trade data stays in DB."""
        if slot in self._runners:
            del self._runners[slot]
        self._scan_counts.pop(slot, None)

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE candidates SET status = 'canceled', resolved_at = ? WHERE slot = ? AND status = 'running'",
            (now, slot),
        )
        await self._db.commit()
        log.info("candidate.canceled", slot=slot, reason=reason)

    async def promote_candidate(self, slot: int) -> str:
        """Promote a candidate: return its code string and cancel ALL candidates.

        Sets the promoted candidate's status to 'promoted' and all others to 'canceled'.
        """
        # Get the code from the runner
        runner = self._runners.get(slot)
        if not runner:
            raise RuntimeError(f"No running candidate in slot {slot}")

        code = runner._code
        if not code:
            # Fallback: read from DB
            row = await self._db.fetchone(
                "SELECT code FROM candidates WHERE slot = ? AND status = 'running'",
                (slot,),
            )
            if not row:
                raise RuntimeError(f"Cannot find code for candidate in slot {slot}")
            code = row["code"]

        now = datetime.now(timezone.utc).isoformat()

        # Mark promoted candidate
        await self._db.execute(
            "UPDATE candidates SET status = 'promoted', resolved_at = ? WHERE slot = ? AND status = 'running'",
            (now, slot),
        )

        # Cancel all other running candidates
        await self._db.execute(
            "UPDATE candidates SET status = 'canceled', resolved_at = ? WHERE slot != ? AND status = 'running'",
            (now, slot),
        )
        await self._db.commit()

        # Clear all runners and scan counts
        self._runners.clear()
        self._scan_counts.clear()

        log.info("candidate.promoted", slot=slot, version=runner.version)
        return code

    async def run_scans(self, markets: dict[str, SymbolData], timestamp: datetime) -> None:
        """Run strategy.analyze() for each active candidate."""
        from src.shell.contract import SymbolData  # avoid circular import at module level

        for slot, runner in list(self._runners.items()):
            try:
                results = runner.run_scan(markets, timestamp)
                if results:
                    log.info("candidate.scan_complete", slot=slot,
                             signals=len(results))
                    if self._notifier:
                        for trade in results:
                            await self._notifier.candidate_trade_executed(slot, trade)

                # Heartbeat every 10 scans
                self._scan_counts[slot] = self._scan_counts.get(slot, 0) + 1
                if self._scan_counts[slot] % 10 == 0:
                    status = runner.get_status()
                    log.info("candidate.heartbeat", slot=slot,
                             scans=self._scan_counts[slot],
                             positions=status["position_count"],
                             value=round(status["total_value"], 2))
            except Exception as e:
                log.error("candidate.scan_error", slot=slot, error=str(e))

    async def check_sl_tp(self, prices: dict[str, float]) -> None:
        """Check SL/TP for all active candidate positions."""
        for slot, runner in list(self._runners.items()):
            try:
                results = runner.check_sl_tp(prices)
                if results and self._notifier:
                    for trade in results:
                        await self._notifier.candidate_stop_triggered(slot, trade)
                        await self._notifier.candidate_trade_executed(slot, trade)
            except Exception as e:
                log.error("candidate.sl_tp_error", slot=slot, error=str(e))

    async def persist_state(self) -> None:
        """Persist current positions and new trades to DB for crash recovery."""
        for slot, runner in self._runners.items():
            try:
                # Delete and reinsert positions for this slot
                await self._db.execute(
                    "DELETE FROM candidate_positions WHERE candidate_slot = ?", (slot,),
                )
                for tag, pos in runner.get_positions().items():
                    await self._db.execute(
                        """INSERT INTO candidate_positions
                           (candidate_slot, symbol, tag, side, qty, avg_entry, current_price,
                            unrealized_pnl, entry_fee, stop_loss, take_profit, intent,
                            strategy_version, opened_at, updated_at, max_adverse_excursion)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'utc'), ?)""",
                        (slot, pos["symbol"], tag, pos.get("side", "long"), pos["qty"],
                         pos["avg_entry"], pos.get("current_price", 0),
                         pos.get("unrealized_pnl", 0), pos.get("entry_fee", 0),
                         pos.get("stop_loss"), pos.get("take_profit"),
                         pos.get("intent", "DAY"), runner.version,
                         pos.get("opened_at"), pos.get("max_adverse_excursion", 0.0)),
                    )

                # Insert new trades
                new_trades = runner.get_new_trades()
                for trade in new_trades:
                    await self._db.execute(
                        """INSERT INTO candidate_trades
                           (candidate_slot, symbol, side, qty, entry_price, exit_price,
                            pnl, pnl_pct, fees, intent, strategy_version, tag,
                            close_reason, opened_at, closed_at, max_adverse_excursion)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (slot, trade["symbol"], trade.get("side", "long"), trade["qty"],
                         trade.get("entry_price", 0), trade.get("exit_price"),
                         trade.get("pnl"), trade.get("pnl_pct"), trade.get("fees", 0),
                         trade.get("intent", "DAY"), runner.version, trade.get("tag"),
                         trade.get("close_reason"), trade.get("opened_at"),
                         trade.get("closed_at"), trade.get("max_adverse_excursion")),
                    )

                # Persist new signals
                new_signals = runner.get_new_signals()
                for sig in new_signals:
                    await self._db.execute(
                        """INSERT INTO candidate_signals
                           (candidate_slot, symbol, action, size_pct, confidence, intent,
                            reasoning, strategy_regime, acted_on, rejected_reason, tag)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (slot, sig["symbol"], sig["action"], sig["size_pct"],
                         sig.get("confidence"), sig.get("intent"), sig.get("reasoning"),
                         sig.get("strategy_regime"), sig.get("acted_on", 0),
                         sig.get("rejected_reason"), sig.get("tag")),
                    )

                # Daily performance snapshot
                status = runner.get_status()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                await self._db.execute(
                    """INSERT OR REPLACE INTO candidate_daily_performance
                       (candidate_slot, date, portfolio_value, cash, total_trades, wins,
                        losses, gross_pnl, net_pnl, fees_total, win_rate, strategy_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (slot, today, status["total_value"], status["cash"],
                     status["trade_count"], status["wins"], status["losses"],
                     status.get("pnl", 0), status.get("pnl", 0), 0,
                     status["win_rate"], runner.version),
                )

                await self._db.commit()
            except Exception as e:
                log.error("candidate.persist_error", slot=slot, error=str(e))

    async def get_context_for_orchestrator(self) -> list[dict]:
        """Build context for orchestrator's nightly analysis."""
        max_slots = self._config.orchestrator.max_candidates
        context = []

        for slot in range(1, max_slots + 1):
            runner = self._runners.get(slot)
            if runner:
                status = runner.get_status()
                # Add creation info from DB
                row = await self._db.fetchone(
                    "SELECT created_at, evaluation_duration_days, description FROM candidates WHERE slot = ? AND status = 'running'",
                    (slot,),
                )
                if row:
                    status["created_at"] = row["created_at"]
                    status["evaluation_duration_days"] = row["evaluation_duration_days"]
                    status["description"] = row["description"]
                context.append(status)
            else:
                context.append({"slot": slot, "status": "empty"})

        return context

    def get_active_slots(self) -> list[int]:
        """Return list of slot numbers with running candidates."""
        return list(self._runners.keys())

    def get_runner(self, slot: int) -> CandidateRunner | None:
        """Get a specific runner by slot."""
        return self._runners.get(slot)

    def _build_risk_limits(self) -> RiskLimits:
        """Build RiskLimits from config."""
        return RiskLimits(
            max_trade_pct=self._config.risk.max_trade_pct,
            default_trade_pct=self._config.risk.default_trade_pct,
            max_positions=self._config.risk.max_positions,
            max_daily_loss_pct=self._config.risk.max_daily_loss_pct,
            max_drawdown_pct=self._config.risk.max_drawdown_pct,
            max_position_pct=self._config.risk.max_position_pct,
            max_daily_trades=self._config.risk.max_daily_trades,
            rollback_consecutive_losses=self._config.risk.rollback_consecutive_losses,
        )

    def _load_strategy_from_code(self, code: str, module_name: str) -> object | None:
        """Load a strategy class from a code string using temp file + importlib."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(code)
                tmp_path = f.name

            # Clean up old module
            if module_name in sys.modules:
                del sys.modules[module_name]

            spec = importlib.util.spec_from_file_location(module_name, tmp_path)
            if spec is None or spec.loader is None:
                return None

            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)

            strategy_cls = getattr(mod, "Strategy", None)
            if strategy_cls is None:
                return None

            return strategy_cls()

        except Exception as e:
            log.error("candidate.load_strategy_failed", module=module_name, error=str(e))
            return None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass
            sys.modules.pop(module_name, None)
