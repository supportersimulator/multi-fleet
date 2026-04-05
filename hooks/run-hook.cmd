#!/usr/bin/env bash
# Multi-Fleet hook runner — self-healing + communication protocols
# Activates automatically on plugin install. Zero config beyond MULTIFLEET_NODE_ID.
# Gracefully degrades if daemon not running.

set -euo pipefail

HOOK_TYPE="${1:-session-start}"

# --- Node identity ---
_RAW_NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s)}"
NODE_ID=$(echo "$_RAW_NODE_ID" | tr -cd 'a-zA-Z0-9_-')
if [ -z "$NODE_ID" ]; then
  echo '{"systemMessage":"[FLEET] ERROR: invalid node ID — set MULTIFLEET_NODE_ID"}'
  exit 1
fi

# --- Paths ---
SEED_FILE="/tmp/fleet-seed-${NODE_ID}.md"
SEED_ARCHIVE="/tmp/fleet-seed-archive"
WORKER_RESULTS="/tmp/fleet-worker-results"
OUTBOX="/tmp/fleet-outbox-${NODE_ID}"
DAEMON_URL="http://127.0.0.1:8855"
GIT_INBOX="/tmp/fleet-git-inbox-${NODE_ID}"

# --- Output buffer (collect lines, emit as single JSON at end) ---
MSG_LINES=()

emit() {
  MSG_LINES+=("$1")
}

output_json() {
  local combined=""
  for line in "${MSG_LINES[@]+"${MSG_LINES[@]}"}"; do
    if [ -n "$combined" ]; then
      combined="${combined}\n${line}"
    else
      combined="$line"
    fi
  done
  if [ -n "$combined" ]; then
    python3 -c "import json,sys; print(json.dumps({'systemMessage': sys.argv[1]}))" "$combined"
  fi
}

# --- Shared helpers ---

daemon_is_running() {
  curl -sf --max-time 2 "$DAEMON_URL/health" >/dev/null 2>&1
}

start_daemon_if_needed() {
  if daemon_is_running; then
    return 0
  fi
  # Try to find and start the daemon
  local daemon_script=""
  for candidate in \
    "${CLAUDE_PLUGIN_ROOT:-}/../tools/fleet_nerve_nats.py" \
    "$(dirname "$0")/../../tools/fleet_nerve_nats.py" \
    "$HOME/.claude/plugins/multi-fleet/tools/fleet_nerve_nats.py"; do
    if [ -f "$candidate" ]; then
      daemon_script="$candidate"
      break
    fi
  done

  if [ -n "$daemon_script" ]; then
    nohup python3 "$daemon_script" serve >/tmp/fleet-daemon-${NODE_ID}.log 2>&1 &
    # Give it a moment to bind
    sleep 1
    if daemon_is_running; then
      emit "[FLEET] Daemon started on port 8855"
      return 0
    fi
  fi

  emit "[FLEET] Daemon not running — start with: python3 tools/fleet_nerve_nats.py serve"
  return 1
}

