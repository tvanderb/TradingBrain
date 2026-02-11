#!/usr/bin/env bash
# Restart trading-brain with .env changes applied.
# 'docker compose restart' does NOT re-read .env — use this script instead.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Recreating container (reads fresh .env)..."
docker compose up -d --force-recreate

echo "Tailing logs (Ctrl+C to stop)..."
docker compose logs -f --tail=50
