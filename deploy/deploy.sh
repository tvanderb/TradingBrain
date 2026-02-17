#!/usr/bin/env bash
# deploy.sh — Lightweight deploy script for Trading Brain
#
# Rsyncs project files to VPS, then picks the minimum-downtime action:
#   Tier 1: config/strategy/statistics only → SIGHUP (zero downtime)
#   Tier 2: src/ or docker-compose.yml      → container restart (~5s)
#   Tier 3: pyproject.toml                  → image rebuild (15-20 min)
#
# Usage:
#   deploy/deploy.sh            # deploy everything
#   deploy/deploy.sh --dry-run  # preview without applying

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse inventory.yml for connection details
INVENTORY="$SCRIPT_DIR/inventory.yml"
if [[ ! -f "$INVENTORY" ]]; then
    echo "ERROR: inventory.yml not found at $INVENTORY"
    exit 1
fi

# Extract connection details (simple grep — works for flat YAML)
SSH_HOST=$(grep 'ansible_host:' "$INVENTORY" | head -1 | awk '{print $2}' | tr -d '"')
SSH_USER=$(grep 'ansible_user:' "$INVENTORY" | head -1 | awk '{print $2}' | tr -d '"')
SSH_KEY=$(grep 'ansible_ssh_private_key_file:' "$INVENTORY" | head -1 | awk '{print $2}' | tr -d '"')

# Resolve key path relative to inventory dir
if [[ ! "$SSH_KEY" = /* ]]; then
    SSH_KEY="$SCRIPT_DIR/$SSH_KEY"
fi

DEPLOY_DIR="/srv/trading-brain"
DRY_RUN=false
CONTAINER_NAME="trading-brain"

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

SSH_CMD="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new $SSH_USER@$SSH_HOST"

echo "=== Trading Brain Deploy ==="
echo "Target: $SSH_USER@$SSH_HOST:$DEPLOY_DIR"
echo "Dry run: $DRY_RUN"
echo ""

# Common rsync options
RSYNC_BASE=(
    rsync -az --itemize-changes
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"
    --exclude='__pycache__'
    --exclude='*.pyc'
    --exclude='.pytest_cache'
)

# Track what changed by category
TIER=0  # 0=nothing, 1=config/strategy, 2=src/compose, 3=deps

sync_and_detect() {
    local src="$1"
    local dest="$2"
    local tier="$3"
    local label="$4"
    local extra_args=("${@:5}")

    local output
    if $DRY_RUN; then
        output=$("${RSYNC_BASE[@]}" --dry-run "${extra_args[@]}" "$src" "$SSH_USER@$SSH_HOST:$dest" 2>&1 || true)
    else
        output=$("${RSYNC_BASE[@]}" "${extra_args[@]}" "$src" "$SSH_USER@$SSH_HOST:$dest" 2>&1 || true)
    fi

    # Check if any files were transferred (lines starting with > or c)
    local changed_files
    changed_files=$(echo "$output" | grep -c '^[>c]' || true)

    if [[ "$changed_files" -gt 0 ]]; then
        echo "[$label] $changed_files file(s) changed"
        if [[ -n "$output" ]]; then
            echo "$output" | grep '^[>c]' | head -10
            local total
            total=$(echo "$output" | grep -c '^[>c]' || true)
            if [[ "$total" -gt 10 ]]; then
                echo "  ... and $((total - 10)) more"
            fi
        fi
        if [[ "$tier" -gt "$TIER" ]]; then
            TIER="$tier"
        fi
    else
        echo "[$label] No changes"
    fi
}

# Sync all file groups
sync_and_detect "$PROJECT_ROOT/config/" "$DEPLOY_DIR/config/" 1 "config"
sync_and_detect "$PROJECT_ROOT/strategy/" "$DEPLOY_DIR/strategy/" 1 "strategy" --exclude='__pycache__'
sync_and_detect "$PROJECT_ROOT/statistics/" "$DEPLOY_DIR/statistics/" 1 "statistics" --exclude='__pycache__'
sync_and_detect "$PROJECT_ROOT/src/" "$DEPLOY_DIR/src/" 2 "src" --delete --exclude='__pycache__'
sync_and_detect "$PROJECT_ROOT/docker-compose.yml" "$DEPLOY_DIR/docker-compose.yml" 2 "docker-compose"
sync_and_detect "$PROJECT_ROOT/Dockerfile" "$DEPLOY_DIR/Dockerfile" 3 "Dockerfile"
sync_and_detect "$PROJECT_ROOT/pyproject.toml" "$DEPLOY_DIR/pyproject.toml" 3 "pyproject.toml"

# Monitoring — always sync, no signal needed (Prometheus/Grafana read from volume)
sync_and_detect "$PROJECT_ROOT/monitoring/" "$DEPLOY_DIR/monitoring/" 0 "monitoring" --delete

echo ""

# Apply the appropriate action
case "$TIER" in
    0)
        echo "=== No changes detected — nothing to do ==="
        ;;
    1)
        echo "=== Tier 1: Config/strategy/statistics changed — sending SIGHUP (zero downtime) ==="
        if ! $DRY_RUN; then
            $SSH_CMD "docker kill --signal=HUP $CONTAINER_NAME" 2>/dev/null || \
                echo "WARNING: SIGHUP failed — container may not be running"
        else
            echo "(dry run — would send SIGHUP)"
        fi
        ;;
    2)
        echo "=== Tier 2: Source code changed — restarting container (~5s downtime) ==="
        if ! $DRY_RUN; then
            $SSH_CMD "cd $DEPLOY_DIR && docker compose up -d --force-recreate $CONTAINER_NAME"
        else
            echo "(dry run — would restart container)"
        fi
        ;;
    3)
        echo "=== Tier 3: Dependencies changed — rebuilding image (15-20 min) ==="
        if ! $DRY_RUN; then
            $SSH_CMD "cd $DEPLOY_DIR && docker compose up -d --build --force-recreate $CONTAINER_NAME"
        else
            echo "(dry run — would rebuild and restart)"
        fi
        ;;
esac

echo ""
echo "Done."
