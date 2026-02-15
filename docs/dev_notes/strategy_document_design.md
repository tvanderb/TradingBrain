# Strategy Document & Institutional Learning System — Design

> Designed in Session W (2026-02-14). This document captures the complete design direction
> agreed upon during collaborative discussion. Implementation planning starts from here.

## Philosophy

The strategy document is **Layer 3** of the orchestrator's three-layer prompt framework —
"Institutional Memory." It contains earned knowledge that the orchestrator discovers through
trading experience, not pre-loaded wisdom.

**Core insight**: Observation-and-summarize is *journaling*. Predict-then-evaluate is *learning*.
The system must make predictions, commit to falsifiable claims, and then rigorously grade them
against evidence. This is how real judgment improves over time.

**Inspiration**: Ray Dalio's "Pain + Reflection = Progress" — principles are created from
experience, refined over time, and periodically reviewed. BlackRock's "insights don't go down
in the elevator — they stay in the model."

**Governing principle**: "Maximize awareness, minimize direction." The system provides full
context and structure, but never tells the orchestrator what conclusions to draw.

---

## Two-Part Model: Predictions + Reflection

### Part 1: Predictions (Nightly, Integrated into Decisions)

Predictions emerge naturally from the orchestrator's decision-making context. They are
**optional on all nights** and **encouraged on action nights** (CREATE_CANDIDATE,
PROMOTE_CANDIDATE, CANCEL_CANDIDATE, analysis updates) through the prompt — not through
hard requirements.

The orchestrator predicts about things it's responsible for and things it designed:
- Strategy performance expectations
- Candidate outcome expectations
- Market regime impact on its strategies
- Design hypotheses about strategy code changes

**Prediction schema** (constrains structure, not content):

```
claim:                "what I'm predicting"
evidence:             "why I believe this"
falsification:        "how I'd know I'm wrong"
confidence:           low | medium | high
evaluation_timeframe: "when to check this"
```

The `falsification` field is the key — it defines exactly what the reflection should check,
making freeform predictions consistently gradeable.

Predictions are stored in a dedicated `predictions` table with a lifecycle:
`created → graded` (graded during reflection).

### Part 2: Reflection (Every 14 Days, Preamble to Nightly Cycle)

The reflection is NOT a separate process. It runs as the opening step of the nightly cycle
every 14 days. This keeps it tightly integrated with the decision-making context.

**14-day lockstep with observations**:
- Observations window: 14 days (changed from 30)
- Reflection runs on day 14, just before the oldest observations expire
- Every observation gets exactly one reflection pass before being pruned
- The rewritten strategy document carries forward the distilled insights

**Cycle flow on reflection night (day 14)**:

```
1. _gather_context()        — normal context gathering
2. _reflect()               — NEW: grades predictions, rewrites strategy doc
   a. Load current strategy document
   b. Query all predictions from past 14 days
   c. Query all observations from past 14 days
   d. Query structured evidence (trades, performance, candidates, versions, signals)
   e. Opus reflection call
      → Grade each prediction against evidence using its falsification criteria
      → Evaluate existing principles: still hold? refuted? need updating?
      → Extract new principles from graded predictions
      → Rewrite strategy document entirely
   f. Write strategy_document.md to disk
   g. Archive previous version in DB
   h. Store graded predictions in DB
   i. Store reflection in thoughts spool
3. Re-gather context         — now includes freshly updated strategy doc
4. _analyze()                — normal nightly analysis, informed by updated doc
5. Decide + predict          — decisions with predictions for next period
6. Execute + store           — normal execution, observation storage
```

The reflection runs BEFORE the main analysis, so the freshly updated institutional memory
immediately informs that night's decisions.

**Reflection prompt design** (maximize awareness, minimize direction):

Uses the same `LAYER_1_IDENTITY + FUND_MANDATE + LAYER_2_SYSTEM` system prompt as nightly
analysis. Same orchestrator identity, different task.

Input (full awareness):
- Current strategy document ("these are the principles you currently hold")
- 14 days of observations ("this is what you observed each night")
- All predictions from the period ("this is what you predicted")
- Flagged observations ("you marked these nights as significant")
- Structured evidence: all closed trades (fund + candidate), daily equity snapshots,
  candidate lifecycle events, strategy versions deployed, signals generated vs rejected

