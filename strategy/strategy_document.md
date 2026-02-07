# Trading Strategy Document

> This document is the AI orchestrator's institutional memory.
> Read every night before making decisions. Updated after each review.
> Keep under ~2,000 words. Distill quarterly.

## 1. Current Market Thesis
*Updated: Not yet — system just deployed.*

No thesis established yet. Markets need to be observed before forming views.

## 2. Core Principles
- Fees eat 0.65-0.80% per round trip. Only trade with high conviction.
- Small account ($200) means position sizing matters enormously.
- Never fight the trend on shorter timeframes.
- Prefer fewer, higher-quality trades over many marginal ones.
- When in doubt, stay out.

## 3. Active Strategy Summary
**Version**: v001 — EMA Crossover + RSI + Volume
**Approach**: Day trading on 5-minute candles
**Entry**: EMA 9 crosses above EMA 21, RSI 30-70, volume > 1.2x average
**Exit**: EMA bearish crossover or stop-loss (2%) / take-profit (4%)
**Symbols**: BTC/USD, ETH/USD, SOL/USD

**Why this strategy**: Simple, well-understood baseline. Agent needs a starting point to iterate from. Not expected to be profitable immediately — it's a learning starting point.

## 4. Performance History
*No data yet — system just deployed.*

| Version | Period | Trades | Win Rate | Net P&L | Expectancy | Market |
|---------|--------|--------|----------|---------|------------|--------|
| v001    | -      | -      | -        | -       | -          | -      |

## 5. Risk Observations
- Round-trip fees of 0.65% require minimum ~1.5% favorable move to profit
- SOL/USD has lower liquidity, wider spreads — may need different parameters
- Low-volatility ranging markets will generate many false crossover signals

## 6. Adaptation Plan
- Observe v001 performance for at least 1 day of paper trading
- Identify which market conditions cause most losses
- Consider adding: MACD confirmation, ATR-based stops, regime filter
- Long-term: develop swing and position trading strategies alongside day trading

## 7. Market Condition Playbook
| Regime | Approach | Notes |
|--------|----------|-------|
| Trending up | Trade with trend, wider TP | EMA crossover works well |
| Trending down | Reduce position sizes, tighter SL | Consider sitting out |
| Ranging | Avoid crossover strategies | Many false signals |
| High volatility | Wider stops, smaller sizes | More opportunity but more risk |
| Low volatility | Reduce trading frequency | Fees dominate in small moves |
