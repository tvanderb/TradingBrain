"""Analyst Brain — AI-powered trade validation.

Uses Claude (Sonnet) to validate trading signals with cost-optimized prompts.
Only analyzes signals above the minimum strength threshold to conserve tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from src.brains.base import BaseBrain
from src.core.config import Config
from src.core.logging import get_logger
from src.core.tokens import TokenTracker, TokenUsage
from src.market.regime import MarketRegime, RegimeAnalysis
from src.market.signals import RawSignal
from src.storage.database import Database
from src.storage.models import Signal
from src.storage import queries

log = get_logger("analyst")


@dataclass
class AnalysisResult:
    valid: bool
    confidence: float
    reasoning: str
    suggested_size_pct: float  # 0-1, suggested position size as fraction of max
    raw_response: str


class AnalystBrain(BaseBrain):
    """AI-powered market analysis and trade validation."""

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
        self._model = config.analyst.model
        self._min_strength = config.analyst.min_signal_strength
        self._daily_limit = config.analyst.daily_token_limit
        self._active = False
        self._paused = False

    @property
    def name(self) -> str:
        return "analyst"

    @property
    def is_active(self) -> bool:
        return self._active and not self._paused

    async def start(self) -> None:
        self._active = True
        log.info("analyst_started", model=self._model)

    async def stop(self) -> None:
        self._active = False
        log.info("analyst_stopped")

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def should_analyze(self, signal: RawSignal) -> bool:
        """Token conservation: only analyze strong signals within budget."""
        if signal.strength < self._min_strength:
            return False
        return await self._tokens.check_budget("analyst", self._daily_limit)

    async def validate_signal(
        self,
        signal: RawSignal,
        regime: RegimeAnalysis,
        recent_trades: list[dict] | None = None,
    ) -> AnalysisResult:
        """Use Claude to validate a trading signal.

        Returns analysis with go/no-go decision.
        """
        if not self._active or self._paused:
            return AnalysisResult(
                valid=False, confidence=0, reasoning="Analyst inactive",
                suggested_size_pct=0, raw_response="",
            )

        # Cost-optimized prompt — minimal context, JSON response
        prompt = f"""Trade signal validation. Respond ONLY with JSON.

Signal: {signal.direction.upper()} {signal.symbol}
Type: {signal.signal_type}
Strength: {signal.strength:.2f}
Reason: {signal.reasoning}

Market: {regime.regime.value} (confidence={regime.confidence:.2f})
Volatility: ATR {regime.atr_pct}% of price
Context: {regime.description}

Fees: ~0.26% per trade (0.52% round trip)

{f"Recent trades: {json.dumps(recent_trades[:5])}" if recent_trades else "No recent trades."}

Evaluate:
1. Does signal align with market regime?
2. Is risk/reward favorable after fees?
3. Confidence (0-1)?
4. Suggested size (0-1 of max)?

JSON: {{"valid": bool, "confidence": float, "reasoning": "1 sentence", "size": float}}"""

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )

            usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=self._model,
            )
            await self._tokens.record("analyst", usage, purpose=f"validate_{signal.symbol}")

            raw = response.content[0].text.strip()

            # Parse JSON from response (handle markdown code blocks)
            json_str = raw
            if "```" in json_str:
                json_str = json_str.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
                json_str = json_str.strip()

            data = json.loads(json_str)

            result = AnalysisResult(
                valid=data.get("valid", False),
                confidence=float(data.get("confidence", 0)),
                reasoning=data.get("reasoning", ""),
                suggested_size_pct=float(data.get("size", 0.5)),
                raw_response=raw,
            )

            # Save signal to DB
            db_signal = Signal(
                symbol=signal.symbol,
                signal_type=signal.signal_type,
                strength=signal.strength,
                direction=signal.direction,
                reasoning=signal.reasoning,
                ai_response=raw,
                acted_on=result.valid,
            )
            await queries.insert_signal(self._db, db_signal)

            log.info(
                "signal_validated",
                symbol=signal.symbol,
                valid=result.valid,
                confidence=result.confidence,
                tokens=usage.total_tokens,
                cost=round(usage.cost_usd, 4),
            )
            return result

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            log.error("analysis_parse_error", error=str(e))
            return AnalysisResult(
                valid=False, confidence=0,
                reasoning=f"Parse error: {e}",
                suggested_size_pct=0, raw_response="",
            )
        except anthropic.APIError as e:
            log.error("anthropic_api_error", error=str(e))
            return AnalysisResult(
                valid=False, confidence=0,
                reasoning=f"API error: {e}",
                suggested_size_pct=0, raw_response="",
            )

    async def ask_question(self, question: str, context: dict | None = None, max_tokens: int = 300) -> str:
        """Answer a user question about market conditions or strategy.

        Used by the /ask and /report Telegram commands.
        """
        budget_ok = await self._tokens.check_budget("analyst", self._daily_limit)
        if not budget_ok:
            return "Token budget exceeded for today. Try again tomorrow."

        ctx = json.dumps(context, indent=2, default=str) if context else "No additional context."
        prompt = f"""You are a crypto trading analyst brain. Answer concisely using the data provided.

Context: {ctx}

Question: {question}"""

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )

            usage = TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                model=self._model,
            )
            await self._tokens.record("analyst", usage, purpose="user_question")

            return response.content[0].text.strip()
        except anthropic.APIError as e:
            return f"Error: {e}"
