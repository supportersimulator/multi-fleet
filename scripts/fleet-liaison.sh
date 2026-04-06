#!/bin/bash
# Launch fleet liaison agent as a background session
# The liaison handles all fleet communication so the coding agent stays focused.

set -euo pipefail

NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="/tmp/fleet-liaison-${NODE_ID}.pid"
STATE_FILE="/tmp/fleet-liaison-state.json"
BATCH_FILE="/tmp/fleet-liaison-batch.json"
OUTBOX_DIR="/tmp/fleet-outbox-${NODE_ID}"

usage() {
    echo "Usage: fleet-liaison.sh [start|stop|status|restart]"
    echo ""
    echo "  start   - Launch liaison as background session"
    echo "  stop    - Stop running liaison"
    echo "  status  - Check if liaison is running"
    echo "  restart - Stop then start"
    exit 1
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    return 1
}

do_start() {
    if is_running; then
        PID=$(cat "$PID_FILE")
        echo "Liaison already running (PID $PID)"
        exit 0
    fi

    # Ensure outbox directory exists
    mkdir -p "$OUTBOX_DIR"

    # Initialize state file
    cat > "$STATE_FILE" << EOF
{
  "node_id": "${NODE_ID}",
  "started": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "last_poll": null,
  "paused": false,
  "stats": {
    "received": 0,
    "silent": 0,
    "info": 0,
    "urgent": 0,
    "critical": 0,
    "sent": 0,
    "malformed": 0
  },
  "active_rebuttals": []
}
EOF

    # Initialize empty batch
    cat > "$BATCH_FILE" << EOF
{
  "updated": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "pending": {"info": 0, "urgent": 0, "critical": 0},
  "items": []
}
EOF

    echo "Starting fleet liaison for $NODE_ID..."

    # Launch method depends on available tooling:
    # 1. Superset task (preferred — managed lifecycle)
    # 2. claude --resume in tmux (fallback — manual lifecycle)
    # 3. Simple background process (minimal — no session management)

    if command -v superset &>/dev/null; then
        # Superset-managed launch
        superset task create \
            --name "fleet-liaison-${NODE_ID}" \
            --agent "fleet-liaison" \
            --prompt "Monitor fleet inbox for ${NODE_ID}. Process all messages per escalation matrix. Write batches to ${BATCH_FILE}." \
            --background &
        echo $! > "$PID_FILE"
        echo "Liaison started via Superset (PID $(cat "$PID_FILE"))"
    elif command -v claude &>/dev/null; then
        # tmux-based launch
        SESSION_NAME="fleet-liaison-${NODE_ID}"
        if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
            echo "tmux session $SESSION_NAME already exists"
            tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1 > "$PID_FILE"
        else
            tmux new-session -d -s "$SESSION_NAME" \
                "cd ${REPO_ROOT} && claude -p 'You are the fleet liaison for ${NODE_ID}. Monitor inbox, classify messages, batch notifications. Agent prompt: multi-fleet/agents/fleet-liaison.md. State: ${STATE_FILE}. Batch: ${BATCH_FILE}.' 2>&1 | tee /tmp/fleet-liaison-${NODE_ID}.log"
            tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1 > "$PID_FILE"
            echo "Liaison started in tmux session: $SESSION_NAME (PID $(cat "$PID_FILE"))"
        fi
    else
        echo "Error: neither superset nor claude CLI found"
        exit 1
    fi
}

do_stop() {
    if ! is_running; then
        echo "Liaison not running"
        return
    fi

    PID=$(cat "$PID_FILE")
    echo "Stopping liaison (PID $PID)..."

    # Try graceful shutdown first
    kill "$PID" 2>/dev/null || true
    sleep 1

    # Force if still running
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null || true
    fi

    rm -f "$PID_FILE"

    # Also kill tmux session if it exists
    SESSION_NAME="fleet-liaison-${NODE_ID}"
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

    echo "Liaison stopped"
}

do_status() {
    if is_running; then
        PID=$(cat "$PID_FILE")
        echo "Liaison RUNNING (PID $PID)"
    else
        echo "Liaison NOT RUNNING"
    fi

    # Show state if available
    if [ -f "$STATE_FILE" ]; then
        python3 -c "
import json
with open('${STATE_FILE}') as f:
    s = json.load(f)
p = s.get('stats', {})
print(f'  Node: {s.get(\"node_id\",\"?\")}')
print(f'  Paused: {s.get(\"paused\", False)}')
print(f'  Last poll: {s.get(\"last_poll\", \"never\")}')
print(f'  Received: {p.get(\"received\",0)} | Sent: {p.get(\"sent\",0)}')
print(f'  Pending: info={p.get(\"info\",0)} urgent={p.get(\"urgent\",0)} critical={p.get(\"critical\",0)}')
" 2>/dev/null || echo "  (state file unreadable)"
    fi
}

case "${1:-status}" in
    start)   do_start ;;
    stop)    do_stop ;;
    status)  do_status ;;
    restart) do_stop; do_start ;;
    *)       usage ;;
esac
