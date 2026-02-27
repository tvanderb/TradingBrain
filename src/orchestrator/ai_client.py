"""AI Client — abstraction over Anthropic, Google Vertex, and OpenRouter APIs.

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

class AIClient:
    """Unified AI client supporting Anthropic, Vertex, and OpenRouter providers."""

    def __init__(self, config: AIConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._client = None  # Anthropic SDK client (anthropic/vertex)
        self._http = None    # httpx client (openrouter)
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
        elif self._config.provider == "openrouter":
            import httpx
            self._http = httpx.AsyncClient(
                base_url=self._config.openrouter_base_url,
                headers={
                    "Authorization": f"Bearer {self._config.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=300.0,
            )
            log.info("ai.initialized", provider="openrouter",
                     base_url=self._config.openrouter_base_url)
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
    def daily_tokens_used(self) -> int:
        return self._daily_tokens_used

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
        model = model or self._config.sonnet_model

        # Check daily token budget
        if self._daily_tokens_used >= self._config.daily_token_limit:
            log.warning("ai.daily_limit_reached", used=self._daily_tokens_used, limit=self._config.daily_token_limit)
            raise RuntimeError("Daily token limit reached")

        if self._config.provider == "openrouter":
            text, input_tokens, output_tokens = await self._call_openrouter(
                prompt, model, system, max_tokens, temperature,
            )
        else:
            text, input_tokens, output_tokens = await self._call_anthropic(
                prompt, model, system, max_tokens, temperature,
            )

        # Track tokens
        total_tokens = input_tokens + output_tokens
        self._daily_tokens_used += total_tokens

        # Calculate cost from config pricing
        costs = self._get_model_costs(model)
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

    def _get_model_costs(self, model: str) -> dict:
        """Resolve per-million-token pricing from config for the given model."""
        c = self._config
        if model == c.opus_model:
            return {"input": c.opus_input_cost, "output": c.opus_output_cost}
        elif model == c.haiku_model:
            return {"input": c.haiku_input_cost, "output": c.haiku_output_cost}
        else:  # sonnet or unknown — default to sonnet pricing
            return {"input": c.sonnet_input_cost, "output": c.sonnet_output_cost}

    async def _call_anthropic(
        self, prompt: str, model: str, system: str,
        max_tokens: int, temperature: float,
    ) -> tuple[str, int, int]:
        """Call via Anthropic SDK (anthropic/vertex providers)."""
        if self._client is None:
            raise RuntimeError("AI client not initialized — call initialize() first")

        messages = [{"role": "user", "content": prompt}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await self._client.messages.create(**kwargs)
                break
            except Exception as e:
                error_str = str(e).lower()
                is_transient = any(k in error_str for k in ("timeout", "rate", "429", "500", "502", "503", "529", "overloaded", "connection"))
                if not is_transient or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                log.warning("ai.retry", attempt=attempt + 1, error=str(e), wait=wait)
                await asyncio.sleep(wait)

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text

        return text, response.usage.input_tokens, response.usage.output_tokens

    async def _call_openrouter(
        self, prompt: str, model: str, system: str,
        max_tokens: int, temperature: float,
    ) -> tuple[str, int, int]:
        """Call via OpenRouter (OpenAI-compatible chat completions)."""
        if self._http is None:
            raise RuntimeError("AI client not initialized — call initialize() first")

        # OpenRouter model names: prefix with anthropic/ if needed
        or_model = model if "/" in model else f"anthropic/{model}"

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": or_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._http.post("/chat/completions", json=payload)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                error_str = str(e).lower()
                is_transient = any(k in error_str for k in ("timeout", "rate", "429", "500", "502", "503", "overloaded", "connection"))
                if not is_transient or attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                log.warning("ai.retry", attempt=attempt + 1, error=str(e), wait=wait, provider="openrouter")
                await asyncio.sleep(wait)

        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        return text, input_tokens, output_tokens

    async def ask_opus(self, prompt: str, system: str = "", max_tokens: int = 16384, purpose: str = "") -> str:
        """Shortcut for Opus model calls."""
        return await self.ask(prompt, model=self._config.opus_model, system=system,
                              max_tokens=max_tokens, purpose=purpose)

    async def ask_sonnet(self, prompt: str, system: str = "", max_tokens: int = 16384, purpose: str = "") -> str:
        """Shortcut for Sonnet model calls."""
        return await self.ask(prompt, model=self._config.sonnet_model, system=system,
                              max_tokens=max_tokens, purpose=purpose)

    async def ask_haiku(self, prompt: str, system: str = "", max_tokens: int = 4096, purpose: str = "") -> str:
        """Shortcut for Haiku model calls."""
        return await self.ask(prompt, model=self._config.haiku_model, system=system,
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
