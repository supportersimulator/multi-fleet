#!/usr/bin/env bash
# Multi-Fleet hook runner
# Checks fleet seed files and worker results, injects messages as context
# Uses MULTIFLEET_NODE_ID env var — works on any machine without hardcoding

HOOK_TYPE="${1:-session-start}"
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
SEED_FILE="/tmp/fleet-seed-${NODE_ID}.md"
SEED_ARCHIVE="/tmp/fleet-seed-archive"
WORKER_RESULTS="/tmp/fleet-worker-results"
DAEMON_URL="http://127.0.0.1:8855"

# --- Shared helpers ---

check_seed_inbox() {
  if [ -f "$SEED_FILE" ] && [ -s "$SEED_FILE" ]; then
    echo "[FLEET INBOX] Messages for ${NODE_ID}:"
    echo "---"
    cat "$SEED_FILE"
    echo "---"
    # Archive the seed file
    mkdir -p "$SEED_ARCHIVE"
    mv "$SEED_FILE" "${SEED_ARCHIVE}/fleet-seed-${NODE_ID}-$(date +%s).md"
    return 0
  fi
  return 1
}

check_worker_results() {
  if [ -d "$WORKER_RESULTS" ] && [ "$(ls -A "$WORKER_RESULTS" 2>/dev/null)" ]; then
    local count
    count=$(ls -1 "$WORKER_RESULTS" | wc -l | tr -d ' ')
    echo "[FLEET WORKERS] ${count} completed result(s) in ${WORKER_RESULTS}:"
    for f in "$WORKER_RESULTS"/*; do
      echo "  - $(basename "$f")"
    done
    return 0
  fi
  return 1
}

check_daemon_health() {
  local status
  status=$(curl -sf --max-time 2 "$DAEMON_URL/health" 2>/dev/null)
  if [ -z "$status" ]; then
    echo "[FLEET] Daemon not running on port 8855"
    return 1
  fi
  echo "[FLEET] Daemon healthy"
  return 0
}

check_daemon_inbox() {
  local inbox count
  inbox=$(curl -sf --max-time 2 "$DAEMON_URL/inbox" 2>/dev/null)
  count=$(echo "$inbox" | python3 -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null)
  if [ "$count" != "0" ] && [ -n "$count" ]; then
    echo "[FLEET] ${count} pending daemon message(s)"
  fi
}

# --- Hook handlers ---

case "$HOOK_TYPE" in
  session-start)
    check_daemon_health
    check_daemon_inbox
    check_seed_inbox
    check_worker_results

    # Show fleet status
    FLEET=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null)
    if [ -n "$FLEET" ]; then
      echo "$FLEET" | python3 -c "import sys,json;s=json.load(sys.stdin).get('fleetStatus','');print(s) if s else None" 2>/dev/null
    fi
    ;;

  prompt-submit)
    check_seed_inbox
    check_worker_results
    ;;

  teammate-idle)
    # Check for pending work that could rewake the session
    if check_seed_inbox; then
      exit 0  # Found messages — rewake
    fi
    if check_worker_results; then
      exit 0  # Found results — rewake
    fi
    # Nothing pending — stay idle
    exit 1
    ;;

  stop)
    # Flush any unsent outbound messages
    OUTBOX="/tmp/fleet-outbox-${NODE_ID}"
    if [ -d "$OUTBOX" ] && [ "$(ls -A "$OUTBOX" 2>/dev/null)" ]; then
      echo "[FLEET] Flushing $(ls -1 "$OUTBOX" | wc -l | tr -d ' ') unsent message(s)..."
      for msg in "$OUTBOX"/*; do
        TARGET=$(basename "$msg" | sed 's/\.md$//')
        curl -sf --max-time 3 -X POST "$DAEMON_URL/message" \
          -H "Content-Type: application/json" \
          -d "{\"type\":\"seed\",\"target\":\"${TARGET}\",\"payload\":$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1]).read()))" "$msg")}" 2>/dev/null \
          && rm -f "$msg"
      done
    fi
    echo "[FLEET] Session ending on ${NODE_ID}"
    ;;

  *)
    echo "[FLEET] Unknown hook type: $HOOK_TYPE"
    exit 1
    ;;
esac

exit 0