Output (structure without direction):
- For each prediction: grade it against evidence using its falsification criteria
- Review existing principles — do they still hold given new evidence?
- Produce an updated strategy document (full rewrite, replaces current)
- Generate predictions for the next 14-day period (optional, encouraged)

No directives about what to prioritize, what principles to extract, or how conservative
to be. The orchestrator's identity and system awareness handle that.

---

## Strategy Document Structure

The document is **rewritten from scratch** each reflection — not appended to. This prevents
unbounded growth and ensures relevance. Target: ~2,000 words.

### Sections

1. **Strategy Design Principles**
   - Earned, evidence-backed principles about strategy design (not trading tips)
   - Each carries a confidence level and sample size
   - Example: "ATR-based stops outperform fixed-percentage stops in volatile regimes
     (3 observations, medium confidence)"
   - Principles that hold across reflections get strengthened
   - Principles refuted by new evidence get removed or revised

2. **Strategy Lineage**
   - What's been tried, what worked, what didn't, and the design lessons
   - Not just a version table — the narrative of WHY strategies evolved
   - Example: "v002 used tight 5m entries but generated only 3 trades in 14 days —
     entry conditions were too restrictive for current volatility"

3. **Known Failure Modes**
   - Things that broke and why, at the code/design level
   - Example: "False bounce entries during pullbacks — entered SOL on 24h bounce while
     7d trend was negative, hit stop in 6 hours"

4. **Market Regime Understanding**
   - Non-directional. How different regimes affect the strategies.
   - NO thesis (no "I'm bullish on crypto"). A thesis could bias strategy design.
   - Example: "Trending environments have lasted 2-3 weeks before reversals. The current
     strategy captures trends well but gets whipsawed during transitions."

5. **Prediction Scorecard**
   - Last period's predictions with grades (confirmed/refuted/partial/inconclusive)
   - The accountability record — prevents unfalsifiable narratives
   - Over time, reveals what the orchestrator is good/bad at predicting
   - This meta-awareness is itself institutional knowledge

6. **Active Predictions**
   - Predictions for the current/next period
   - Specific, falsifiable, with evidence and falsification criteria
   - Carried forward from the most recent nightly cycle(s)

### Key Properties

- **Rewritten, not appended**: Each reflection produces a complete fresh document
- **Under ~2,000 words**: Synthesis, not accumulation
- **Evidence-backed**: Every claim references specific observations or trades
- **Self-correcting**: Wrong principles get removed, right ones get strengthened
- **Versioned**: Each version stored in DB before overwriting, preserving evolution history

---

## Architecture Integration

### Flow Diagram

```
                    STRATEGY DOCUMENT
                    (strategy_document.md)
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                  │
         ▼                 ▼                  ▼
    NIGHTLY CYCLE     NIGHTLY CYCLE      NIGHTLY CYCLE
    (Day 1)           (Day 2-13)         (Day 14)
         │                 │                  │
         ▼                 ▼                  ▼
    gather_context    gather_context     gather_context
         │                 │                  │
         ▼                 ▼                  ▼
    analyze (w/doc)   analyze (w/doc)    ┌─ REFLECT ─┐
         │                 │             │  grade     │
         ▼                 ▼             │  evaluate  │
    decide + predict  decide + predict   │  rewrite   │
         │                 │             │  doc_v(N+1)│
         ▼                 ▼             └────┬───────┘
    store obs +       store obs +             │
    store predictions store predictions  re-gather context
                                              │
                                         analyze (w/new doc)
                                              │
                                         decide + predict
                                              │
                                         store obs + predictions
```

### Document Evolution Over Time

```
doc_v1 (mostly empty)
  ──informs──► 14 nights of decisions + predictions
                    │
               reflection grades predictions
               evaluates principles against evidence
                    │
doc_v2 (first earned principles)
  ──informs──► next 14 nights
                    │
               reflection
                    │
doc_v3 (refined, some principles strengthened, others dropped)
  ──informs──► ...
```

### How Strategy Doc Feeds Into Existing Prompts

The strategy document is injected in two places (already existing):
1. **Opus analysis prompt** (`orchestrator.py:879`): Under "Strategy Document (Institutional
   Memory)" — informs nightly decision-making
