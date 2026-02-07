"""Executive Brain — Daily self-analysis and parameter evolution.

Uses Claude (Opus) to analyze daily performance and adjust strategy parameters.
Runs once per day after market close (or at configurable time for 24/7 crypto).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import anthropic

from src.brains.base import BaseBrain
from src.core.config import Config
from src.core.logging import get_logger
from src.core.tokens import TokenTracker, TokenUsage
from src.storage.database import Database
from src.storage.models import DailyPerformance
from src.storage import queries

log = get_logger("executive")


class ExecutiveBrain(BaseBrain):
    """Daily self-analysis and strategy parameter evolution."""

    def __init__(
        self,
        config: Config,
        db: Database,
        token_tracker: TokenTracker,
    ) -> None:
        self._config = config
        self._db = db
        self._tokens = token_tracker
        self._client = anthropic.AsyncAnthropic()
        self._model = config.executive.model
        self._daily_limit = config.executive.daily_token_limit
        self._active = False

        # Notification callback
        self.on_evolution_complete: callable | None = None

    @property
    def name(self) -> str:
        return "executive"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        self._active = True
        log.info("executive_started", model=self._model)

    async def stop(self) -> None:
        self._active = False

    async def daily_evolution_cycle(self) -> dict | None:
        """Main daily analysis and evolution process.

        Returns the evolution summary dict, or None if skipped.
        """
        if not self._active:
            return None

        budget_ok = await self._tokens.check_budget("executive", self._daily_limit)
        if not budget_ok:
            log.warning("executive_budget_exceeded")
            return None

        log.info("evolution_cycle_started")

        try:
            # Step 1: Gather data
            today = date.today()
            yesterday = (today - timedelta(days=1)).isoformat()
            week_ago = (today - timedelta(days=7)).isoformat()

            recent_perf = await queries.get_performance_range(self._db, week_ago, yesterday)
            recent_trades = await queries.get_trades_for_date(self._db, yesterday)
            latest_fees = await queries.get_latest_fees(self._db)
            current_params = self._config.strategy
            token_costs = await self._tokens.get_daily_cost_summary()

            # Step 2: Deep analysis via Claude
            analysis = await self._analyze(
                performance=recent_perf,
                trades=recent_trades,
                fees=latest_fees,
                current_params=current_params,
                token_costs=token_costs,
            )

            if analysis is None:
                return None

            # Step 3: Generate and apply parameter changes
            changes = await self._generate_changes(analysis, current_params)

            if changes and changes.get("adjustments"):
                self._apply_changes(changes["adjustments"])

            # Step 4: Log evolution
            await queries.insert_evolution_log(
                self._db,
                date_str=today.isoformat(),
                analysis=json.dumps(analysis),
                changes=json.dumps(changes) if changes else None,
                patterns=json.dumps(analysis.get("new_patterns", [])),
            )

            summary = {
                "date": today.isoformat(),
                "analysis": analysis,
                "changes": changes,
            }

            if self.on_evolution_complete:
                await self.on_evolution_complete(summary)

            log.info("evolution_cycle_complete", changes_made=bool(changes))
            return summary

        except Exception as e:
            log.error("evolution_cycle_error", error=str(e))
            return None

    async def _analyze(
        self,
        performance: list[DailyPerformance],
        trades: list,
        fees: object | None,
        current_params: object,
        token_costs: dict,
    ) -> dict | None:
        """Deep performance analysis via Claude."""

        perf_summary = []
        for p in performance:
            perf_summary.append({
                "date": p.date,
                "trades": p.total_trades,
                "wins": p.wins,
                "losses": p.losses,
                "net_pnl": p.net_pnl,
                "win_rate": p.win_rate,
                "token_cost": p.token_cost_usd,
            })

        trade_summary = []
        for t in trades[:20]:  # Limit to save tokens
            trade_summary.append({
                "symbol": t.symbol,
                "side": t.side,
                "pnl": t.pnl,
                "commission": t.commission,
                "notes": t.notes,
            })

        prompt = f"""You are the Executive Brain of a self-evolving crypto trading system.
Analyze performance and recommend parameter adjustments.

PERFORMANCE (last 7 days):
{json.dumps(perf_summary, indent=2) if perf_summary else "No data yet (system just started)."}

