#!/bin/bash
# Trading Brain — System Health Monitor
# Runs via cron every 15 minutes, appends JSON snapshots to monitor.jsonl
# Review after 72 hours to verify system stability.

set -euo pipefail

DEPLOY_DIR="/srv/trading-brain"
COMPOSE="docker compose -f ${DEPLOY_DIR}/docker-compose.yml"
DB="${DEPLOY_DIR}/data/brain.db"
OUT="${DEPLOY_DIR}/data/monitor.jsonl"
CONTAINER="trading-brain"

ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Container running?
container_running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo "false")

if [ "$container_running" != "true" ]; then
    echo "{\"ts\":\"${ts}\",\"status\":\"down\"}" >> "$OUT"
    exit 0
fi

# Resource usage
read cpu mem <<< $(docker stats "$CONTAINER" --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' | awk '{print $1, $2}')

# DB size
db_size=$(stat -c%s "$DB" 2>/dev/null || echo 0)

# DB queries — single sqlite3 call, pipe-separated
IFS='|' read -r scans last_scan c5m c1h c1d versions thoughts obs sigs trades positions ai_calls \
    orders cond_orders daily_perf_rows paper_tests \
    close_signal close_sl close_tp close_emergency close_recon \
    <<< $(sqlite3 "$DB" "
SELECT
  (SELECT COUNT(*) FROM scan_results),
  (SELECT MAX(created_at) FROM scan_results),
  (SELECT COUNT(*) FROM candles WHERE timeframe='5m'),
  (SELECT COUNT(*) FROM candles WHERE timeframe='1h'),
  (SELECT COUNT(*) FROM candles WHERE timeframe='1d'),
  (SELECT COUNT(*) FROM strategy_versions),
  (SELECT COUNT(*) FROM orchestrator_thoughts),
  (SELECT COUNT(*) FROM orchestrator_observations),
  (SELECT COUNT(*) FROM signals),
  (SELECT COUNT(*) FROM trades),
  (SELECT COUNT(*) FROM positions),
  (SELECT COUNT(*) FROM token_usage),
  (SELECT COUNT(*) FROM orders),
  (SELECT COUNT(*) FROM conditional_orders WHERE status='active'),
  (SELECT COUNT(*) FROM daily_performance),
  (SELECT COUNT(*) FROM paper_tests),
  (SELECT COUNT(*) FROM trades WHERE close_reason='signal'),
  (SELECT COUNT(*) FROM trades WHERE close_reason='stop_loss'),
  (SELECT COUNT(*) FROM trades WHERE close_reason='take_profit'),
  (SELECT COUNT(*) FROM trades WHERE close_reason='emergency'),
  (SELECT COUNT(*) FROM trades WHERE close_reason='reconciliation');
")

# Error count in recent logs (last 15 min window)
errors=$($COMPOSE logs --since 15m 2>&1 | grep -ciE 'error|exception|traceback' || true)
errors=${errors:-0}

# Build snapshot — single line JSON
printf '{"ts":"%s","status":"up","cpu":"%s","mem":"%s","db_bytes":%s,"errors_15m":%s,"scans":%s,"last_scan":"%s","candles_5m":%s,"candles_1h":%s,"candles_1d":%s,"strategy_versions":%s,"thoughts":%s,"observations":%s,"signals":%s,"trades":%s,"positions":%s,"ai_calls":%s,"orders":%s,"cond_orders_active":%s,"daily_perf_rows":%s,"paper_tests":%s,"close_signal":%s,"close_sl":%s,"close_tp":%s,"close_emergency":%s,"close_recon":%s}\n' \
    "$ts" "$cpu" "$mem" "$db_size" "$errors" \
    "$scans" "$last_scan" "$c5m" "$c1h" "$c1d" \
    "$versions" "$thoughts" "$obs" "$sigs" "$trades" "$positions" "$ai_calls" \
    "$orders" "$cond_orders" "$daily_perf_rows" "$paper_tests" \
    "$close_signal" "$close_sl" "$close_tp" "$close_emergency" "$close_recon" \
    >> "$OUT"
