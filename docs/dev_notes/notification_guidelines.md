# Notification Guidelines

Design spec for Telegram notifications. Follow these rules when adding new notification types.

## Philosophy

- **Data-rich**: Include enough context that the user doesn't need to open another command
- **Scannable**: Emoji prefix for visual anchoring on mobile, key numbers up front
- **Portfolio-aware**: Include portfolio value and position count where relevant
- **Consistent**: Follow the template structure below

## Template Structure

```
{EMOJI} {HEADLINE}
{KEY_DETAIL_1}
{KEY_DETAIL_2}
{PORTFOLIO_CONTEXT}
```

## Emoji Table

| Category | Emoji | Events |
|----------|-------|--------|
| Buy | `📈` | trade_executed (BUY), candidate BUY |
| Sell/Close | `📉` | trade_executed (SELL/CLOSE), candidate SELL |
| Stop Loss | `🛑` | stop_triggered, candidate_stop_triggered |
| Rejected | `⛔` | signal_rejected |
| Risk Halt | `🚨` | risk_halt |
| Resumed | `✅` | risk_resumed |
| Rollback | `⚠️` | strategy_rollback |
| Scan | `🔍` | scan_complete |
| Deploy | `🚀` | strategy_deployed |
| Nightly | `🌙` | orchestrator_cycle_completed |
| Online | `🟢` | system_online |
| Shutdown | `🔴` | system_shutdown |
| Error | `❌` | system_error, candidate_canceled |
| WS Lost | `⚠️` | websocket_feed_lost |
| Candidate | `🧪` | candidate_created |
| Promoted | `🏆` | candidate_promoted |
| Reflection | `🔬` | reflection_completed |
| Drought | `⏳` | signal_drought |

## Portfolio Context Rules

Include portfolio context (`Portfolio: $X | Positions: N/M`) on:
- All trade_executed events (BUY and SELL)
- stop_triggered
- system_online / system_shutdown
- risk_halt / risk_resumed
- scan_complete (portfolio value only)

Skip portfolio context on:
- signal_rejected (hasn't affected portfolio)
- orchestrator events (not trade-related)
- candidate events (separate paper portfolio)

## Data Density Rules

- **Price fields**: Always `:,.2f` format with `$` prefix
- **Percentages**: `:+.1f%` format (show sign)
- **P&L**: Always show both absolute (`$+5.10`) and percentage (`+2.5%`)
- **Hold duration**: Days + hours (e.g., `3d 14h`), hours only if <1d
- **Fee**: `$0.82` format, 2 decimal places
- **Qty**: 6 decimal places for crypto

## Checklist — Adding a New Notification

1. Add event name to `_EVENT_ACTIVITY` dict in `notifications.py`
2. Add `_format_activity()` handler for activity log summary
3. Add config toggle in `NotificationConfig` (default True for important, False for high-frequency)
4. Add method to `Notifier` class with emoji prefix
5. Use keyword-only args with defaults for optional enrichment (backward-compatible)
6. Add test in `test_integration.py`
7. Update this doc's emoji table
