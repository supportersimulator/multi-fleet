#!/usr/bin/env bash
# Multi-Fleet hook runner
# Checks fleet seed files and worker results, injects messages as context
# Uses MULTIFLEET_NODE_ID env var — works on any machine without hardcoding

HOOK_TYPE="${1:-session-start}"
_RAW_NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
# CV-001: Sanitize node ID — alphanumeric, dash, underscore only
NODE_ID=$(echo "$_RAW_NODE_ID" | tr -cd 'a-zA-Z0-9_-')
if [ -z "$NODE_ID" ]; then
  echo "[FLEET] ERROR: invalid node ID"
  exit 1
fi
SEED_FILE="/tmp/fleet-seed-${NODE_ID}.md"
# Session-specific seed file — CLAUDE_SESSION_ID is set by Claude Code runtime
SESSION_SEED_FILE=""
if [ -n "${CLAUDE_SESSION_ID:-}" ]; then
  # Sanitize session ID — alphanumeric, dash, underscore only
  _SAFE_SID=$(echo "$CLAUDE_SESSION_ID" | tr -cd 'a-zA-Z0-9_-')
  if [ -n "$_SAFE_SID" ]; then
    SESSION_SEED_FILE="/tmp/fleet-seed-${_SAFE_SID}.md"
  fi
fi
SEED_ARCHIVE="/tmp/fleet-seed-archive"
WORKER_RESULTS="/tmp/fleet-worker-results"
DAEMON_URL="http://127.0.0.1:8855"

# --- Shared helpers ---

_read_and_archive_seed() {
  # Helper: read a seed file, print contents, archive it.
  # Args: $1 = seed file path, $2 = label for log output
  local seed_path="$1"
  local label="${2:-seed}"
  if [ -f "$seed_path" ] && [ -s "$seed_path" ]; then
    echo "[FLEET INBOX] Messages for ${label}:"
    echo "---"
    cat "$seed_path"
    echo "---"
    # Archive the seed file
    mkdir -p "$SEED_ARCHIVE"
    mv "$seed_path" "${SEED_ARCHIVE}/$(basename "$seed_path" .md)-$(date +%s).md"
    return 0
  fi
  return 1
}

check_seed_inbox() {
  local found=1

  # Priority 1: Session-specific seed file (per-session targeting)
  if [ -n "$SESSION_SEED_FILE" ]; then
    if _read_and_archive_seed "$SESSION_SEED_FILE" "session ${_SAFE_SID:-unknown}"; then
      found=0
    fi
  fi

  # Priority 2: Node-level seed file (legacy fallback / broadcasts)
  if _read_and_archive_seed "$SEED_FILE" "node ${NODE_ID}"; then
    found=0
  fi

  return $found
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

ensure_api_keys() {
  # Load keys from keychain → fleet env fallback → AWS SM refresh
  # Never logs key values. Startup-only refresh (3-surgeon consensus 2026-04-05).
  local needs_refresh=0

  for key_name in Context_DNA_OPENAI Context_DNA_Deepseek; do
    # Try keychain first
    local val
    val=$(security find-generic-password -s "$key_name" -w 2>/dev/null)
    if [ -n "$val" ]; then
      export "$key_name=$val"
      continue
    fi

    # Try fleet env fallback
    if [ -f "$HOME/.fleet-nerve/env" ]; then
      val=$(grep "^${key_name}=" "$HOME/.fleet-nerve/env" 2>/dev/null | cut -d= -f2-)
      if [ -n "$val" ]; then
        export "$key_name=$val"
        # Store in keychain for next time
        security add-generic-password -s "$key_name" -a "atlas" -w "$val" 2>/dev/null
        continue
      fi
    fi

    # Neither source has it — need AWS refresh
    needs_refresh=1
  done

  if [ "$needs_refresh" = "1" ]; then
    # Pull from AWS Secrets Manager (startup-only, never on probe failure)
    if command -v aws &>/dev/null; then
      for key_name in Context_DNA_OPENAI Context_DNA_Deepseek; do
        local existing
        existing=$(security find-generic-password -s "$key_name" -w 2>/dev/null)
        [ -n "$existing" ] && continue

        local aws_val
        aws_val=$(aws secretsmanager get-secret-value --secret-id "$key_name" --query 'SecretString' --output text 2>/dev/null)
        if [ -n "$aws_val" ]; then
          security delete-generic-password -s "$key_name" 2>/dev/null
          security add-generic-password -s "$key_name" -a "atlas" -w "$aws_val" 2>/dev/null
          export "$key_name=$aws_val"
          echo "[FLEET] Refreshed $key_name from AWS Secrets Manager → Keychain"
        fi
      done
    fi
  fi
}

# --- Hook handlers ---

case "$HOOK_TYPE" in
  session-start)
    ensure_api_keys
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
        TARGET=$(basename "$msg" | sed 's/\.md$//' | tr -cd 'a-zA-Z0-9_-')
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