YESTERDAY'S TRADES:
{json.dumps(trade_summary, indent=2) if trade_summary else "No trades yesterday."}

CURRENT STRATEGY PARAMS:
{json.dumps({
    "momentum": current_params.momentum,
    "mean_reversion": current_params.mean_reversion,
    "volume": current_params.volume,
    "trend": current_params.trend,
}, indent=2)}

FEE INFO: {f"Maker: {fees.maker_fee_pct}%, Taker: {fees.taker_fee_pct}%" if fees else "Default: 0.16%/0.26%"}

TOKEN COSTS TODAY: {json.dumps(token_costs)}

CONSTRAINTS:
- Optimize for CONSISTENT profitability, not maximum gains
- Account for fees in all trade evaluations (0.52% round trip minimum)
- Reduce token costs where possible
- Parameter changes must be small and incremental
- If no data yet, provide baseline recommendations

Respond with JSON only:
{{
  "wins_analysis": "what worked",
  "losses_analysis": "what failed",
  "fee_impact": "how fees affected profitability",
  "token_efficiency": "token usage assessment",
  "parameter_recommendations": [
    {{"param": "momentum.threshold", "current": 0.65, "suggested": 0.70, "reason": "..."}}
  ],
  "new_patterns": ["pattern 1", "pattern 2"],
  "overall_assessment": "1-2 sentence summary"
}}"""

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )

            usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=self._model,
            )
            await self._tokens.record("executive", usage, purpose="daily_analysis")

            raw = response.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            return json.loads(raw)

        except (json.JSONDecodeError, anthropic.APIError) as e:
            log.error("executive_analysis_error", error=str(e))
            return None

    async def _generate_changes(self, analysis: dict, current_params: object) -> dict | None:
        """Convert analysis recommendations into concrete parameter changes."""
        recommendations = analysis.get("parameter_recommendations", [])
        if not recommendations:
            return None

        adjustments = {}
        for rec in recommendations:
            param_path = rec.get("param", "")
            suggested = rec.get("suggested")
            current = rec.get("current")

            if suggested is None or param_path == "":
                continue

            # Limit change magnitude (max 20% change per cycle)
            if current and current != 0:
                max_change = abs(current) * 0.2
                if abs(suggested - current) > max_change:
                    suggested = current + (max_change if suggested > current else -max_change)

            adjustments[param_path] = {
                "from": current,
                "to": suggested,
                "reason": rec.get("reason", ""),
            }

        return {"adjustments": adjustments} if adjustments else None

    def _apply_changes(self, adjustments: dict) -> None:
        """Apply parameter changes to strategy_params.json."""
        params = self._config.strategy

        for param_path, change in adjustments.items():
            parts = param_path.split(".")
            if len(parts) == 2:
                group, key = parts
                group_dict = getattr(params, group, None)
                if isinstance(group_dict, dict) and key in group_dict:
                    old = group_dict[key]
                    group_dict[key] = change["to"]
                    log.info(
                        "param_adjusted",
                        param=param_path,
                        old=old,
                        new=change["to"],
                        reason=change.get("reason", ""),
                    )

        params.version += 1
        params.last_updated = datetime.now().isoformat()
        params.updated_by = "executive_brain"
        params.save()

        log.info("strategy_params_saved", version=params.version)

    async def get_latest_evolution_summary(self) -> str:
        """Get human-readable summary of latest evolution for Telegram."""
        evo = await queries.get_latest_evolution(self._db)
        if not evo:
            return "No evolution cycles have run yet."

        analysis = json.loads(evo["analysis_json"]) if evo["analysis_json"] else {}
        changes = json.loads(evo["changes_json"]) if evo.get("changes_json") else {}

        lines = [f"Evolution ({evo['date']}):\n"]
        lines.append(analysis.get("overall_assessment", "No assessment."))

        if changes and changes.get("adjustments"):
            lines.append("\nChanges:")
            for param, detail in changes["adjustments"].items():
                lines.append(f"  {param}: {detail['from']} -> {detail['to']}")

        patterns = json.loads(evo["patterns_json"]) if evo.get("patterns_json") else []
        if patterns:
            lines.append("\nNew Patterns:")
            for p in patterns[:3]:
                lines.append(f"  - {p}")

        return "\n".join(lines)