check_seed_inbox() {
  local found=1
  # Check file-based seed
  if [ -f "$SEED_FILE" ] && [ -s "$SEED_FILE" ]; then
    emit "[FLEET INBOX] Messages for ${NODE_ID}:"
    emit "---"
    emit "$(cat "$SEED_FILE")"
    emit "---"
    mkdir -p "$SEED_ARCHIVE"
    mv "$SEED_FILE" "${SEED_ARCHIVE}/fleet-seed-${NODE_ID}-$(date +%s).md"
    found=0
  fi

  # Check git-based inbox
  if [ -d "$GIT_INBOX" ] && [ "$(ls -A "$GIT_INBOX" 2>/dev/null)" ]; then
    local count
    count=$(ls -1 "$GIT_INBOX" | wc -l | tr -d ' ')
    emit "[FLEET GIT INBOX] ${count} git message(s):"
    for f in "$GIT_INBOX"/*; do
      emit "  $(basename "$f"): $(head -1 "$f")"
    done
    mkdir -p "$SEED_ARCHIVE"
    mv "$GIT_INBOX"/* "${SEED_ARCHIVE}/" 2>/dev/null || true
    found=0
  fi

  return $found
}

check_worker_results() {
  if [ -d "$WORKER_RESULTS" ] && [ "$(ls -A "$WORKER_RESULTS" 2>/dev/null)" ]; then
    local count
    count=$(ls -1 "$WORKER_RESULTS" | wc -l | tr -d ' ')
    emit "[FLEET WORKERS] ${count} completed result(s):"
    for f in "$WORKER_RESULTS"/*; do
      emit "  - $(basename "$f")"
    done
    return 0
  fi
  return 1
}

check_daemon_inbox() {
  if ! daemon_is_running; then
    return 1
  fi
  local inbox count
  inbox=$(curl -sf --max-time 2 "$DAEMON_URL/inbox" 2>/dev/null) || return 1
  count=$(echo "$inbox" | python3 -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null) || return 1
  if [ "$count" != "0" ] && [ -n "$count" ]; then
    emit "[FLEET] ${count} pending daemon message(s)"
    return 0
  fi
  return 1
}

fleet_health_summary() {
  if ! daemon_is_running; then
    return 1
  fi
  local fleet
  fleet=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null) || return 1
  local summary
  summary=$(echo "$fleet" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    peers = d.get('peers', [])
    channels = d.get('channels', [])
    surgeons = d.get('surgeons', {})
    parts = []
    parts.append(f'{len(peers)} peer(s): {', '.join(peers[:5])}' if peers else '0 peers')
    if channels:
        parts.append(f'{len(channels)} channel(s)')
    if surgeons:
        healthy = sum(1 for v in surgeons.values() if v.get('healthy'))
        parts.append(f'{healthy}/{len(surgeons)} surgeon(s) healthy')
    status = d.get('fleetStatus', '')
    if status:
        parts.append(status)
    print(' | '.join(parts))
except:
    pass
" 2>/dev/null)
  if [ -n "$summary" ]; then
    emit "[FLEET STATUS] $summary"
  fi
}

auto_pull_if_behind() {
  # Only pull if we're in a git repo and behind origin
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi
  # Fetch with timeout, don't block session start on network issues
  git fetch --quiet 2>/dev/null || return 0
  local local_rev remote_rev
  local_rev=$(git rev-parse HEAD 2>/dev/null) || return 0
  remote_rev=$(git rev-parse '@{upstream}' 2>/dev/null) || return 0
  if [ "$local_rev" != "$remote_rev" ]; then
    local behind
    behind=$(git rev-list --count HEAD..@{upstream} 2>/dev/null) || return 0
    if [ "$behind" -gt 0 ] 2>/dev/null; then
      git pull --ff-only --quiet 2>/dev/null && emit "[FLEET] Auto-pulled ${behind} commit(s) from origin" || emit "[FLEET] Behind origin by ${behind} commit(s) — manual pull needed (diverged)"
    fi
  fi
}

flush_outbox() {
  if [ ! -d "$OUTBOX" ] || [ -z "$(ls -A "$OUTBOX" 2>/dev/null)" ]; then
    return 0
  fi
  if ! daemon_is_running; then
    emit "[FLEET] Cannot flush outbox — daemon not running"
    return 1
  fi
  local count
  count=$(ls -1 "$OUTBOX" | wc -l | tr -d ' ')
  emit "[FLEET] Flushing ${count} queued message(s)..."
  for msg in "$OUTBOX"/*; do
    local target
    target=$(basename "$msg" | sed 's/\.md$//' | tr -cd 'a-zA-Z0-9_-')
    local payload
    payload=$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1]).read()))" "$msg" 2>/dev/null) || continue
    curl -sf --max-time 3 -X POST "$DAEMON_URL/message" \
      -H "Content-Type: application/json" \
      -d "{\"type\":\"seed\",\"target\":\"${target}\",\"payload\":${payload}}" 2>/dev/null \
      && rm -f "$msg"
  done
}

send_session_notification() {
  local event_type="$1"
  if ! daemon_is_running; then
    return 0
  fi
  curl -sf --max-time 2 -X POST "$DAEMON_URL/message" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"session-${event_type}\",\"source\":\"${NODE_ID}\",\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" 2>/dev/null || true
}

ensure_api_keys() {
  # Load keys from keychain -> fleet env fallback -> AWS SM refresh
  # Never logs key values. Startup-only refresh.
  local needs_refresh=0

  for key_name in Context_DNA_OPENAI Context_DNA_Deepseek; do
    local val
    val=$(security find-generic-password -s "$key_name" -w 2>/dev/null) || true
    if [ -n "$val" ]; then
      export "$key_name=$val"
      continue
    fi

    if [ -f "$HOME/.fleet-nerve/env" ]; then
      val=$(grep "^${key_name}=" "$HOME/.fleet-nerve/env" 2>/dev/null | cut -d= -f2-) || true
      if [ -n "$val" ]; then
        export "$key_name=$val"
        security add-generic-password -s "$key_name" -a "atlas" -w "$val" 2>/dev/null || true
        continue
      fi
    fi

    needs_refresh=1
  done

  if [ "$needs_refresh" = "1" ] && command -v aws &>/dev/null; then
    for key_name in Context_DNA_OPENAI Context_DNA_Deepseek; do
      local existing
      existing=$(security find-generic-password -s "$key_name" -w 2>/dev/null) || true
      [ -n "$existing" ] && continue

      local aws_val
      aws_val=$(aws secretsmanager get-secret-value --secret-id "$key_name" --query 'SecretString' --output text 2>/dev/null) || true
      if [ -n "$aws_val" ]; then
        security delete-generic-password -s "$key_name" 2>/dev/null || true
        security add-generic-password -s "$key_name" -a "atlas" -w "$aws_val" 2>/dev/null || true
        export "$key_name=$aws_val"
        emit "[FLEET] Refreshed ${key_name} from AWS Secrets Manager"
      fi
    done
  fi
}

# --- Hook handlers ---

case "$HOOK_TYPE" in
  session-start)
    # 1. Start daemon if not running
    start_daemon_if_needed

    # 2. Ensure API keys available
    ensure_api_keys

    # 3. Check inbox for pending messages
    check_seed_inbox || true
    check_daemon_inbox || true
    check_worker_results || true

    # 4. Fleet health summary
    fleet_health_summary || true

    # 5. Auto-pull if behind origin
    auto_pull_if_behind

    # Always emit something on session start so user knows fleet is active
    if [ ${#MSG_LINES[@]} -eq 0 ]; then
      emit "[FLEET] ${NODE_ID} online — no pending messages"
    fi

    output_json
    ;;

  prompt-submit)
    # Check inboxes — silent if nothing pending
    check_seed_inbox || true
    check_daemon_inbox || true
    check_worker_results || true

    # Only output if there are messages
    if [ ${#MSG_LINES[@]} -gt 0 ]; then
      output_json
    fi
    ;;

  teammate-idle)
    # Gentle wake — check for messages, asyncRewake if found
    has_messages=false

    if check_seed_inbox; then
      has_messages=true
    fi
    if check_daemon_inbox; then
      has_messages=true
    fi
    if check_worker_results; then
      has_messages=true
    fi

    if [ "$has_messages" = true ]; then
      output_json
      exit 2  # asyncRewake — wake the session
    fi

    # Nothing pending — stay idle
    exit 0
    ;;

  stop)
    # 1. Flush queued outbound messages
    flush_outbox

    # 2. Send session-end notification to fleet
    send_session_notification "end"

    emit "[FLEET] Session ended on ${NODE_ID}"
    output_json
    ;;

  *)
    echo '{"systemMessage":"[FLEET] Unknown hook type: '"$HOOK_TYPE"'"}'
    exit 1
    ;;
esac

exit 0
