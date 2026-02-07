"""Token usage tracking and budget enforcement.

Tracks every Claude API call's token usage, calculates cost,
and enforces daily budgets per brain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.core.logging import get_logger
from src.storage.database import Database

log = get_logger("tokens")

# Pricing per million tokens (as of 2025)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-5-20250514": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
}


@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        pricing = MODEL_PRICING.get(self.model, {"input": 3.0, "output": 15.0})
        input_cost = (self.input_tokens / 1_000_000) * pricing["input"]
        output_cost = (self.output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost


class TokenTracker:
    """Tracks token usage per brain and enforces daily limits."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._daily_cache: dict[str, int] = {}
        self._cache_date: date | None = None

    async def record(
        self, brain: str, usage: TokenUsage, purpose: str = ""
    ) -> None:
        """Record a token usage event."""
        await self._db.execute(
            """INSERT INTO token_usage (brain, model, input_tokens, output_tokens, cost_usd, purpose)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (brain, usage.model, usage.input_tokens, usage.output_tokens, usage.cost_usd, purpose),
        )
        # Update cache
        today = date.today()
        if self._cache_date != today:
            self._daily_cache.clear()
            self._cache_date = today
        self._daily_cache[brain] = self._daily_cache.get(brain, 0) + usage.total_tokens

        log.info(
            "token_usage",
            brain=brain,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=round(usage.cost_usd, 6),
            purpose=purpose,
        )

    async def get_daily_usage(self, brain: str) -> int:
        """Get total tokens used today by a brain."""
        today = date.today()
        if self._cache_date != today:
            self._daily_cache.clear()
            self._cache_date = today

        if brain in self._daily_cache:
            return self._daily_cache[brain]

        row = await self._db.fetchone(
            """SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
               FROM token_usage
               WHERE brain = ? AND DATE(created_at) = DATE('now')""",
            (brain,),
        )
        total = row[0] if row else 0
        self._daily_cache[brain] = total
        return total

    async def check_budget(self, brain: str, limit: int) -> bool:
        """Return True if the brain is within its daily token budget."""
        used = await self.get_daily_usage(brain)
        return used < limit

    async def get_daily_cost_summary(self) -> dict[str, float]:
        """Get today's cost breakdown by brain."""
        rows = await self._db.fetchall(
            """SELECT brain, SUM(cost_usd)
               FROM token_usage
               WHERE DATE(created_at) = DATE('now')
               GROUP BY brain""",
        )
        return {row[0]: round(row[1], 4) for row in rows}