2. **Sonnet code generation prompt** (`orchestrator.py:1001`): Under "Strategy Document" —
   informs strategy code design

No new injection points needed. The document just becomes richer over time.

---

## Data Architecture

### New Tables

#### `predictions`
```sql
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    claim TEXT NOT NULL,
    evidence TEXT NOT NULL,
    falsification TEXT NOT NULL,
    confidence TEXT NOT NULL,            -- 'low', 'medium', 'high'
    evaluation_timeframe TEXT,
    category TEXT,                       -- freeform: 'strategy', 'candidate', 'market', etc.
    graded_at TEXT,
    grade TEXT,                          -- 'confirmed', 'refuted', 'partially_confirmed', 'inconclusive'
    grade_evidence TEXT,                 -- what data confirmed/refuted it
    grade_learning TEXT,                 -- what this tells us
    created_at TEXT DEFAULT (datetime('now', 'utc'))
);
```

#### `strategy_doc_versions`
```sql
CREATE TABLE IF NOT EXISTS strategy_doc_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    reflection_cycle_id TEXT,
    created_at TEXT DEFAULT (datetime('now', 'utc'))
);
```

#### `candidate_signals`
```sql
CREATE TABLE IF NOT EXISTS candidate_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_slot INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    size_pct REAL NOT NULL,
    confidence REAL,
    intent TEXT,
    reasoning TEXT,
    strategy_regime TEXT,
    acted_on INTEGER DEFAULT 0,
    rejected_reason TEXT,
    tag TEXT,
    created_at TEXT DEFAULT (datetime('now', 'utc'))
);
```

#### `candidate_daily_performance`
```sql
CREATE TABLE IF NOT EXISTS candidate_daily_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_slot INTEGER NOT NULL,
    date TEXT NOT NULL,
    portfolio_value REAL,
    cash REAL,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    fees_total REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    strategy_version TEXT,
    UNIQUE(candidate_slot, date)
);
```

### Modified Tables

#### `orchestrator_observations` — add columns
```sql
ALTER TABLE orchestrator_observations ADD COLUMN strategy_version TEXT;
ALTER TABLE orchestrator_observations ADD COLUMN doc_flag INTEGER DEFAULT 0;
ALTER TABLE orchestrator_observations ADD COLUMN flag_reason TEXT;
```

#### `positions` and `candidate_positions` — add drawdown tracking
```sql
ALTER TABLE positions ADD COLUMN max_adverse_excursion REAL DEFAULT 0;
ALTER TABLE candidate_positions ADD COLUMN max_adverse_excursion REAL DEFAULT 0;
```

#### `trades` and `candidate_trades` — carry drawdown from position on close
```sql
ALTER TABLE trades ADD COLUMN max_adverse_excursion REAL;
ALTER TABLE candidate_trades ADD COLUMN max_adverse_excursion REAL;
```

### Fix: `strategy_regime` Population

Currently hard-coded as `None` in `main.py:724`. Fix: pass the strategy's regime
classification through to both fund and candidate trades.

For candidates, `runner.py` should also record `strategy_regime` on trades — currently
not populated.

### Retention / Pruning

| Table | Retention | Rationale |
|-------|-----------|-----------|
| `orchestrator_observations` | 14 days (changed from 30) | Lockstep with reflection cycle |
| `orchestrator_thoughts` | 30 days | Observability (Telegram, Grafana), wider than reflection window |
| `predictions` | 30 days after grading | Keep one cycle beyond grading for reference |
| `strategy_doc_versions` | Permanent | Institutional evolution history, small data |
| `candidate_signals` | 30 days after candidate resolved | Match candidate data lifecycle |
| `candidate_daily_performance` | 30 days after candidate resolved | Match candidate data lifecycle |
| `candidate_trades` | 30 days after candidate resolved | Existing pattern |
| `candidate_positions` | Pruned on cancel/promote | Existing behavior |

---

## Candidate Data Parity

Candidates should have the same data richness as fund activity, clearly labeled and pruned
when irrelevant. The principle: **candidates are a simulation of the fund — the data must match.**

