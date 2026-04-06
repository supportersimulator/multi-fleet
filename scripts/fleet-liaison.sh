#!/usr/bin/env bash
# Fleet Liaison Agent — launcher script
# Usage: fleet-liaison.sh [start|stop|status|restart]

set -euo pipefail

SESSION_NAME="fleet-liaison"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LIAISON_SCRIPT="${REPO_ROOT}/multi-fleet/liaison.py"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"

cmd="${1:-status}"

is_running() {
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

case "$cmd" in
    start)
        if is_running; then
            echo "[liaison] Already running in tmux session '$SESSION_NAME'"
            exit 0
        fi
        if [[ ! -f "$LIAISON_SCRIPT" ]]; then
            echo "[liaison] ERROR: $LIAISON_SCRIPT not found — mac3 building this"
            exit 1
        fi
        echo "[liaison] Starting background liaison agent (node: $NODE_ID)..."
        tmux new-session -d -s "$SESSION_NAME" \
            "cd '$REPO_ROOT' && MULTIFLEET_NODE_ID='$NODE_ID' python3 '$LIAISON_SCRIPT' 2>&1 | tee /tmp/fleet-liaison.log"
        echo "[liaison] Started in tmux session '$SESSION_NAME'"
        ;;
    stop)
        if ! is_running; then
            echo "[liaison] Not running"
            exit 0
        fi
        echo "[liaison] Stopping..."
        tmux send-keys -t "$SESSION_NAME" C-c
        sleep 1
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
        echo "[liaison] Stopped"
        ;;
    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;
    status)
        if is_running; then
            echo "[liaison] RUNNING in tmux session '$SESSION_NAME'"
            echo "[liaison] Log: /tmp/fleet-liaison.log"
            tail -3 /tmp/fleet-liaison.log 2>/dev/null || true
        else
            echo "[liaison] NOT RUNNING"
        fi
        ;;
    *)
        echo "Usage: $0 [start|stop|status|restart]"
        exit 1
        ;;
esac
