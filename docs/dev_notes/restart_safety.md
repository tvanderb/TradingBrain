# Restart Safety & Configuration Landmines

**Purpose**: Make the system safe for an unfamiliar operator to restart, reconfigure, and migrate without silently corrupting state.

**Philosophy**: Every failure should be loud. Silent corruption is the worst outcome — it erodes trust in data that the entire system depends on.

---

## Landmine Inventory

### L1: Paper Cash Reset on Config Change (Critical — Silent Corruption)

**What happens**: `portfolio.py:50` initializes `self._cash = config.paper_balance_usd` before DB restoration. The DB restoration at lines 119-153 then attempts to override this from `daily_performance` snapshots. But if no snapshot exists yet (fresh start, first day), or the full reconciliation at line 135 uses `config.paper_balance_usd` as the `starting` value for first-principles recalculation — meaning a config change to `paper_balance_usd` retroactively rewrites the cash baseline for all historical trades.

**Scenario**: User runs paper mode for a week at $200, portfolio grows to $230. User changes config to `paper_balance_usd: 500` (thinking it's for a new run). Restarts. Line 135 recalculates: `cash = 500 + 0 + total_pnl - position_costs`. Cash jumps by $300 that was never deposited. Portfolio value is now wrong. All future P&L calculations are tainted.

**Severity**: Critical — silent data corruption, no error, no log warning.

**Fix plan**:
1. **Store starting capital in DB** at first boot (new table `system_config` or a row in `daily_performance`). Once set, `paper_balance_usd` from config is only used if DB has no recorded starting capital.
2. **On startup, compare** config value to DB value. If they differ, log a WARNING: `"Config paper_balance_usd ($500) differs from DB starting capital ($200). Using DB value. Use /deposit or /withdraw to adjust capital."`
3. **Capital changes go through `capital_events`** table only — the existing deposit/withdrawal mechanism. Config value becomes "initial seed" only, never re-read after first boot.
4. **Live mode is safe** — line 156-158 fetches balance from Kraken, doesn't use config.

---

### L2: Risk Limit Changes Don't Re-evaluate Halt State (Critical — Logic Error)

**What happens**: `risk.py` restores counters from DB at startup (lines 40-75) but never evaluates whether those restored counters already violate the *new* config limits. The halt checks only run inside `check_signal()` (lines 180-192), which requires a trade attempt.

**Scenario A — Loosening limits un-halts silently**: System was halted at 14 consecutive losses (limit was 15, so not halted — but close). User changes `rollback_consecutive_losses` from 15 to 20. No effect. But if system *was* halted at 15, and user changes limit to 20, the `_halted` flag is `False` on restart (line 37), and the counter (15) is below new limit (20). System resumes trading without any notification that it was previously halted.

**Scenario B — Tightening limits doesn't take effect immediately**: User tightens `max_drawdown_pct` from 0.40 to 0.20. Current drawdown is 30%. System should be halted but isn't — drawdown check only fires in `check_signal()` (line 181-186), not at startup. System happily accepts the next BUY signal, *then* halts.

**Scenario C — Daily loss counter across config change**: Daily P&L is -$15 (restored from DB). Old limit was 10% of $200 = $20 (not halted). New config changes portfolio or limit. The base value used at line 150 is `daily_start_value` which comes from `daily_performance` snapshot — unchanged. But if no snapshot exists, it falls back to current portfolio value, which may differ.

**Severity**: Critical — halted system can silently resume, or tightened limits don't take effect until first trade.

**Fix plan**:
1. **Add `evaluate_halt_state()` to RiskManager** — called once after `initialize()` completes. It checks all halt conditions (drawdown, consecutive losses, daily loss) against current values and sets `_halted` + `_halt_reason` immediately.
2. **In `main.py` startup**, after `risk.initialize()` and `portfolio.initialize()`, call `risk.evaluate_halt_state(portfolio_value, daily_start_value)`.
3. **Log clearly** if halt state is set on startup: `"risk.halt_on_startup"` with reason.
4. **Send Telegram notification** if system boots into halted state — user should know.

---

### L3: Orphaned Positions After Symbol Removal (High — Silent Neglect)

**What happens**: `portfolio.initialize()` loads ALL positions from DB regardless of configured symbols (line 114). But the scan loop (main.py) only iterates over `config.symbols`. Position monitor only checks SL/TP for positions whose symbol has a current price. If a symbol is removed from config, its positions become invisible to all monitoring.

**Scenario**: User has open SOL position. Removes SOL/USD from `config.symbols`. Restarts. Position is loaded into memory (line 116) but: no price updates from WebSocket, no scan loop checks, no SL/TP monitoring. Exchange-native SL/TP on Kraken would still fire, but the system wouldn't know — `_reconcile_orders()` only checks symbols in config.

**Severity**: High — position exists in portfolio value calculations (using stale price), but is unmonitored. In live mode, exchange orders could fill without the system recording them.

**Fix plan**:
1. **Startup validation**: After loading positions and config, compare position symbols against `config.symbols`. If any position exists for an unconfigured symbol, **log an ERROR** and **send Telegram alert**: `"WARNING: Open position for SOL/USD but symbol not in config. Position is unmonitored."`
2. **Don't auto-close** — that's a destructive action. Just make it loud.
3. **Consider**: Block startup entirely if orphaned positions exist in live mode (force user to close positions or re-add symbol before restarting).
4. **Reconciliation should check ALL position symbols**, not just config symbols.

---

### L4: Strategy File Missing = Hard Crash, No Fallback (Critical — Availability)

**What happens**: `loader.py:45-46` raises `RuntimeError` if `strategy/active/strategy.py` doesn't exist. `main.py:137-139` catches it and re-raises. Entire process exits. No Telegram notification (Telegram not initialized yet at that point in startup).

**Scenario**: Docker image rebuild copies code, but volume mount for `strategy/active/` is empty (first deploy, or volume deleted). Or orchestrator deployed bad code that passes sandbox but crashes at import. System is dead until manual intervention.

**Severity**: Critical for availability — no trading, no monitoring, no observability.

**Fix plan**:
1. **Store strategy code in DB** alongside the `strategy_versions` table (new column `code TEXT`). Already has `code_hash` — just add the source.
2. **Fallback chain in `load_strategy()`**: Try filesystem first → if missing/invalid, try latest deployed version from DB → if that fails too, try archive directory → if all fail, start in "paused" mode (no trading, but Telegram/API still up).
3. **"Paused" startup mode**: If strategy can't load, initialize everything *except* the scan loop. Telegram commands still work. API still works. User gets notified: `"System started in PAUSED mode — strategy failed to load: {error}. Fix and restart."` This is dramatically better than a dead process.
4. **Pre-flight check**: Before deploying new strategy via orchestrator, verify the *current* strategy file matches the expected hash. If filesystem was corrupted, don't overwrite — flag it.

---

### L5: Analysis Modules Missing = Silent Orchestrator Failure (Medium)

**What happens**: Analysis modules loaded lazily during orchestrator cycle (not at startup). If files are missing, orchestrator catches the error and includes `{"error": "..."}` in context. Orchestrator still runs but has degraded input. Not fatal for trading.

**Scenario**: Deploy to new Docker instance, forget to mount `statistics/active/` volume. System trades fine. But orchestrator sees `"error"` in analysis reports every night. It might make worse decisions due to missing data.

**Severity**: Medium — trading continues, orchestrator degrades gracefully but suboptimally.

**Fix plan**:
1. **Startup health check**: Verify analysis module files exist and are loadable. Log WARNING if missing. Don't block startup.
2. **Store analysis module code in DB** (same pattern as strategy — already has version/hash tracking in orchestrator).
3. **Telegram `/status` should show** whether analysis modules are loaded vs missing.

---

### L6: No Config Validation (Medium — Delayed Crash or Silent Corruption)

**What happens**: `config.py:143-239` reads TOML values with `.get()` defaults. Zero type checking. Zero range validation. The only validation is `max(1, ...)` for `min_paper_test_trades` at line 188.

**Bad configs that are silently accepted**:
- `paper_balance_usd: -100` → negative starting cash, every BUY appears to succeed
- `max_trade_pct: 5.0` → 500% of portfolio per trade (obviously wrong but accepted)
- `max_drawdown_pct: 0` → immediate halt on any drawdown
- `symbols: []` → system runs but never scans anything, appears healthy
- `timezone: "US/Easterrn"` → `ZoneInfo` crashes at `risk.initialize()` (after DB is open)
- `mode: "lve"` → not "paper" so `is_paper()` returns False, tries live trading without credentials

**Severity**: Medium — most bad configs cause crashes on first use, but some (negative balance, empty symbols, >100% trade size) cause silent corruption.

**Fix plan**:
1. **Add `validate()` method to Config** — called immediately after `load_config()` in `main.py`.
2. **Validations**:
   - `mode` must be `"paper"` or `"live"` (exact match)
   - `paper_balance_usd` must be > 0
   - All `_pct` fields must be 0 < x <= 1.0
   - `symbols` must be non-empty, each must match `XXX/USD` pattern
   - `timezone` must be valid `ZoneInfo` (try/catch at config load time)
   - `max_positions` must be >= 1
   - `max_daily_trades` must be >= 1
   - `default_trade_pct` <= `max_trade_pct`
   - `max_position_pct` >= `max_trade_pct` (can't have per-trade larger than per-position)
3. **Fail fast**: Invalid config = startup abort with clear error message listing ALL validation failures (not just the first one).
4. **Config summary log**: At startup, log all risk limits in a single structured message so user can verify.

---

### L7: Stale/Missing API Credentials (High — Silent Trading Failure)

**What happens**: Live mode checks that `api_key` and `secret_key` exist (main.py:83-85) but never validates they *work*. Paper mode doesn't check at all (no API needed). Telegram token is never validated.

**Scenario**: User rotates Kraken API keys, updates `.env`, runs `docker compose restart` (which does NOT re-read `.env` — documented gotcha). System starts with old keys. Every trade attempt fails with Kraken "EAPI:Invalid key". System is running but unable to trade. If user doesn't check Telegram (which might also be broken), they have no idea.

**Severity**: High — system appears healthy but can't execute any trades.

**Fix plan**:
1. **Startup credential probe**: In live mode, make a single `get_balance()` call to Kraken during startup (already happens at portfolio.py:157-158). If it fails with auth error, **abort startup** with clear message. Don't start trading if you can't talk to the exchange.
2. **Telegram probe**: Send a startup message (`"System starting..."`) during init. If it fails, log ERROR but continue (Telegram is observability, not critical path).
3. **`.env` reload reminder**: In docker-compose.yml, add a comment: `# IMPORTANT: Use 'docker compose up -d --force-recreate' after changing .env. 'restart' does NOT re-read .env.`
4. **Health endpoint**: `/v1/system` should include `kraken_connected: true/false` and `telegram_connected: true/false`.

---

### L8: Database Migration Crash Window (Critical — Data Loss Risk)

**What happens**: The special migration at `database.py:326-389` recreates the positions table to add the `tag` column. Between line 344 (`DROP TABLE`) and line 368 (new table created + data inserted), a crash would destroy the positions table with no recovery.

**Current state**: This migration is already complete on any existing brain.db (tag column exists). So this is only a risk for: (a) a truly ancient DB that somehow hasn't migrated, or (b) future migrations that follow the same pattern.

**Severity**: Critical if triggered, but unlikely with current DB. Pattern is dangerous for future use.

**Fix plan**:
1. **Wrap special migrations in a transaction** — SQLite supports transactional DDL. If the process crashes mid-migration, the transaction rolls back and the old table is preserved.
2. **Currently**: Lines 340-389 are NOT in an explicit transaction (they rely on autocommit, and line 387 calls `commit()`). The `DROP TABLE` at line 344 is immediately committed.
3. **Fix**: Wrap lines 340-387 in `BEGIN; ... COMMIT;` so the DROP + CREATE + INSERT is atomic.
4. **Pre-migration backup**: Before any special migration, copy brain.db to `brain.db.pre_migration`. Cheap insurance.
5. **Future pattern**: For any new table-recreate migration, always: backup → begin transaction → read old → drop old → create new → insert → commit.

---

### L9: Docker `.env` Gotcha (Medium — Well-Known but Undocumented in System)

**What happens**: `docker compose restart` reuses the existing container (and its baked-in environment). Only `docker compose up -d --force-recreate` re-reads `.env`.

**Severity**: Medium — documented in our dev notes but an unfamiliar operator wouldn't know.

**Fix plan**:
1. **Add `deploy/restart.sh`** helper script:
   ```bash
   #!/bin/bash
   # Safe restart that re-reads .env and config
   docker compose up -d --force-recreate
   docker compose logs -f --tail=50
   ```
2. **Add comment in docker-compose.yml** (near `env_file` line).
3. **DEPLOY.md already mentions this** — verify it's prominent.

---

## Implementation Priority

### Batch 1: Prevent Silent Corruption (do first)
| Fix | Landmine | Effort | Impact |
|-----|----------|--------|--------|
| Config validation | L6 | Small | Catches most operator errors at startup |
| Risk halt evaluation on startup | L2 | Small | Prevents halted system from silently resuming |
| Orphaned position detection | L3 | Small | Loud warning, prevents unmonitored positions |
| Starting capital in DB | L1 | Medium | Prevents cash corruption on config change |

### Batch 2: Improve Availability (do second)
| Fix | Landmine | Effort | Impact |
|-----|----------|--------|--------|
| Paused startup mode | L4 | Medium | System stays observable when strategy fails |
| Strategy code in DB (fallback) | L4 | Medium | Recovery path when filesystem is wrong |
| Credential probe on startup | L7 | Small | Fail fast on bad keys |

### Batch 3: Defensive Hardening (do third)
| Fix | Landmine | Effort | Impact |
|-----|----------|--------|--------|
| Transactional migrations | L8 | Small | Protects against future migration bugs |
| Analysis module health check | L5 | Small | Better observability |
| restart.sh helper | L9 | Tiny | Operator convenience |
| Store strategy/analysis code in DB | L4, L5 | Medium | Full system state in one artifact |

---

## Design Principles for Fixes

1. **Loud failures over silent corruption** — if state is inconsistent, log ERROR + send Telegram alert. Don't try to silently "fix" it.
2. **DB is the source of truth for runtime state** — config provides initial seed values and policy, DB provides accumulated state. Config should never retroactively rewrite DB-derived state.
3. **Startup is the validation boundary** — all checks happen here. If something is wrong, catch it before the scan loop starts.
4. **Degrade gracefully** — if a non-critical component fails (analysis module, Telegram), continue with degraded functionality. If a critical component fails (strategy, exchange, DB), abort or pause.
5. **Never auto-fix destructive state** — if positions are orphaned, don't auto-close them. Alert the operator. Humans make the call on irreversible actions.

---

## Files That Need Changes

| File | Changes |
|------|---------|
| `src/shell/config.py` | Add `validate()` method with all range/type checks |
| `src/shell/risk.py` | Add `evaluate_halt_state()` method |
| `src/shell/portfolio.py` | Store/read starting capital from DB; orphan detection |
| `src/shell/database.py` | Wrap special migrations in transaction; `system_config` table; strategy code column |
| `src/strategy/loader.py` | Fallback chain (filesystem → DB → archive → paused) |
| `src/main.py` | Call config validation, risk halt eval, orphan check, credential probe; support paused mode |
| `docker-compose.yml` | Add `.env` comment |
| `deploy/restart.sh` | New helper script |