### Current State (what already has parity)
- `candidate_trades` mirrors `trades` — same key columns
- `candidate_positions` mirrors `positions` — same key columns

### What Needs Parity

| Gap | Solution | Why |
|-----|----------|-----|
| **Candidate signals not logged** | New `candidate_signals` table | Can't evaluate "strategy generates X signals" predictions without signal-level data |
| **No candidate daily snapshots** | New `candidate_daily_performance` table | Can't evaluate "candidate equity stays above $X" predictions without equity curve |
| **strategy_regime not populated** | Fix in both fund (main.py) and candidate (runner.py) paths | Can't correlate trade outcomes with market regimes |
| **No position drawdown tracking** | `max_adverse_excursion` on positions + trades (both fund and candidate) | Can't evaluate "positions hold through volatility" predictions |
| **Candidate signal reasoning not captured** | Captured in `candidate_signals.reasoning` | Can't verify which entry logic fired |

---

## Nightly Cycle Changes (Summary)

### Analysis Prompt Additions

The Opus analysis decision JSON gets a new optional field:

```json
{
    "decision": "CREATE_CANDIDATE",
    "reasoning": "...",
    "predictions": [
        {
            "claim": "The candidate will generate 5-10 closed trades in 7 days",
            "evidence": "Entry conditions use 1h+5m confirmation across 9 symbols, moderate volatility environment",
            "falsification": "Fewer than 5 closed trades after 7 days means conditions are too restrictive",
            "confidence": "medium",
            "evaluation_timeframe": "7 days"
        }
    ],
    ...existing fields...
}
```

Predictions are prompted as optional on quiet nights, encouraged on action nights —
through the prompt phrasing, not hard enforcement.

### Observation Storage Additions

After storing the observation, also:
- Record `strategy_version` (the active fund strategy at observation time)
- Record `doc_flag` and `flag_reason` if the orchestrator flagged the observation
- Store any predictions from the decision JSON into the `predictions` table

### Reflection Trigger

Track reflection schedule via `system_meta` table:
- Key: `last_reflection_date`, value: ISO date string
- On each nightly cycle, check if 14 days have elapsed since last reflection
- If yes: run reflection before analysis, update `last_reflection_date`

---

## Reflection Data Gathering (What the Opus Reflection Call Receives)

### Layer A: What the Orchestrator Was Thinking (Narrative)
- Current strategy document (principles it currently holds)
- 14 days of observations (market assessment, reasoning, notable findings, flags)
- All predictions from the period (claims + falsification criteria)
- Thoughts spool entries for analysis steps (full Opus reasoning)

### Layer B: What Actually Happened (Ground Truth)
- All closed trades (fund + candidate): entry, exit, P&L, fees, close_reason, strategy_regime,
  max_adverse_excursion, tag, timestamps
- Open positions (fund + candidate): current state, unrealized P&L, MAE
- Daily performance snapshots (fund + candidate): equity curve through the period
- Candidate lifecycle: created, canceled, promoted — with reasons and performance at resolution
- Strategy versions: what code was deployed when, backtest results
- Signals (fund + candidate): generated, acted_on, rejected + reasons
- Flagged observations: which nights the orchestrator marked as significant

### Why Both Layers

Layer A provides the narrative — what the orchestrator was thinking and predicting.
Layer B provides the evidence — what actually happened in the system and markets.

The reflection cross-references them: "On day 3, I predicted X (Layer A). The evidence
shows Y actually happened (Layer B). My prediction was wrong because Z. This tells me..."

This diagnostic precision is what produces real principles vs hand-waving. Without Layer B,
the reflection can rationalize anything. Without Layer A, it loses the context of why
decisions were made.

---

## Open Items for Implementation Planning

1. **Exact prompt wording** for predictions field in analysis JSON schema
2. **Exact prompt wording** for the reflection call
3. **CandidateRunner changes** for signal logging, regime tracking, daily snapshots
4. **Main loop changes** for strategy_regime passthrough, MAE tracking
5. **Database migrations** for all new columns and tables
6. **Pruning logic** updates (14d observations, candidate data lifecycle)
7. **Reflection scheduling** mechanism in orchestrator
8. **Tests** for all new functionality
9. **Grafana/Prometheus** updates for prediction and reflection observability
