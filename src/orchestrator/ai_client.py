"""AI Client — abstraction over Anthropic and Google Vertex APIs.

Provides a unified interface for calling Claude models.
Tracks token usage and costs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from src.shell.config import AIConfig
from src.shell.database import Database

log = structlog.get_logger()

# Cost per million tokens (approximate, as of 2025)
MODEL_COSTS = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
}


class AIClient:
    """Unified AI client supporting Anthropic and Vertex providers."""

    def __init__(self, config: AIConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._client = None
        self._daily_tokens_used: int = 0

    async def initialize(self) -> None:
        """Initialize the appropriate API client and seed token counter from DB."""
        if self._config.provider == "vertex":
            from anthropic import AsyncAnthropicVertex
            self._client = AsyncAnthropicVertex(
                project_id=self._config.vertex_project_id,
                region=self._config.vertex_region,
                timeout=300.0,
            )
            log.info("ai.initialized", provider="vertex",
                     project=self._config.vertex_project_id, region=self._config.vertex_region)
        else:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(
                api_key=self._config.anthropic_api_key,
                timeout=300.0,
            )
            log.info("ai.initialized", provider="anthropic")

        # Seed daily token counter from DB to survive restarts
        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) as total FROM token_usage WHERE created_at >= date('now')"
        )
        if row and row["total"]:
            self._daily_tokens_used = row["total"]
            log.info("ai.tokens_seeded", used_today=self._daily_tokens_used)

    @property
    def tokens_remaining(self) -> int:
        return max(0, self._config.daily_token_limit - self._daily_tokens_used)

    def reset_daily_tokens(self) -> None:
        self._daily_tokens_used = 0

    async def ask(
        self,
        prompt: str,
        model: str | None = None,
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        purpose: str = "",
    ) -> str:
        """Send a message to Claude and get a response.

        Args:
            prompt: The user message
            model: Model ID (defaults to sonnet)
            system: System prompt
            max_tokens: Max response tokens
            temperature: Creativity (0=deterministic, 1=creative)
            purpose: Description for token logging

        Returns:
            Response text
        """
        if self._client is None:
            raise RuntimeError("AI client not initialized — call initialize() first")

        model = model or self._config.sonnet_model

        # Check daily token budget
        if self._daily_tokens_used >= self._config.daily_token_limit:
            log.warning("ai.daily_limit_reached", used=self._daily_tokens_used, limit=self._config.daily_token_limit)
            raise RuntimeError("Daily token limit reached")

        messages = [{"role": "user", "content": prompt}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system

        # Retry with exponential backoff for transient errors
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                response = await self._client.messages.create(**kwargs)
                break
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # Retry on transient errors (network, rate limit, server errors)
                is_transient = any(k in error_str for k in ("timeout", "rate", "429", "500", "502", "503", "529", "overloaded", "connection"))
                if not is_transient or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt  # 1s, 2s, 4s
                log.warning("ai.retry", attempt=attempt + 1, error=str(e), wait=wait)
                await asyncio.sleep(wait)

        # Extract text
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        # Track tokens
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens
        self._daily_tokens_used += total_tokens

        # Calculate cost
        costs = MODEL_COSTS.get(model, {"input": 3.0, "output": 15.0})
        cost = (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000

        # Log to database
        await self._db.execute(
            """INSERT INTO token_usage (model, input_tokens, output_tokens, cost_usd, purpose)
               VALUES (?, ?, ?, ?, ?)""",
            (model, input_tokens, output_tokens, cost, purpose),
        )
        await self._db.commit()

        log.info("ai.response", model=model, input_tokens=input_tokens,
                 output_tokens=output_tokens, cost=f"${cost:.4f}", purpose=purpose)

        return text

    async def ask_opus(self, prompt: str, system: str = "", max_tokens: int = 4096, purpose: str = "") -> str:
        """Shortcut for Opus model calls."""
        return await self.ask(prompt, model=self._config.opus_model, system=system,
                              max_tokens=max_tokens, purpose=purpose)

    async def ask_sonnet(self, prompt: str, system: str = "", max_tokens: int = 8192, purpose: str = "") -> str:
        """Shortcut for Sonnet model calls."""
        return await self.ask(prompt, model=self._config.sonnet_model, system=system,
                              max_tokens=max_tokens, purpose=purpose)

    async def get_daily_usage(self) -> dict:
        """Get today's token usage summary."""
        rows = await self._db.fetchall(
            """SELECT model, SUM(input_tokens) as input_total, SUM(output_tokens) as output_total,
                      SUM(cost_usd) as cost_total, COUNT(*) as calls
               FROM token_usage WHERE created_at >= date('now')
               GROUP BY model"""
        )
        return {
            "models": {r["model"]: {
                "input": r["input_total"], "output": r["output_total"],
                "cost": r["cost_total"], "calls": r["calls"],
            } for r in rows},
            "total_cost": sum(r["cost_total"] for r in rows),
            "daily_limit": self._config.daily_token_limit,
            "used": self._daily_tokens_used,
        }
