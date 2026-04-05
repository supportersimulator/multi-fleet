#!/usr/bin/env bash
<<<<<<< Updated upstream
# Multi-Fleet hook runner — self-healing + communication protocols
# Activates automatically on plugin install. Zero config beyond MULTIFLEET_NODE_ID.
# Gracefully degrades if daemon not running.

set -euo pipefail
=======
# Multi-Fleet hook runner — auto-healing on every session event
# Checks fleet seed files and worker results, injects messages as context
# Uses MULTIFLEET_NODE_ID env var — works on any machine without hardcoding
#
# Invariants:
#   - All hooks complete within 10 seconds (timeout enforced in hooks.json)
#   - Never expose secrets in output
#   - Use asyncRewake for non-blocking fleet message injection
#   - Log errors but never crash the hook (exit 0 on error)

set -o pipefail
>>>>>>> Stashed changes

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
<<<<<<< Updated upstream
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
=======
OUTBOX="/tmp/fleet-outbox-${NODE_ID}"
FLEET_STATE="/tmp/fleet-state-${NODE_ID}"
LAST_INJECT_FILE="/tmp/fleet-last-inject-${NODE_ID}"
INBOX_ARCHIVE="/tmp/fleet-inbox-archive-${NODE_ID}"
>>>>>>> Stashed changes

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
<<<<<<< Updated upstream
  # Never logs key values. Startup-only refresh.
=======
  # Never logs key values. Startup-only refresh (3-surgeon consensus 2026-04-05).
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream
      local aws_val
      aws_val=$(aws secretsmanager get-secret-value --secret-id "$key_name" --query 'SecretString' --output text 2>/dev/null) || true
      if [ -n "$aws_val" ]; then
        security delete-generic-password -s "$key_name" 2>/dev/null || true
        security add-generic-password -s "$key_name" -a "atlas" -w "$aws_val" 2>/dev/null || true
        export "$key_name=$aws_val"
        emit "[FLEET] Refreshed ${key_name} from AWS Secrets Manager"
      fi
    done
=======
        local aws_val
        aws_val=$(aws secretsmanager get-secret-value --secret-id "$key_name" --query 'SecretString' --output text 2>/dev/null)
        if [ -n "$aws_val" ]; then
          security delete-generic-password -s "$key_name" 2>/dev/null
          security add-generic-password -s "$key_name" -a "atlas" -w "$aws_val" 2>/dev/null
          export "$key_name=$aws_val"
          echo "[FLEET] Refreshed $key_name from AWS Secrets Manager -> Keychain"
        fi
      done
    fi
>>>>>>> Stashed changes
  fi
}

# Check peer channel health and spawn healing if degraded
check_peer_health_and_heal() {
  local fleet_data degraded_peers
  fleet_data=$(curl -sf --max-time 3 "$DAEMON_URL/fleet/live" 2>/dev/null) || return 0

  degraded_peers=$(echo "$fleet_data" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    peers = data.get('peers', data.get('fleet', []))
    if isinstance(peers, dict):
        peers = [{'id': k, **v} for k, v in peers.items()]
    degraded = []
    for p in peers:
        channels = p.get('channels', {})
        for ch_name, ch_status in channels.items():
            if isinstance(ch_status, dict):
                st = ch_status.get('status', 'unknown')
            else:
                st = str(ch_status)
            if st in ('degraded', 'down', 'error', 'unreachable'):
                degraded.append(f\"{p.get('id','?')}:{ch_name}={st}\")
    if degraded:
        print('|'.join(degraded))
except:
    pass
" 2>/dev/null)

  if [ -n "$degraded_peers" ]; then
    echo "[FLEET HEAL] Degraded channels detected: ${degraded_peers}"
    # Spawn background healing — write request file for daemon to pick up
    mkdir -p "$FLEET_STATE"
    echo "{\"action\":\"heal\",\"degraded\":\"${degraded_peers}\",\"ts\":$(date +%s),\"requestor\":\"${NODE_ID}\"}" \
      > "${FLEET_STATE}/heal-request-$(date +%s).json"
    # Attempt direct daemon heal endpoint (non-blocking, best-effort)
    curl -sf --max-time 2 -X POST "$DAEMON_URL/fleet/heal" \
      -H "Content-Type: application/json" \
      -d "{\"degraded\":\"${degraded_peers}\",\"requestor\":\"${NODE_ID}\"}" 2>/dev/null &
  fi
}

# Report this node's channel status to the fleet
report_channel_status() {
  curl -sf --max-time 2 -X POST "$DAEMON_URL/fleet/status" \
    -H "Content-Type: application/json" \
    -d "{\"node\":\"${NODE_ID}\",\"status\":\"online\",\"ts\":$(date +%s),\"event\":\"session-start\"}" 2>/dev/null || true
}

# Send offline notification to fleet
notify_going_offline() {
  curl -sf --max-time 2 -X POST "$DAEMON_URL/fleet/status" \
    -H "Content-Type: application/json" \
    -d "{\"node\":\"${NODE_ID}\",\"status\":\"offline\",\"ts\":$(date +%s),\"event\":\"stop\"}" 2>/dev/null || true
}

# Archive unprocessed inbox messages (for stop hook)
archive_unprocessed_inbox() {
  mkdir -p "$INBOX_ARCHIVE"
  # Archive seed file if still present
  if [ -f "$SEED_FILE" ] && [ -s "$SEED_FILE" ]; then
    cp "$SEED_FILE" "${INBOX_ARCHIVE}/unprocessed-seed-$(date +%s).md"
    echo "[FLEET] Archived unprocessed seed file"
  fi
  # Archive any daemon inbox messages
  local inbox_dump
  inbox_dump=$(curl -sf --max-time 2 "$DAEMON_URL/inbox?archive=true" 2>/dev/null)
  if [ -n "$inbox_dump" ]; then
    echo "$inbox_dump" > "${INBOX_ARCHIVE}/daemon-inbox-$(date +%s).json"
  fi
}

# Flush outbox messages to daemon
flush_outbox() {
  if [ -d "$OUTBOX" ] && [ "$(ls -A "$OUTBOX" 2>/dev/null)" ]; then
    local flush_count
    flush_count=$(ls -1 "$OUTBOX" | wc -l | tr -d ' ')
    echo "[FLEET] Flushing ${flush_count} unsent message(s)..."
    for msg in "$OUTBOX"/*; do
      TARGET=$(basename "$msg" | sed 's/\.md$//' | tr -cd 'a-zA-Z0-9_-')
      curl -sf --max-time 3 -X POST "$DAEMON_URL/message" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"seed\",\"target\":\"${TARGET}\",\"payload\":$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1]).read()))" "$msg")}" 2>/dev/null \
        && rm -f "$msg"
    done
  fi
}

# Check rate limit for fleet context injection (once per 60s)
should_inject_fleet_context() {
  local now last_inject elapsed
  now=$(date +%s)
  last_inject=0
  if [ -f "$LAST_INJECT_FILE" ]; then
    last_inject=$(cat "$LAST_INJECT_FILE" 2>/dev/null || echo 0)
  fi
  elapsed=$(( now - last_inject ))
  if [ "$elapsed" -ge 60 ]; then
    echo "$now" > "$LAST_INJECT_FILE"
    return 0
  fi
  return 1
}

# Check for fleet messages arrived since last prompt
check_fleet_messages_since_last() {
  if ! should_inject_fleet_context; then
    return 1
  fi
  local msgs
  msgs=$(curl -sf --max-time 2 "$DAEMON_URL/inbox?since=last" 2>/dev/null)
  local msg_count
  msg_count=$(echo "$msgs" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    messages = data.get('messages', [])
    if messages:
        print(len(messages))
        for m in messages[:5]:
            src = m.get('from', m.get('source', '?'))
            preview = str(m.get('subject', m.get('preview', m.get('payload', ''))))[:80]
            print(f'  [{src}] {preview}')
    else:
        print('0')
except:
    print('0')
" 2>/dev/null)

  if [ -n "$msg_count" ] && [ "$(echo "$msg_count" | head -1)" != "0" ]; then
    echo "[FLEET] New messages since last check:"
    echo "$msg_count" | tail -n +2
    return 0
  fi
  return 1
}

# Suggest productive tasks when idle and fleet has pending work
suggest_fleet_tasks() {
  local pending
  pending=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/pending" 2>/dev/null)
  if [ -n "$pending" ]; then
    local task_count
    task_count=$(echo "$pending" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    tasks = data.get('tasks', data.get('pending', []))
    if tasks:
        print(len(tasks))
        for t in tasks[:3]:
            desc = str(t.get('description', t.get('task', t.get('summary', '?'))))[:100]
            print(f'  - {desc}')
except:
    print('0')
" 2>/dev/null)
    if [ -n "$task_count" ] && [ "$(echo "$task_count" | head -1)" != "0" ]; then
      echo "[FLEET] Pending fleet work available:"
      echo "$task_count" | tail -n +2
      echo "[FLEET] Consider picking up a pending task"
      return 0
    fi
  fi
  return 1
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

<<<<<<< Updated upstream
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
=======
    # NEW: Check peer health, spawn healing if degraded
    check_peer_health_and_heal

    # NEW: Report this node's channel status to fleet
    report_channel_status
    ;;

  prompt-submit)
    # Existing: check seed inbox and worker results
    check_seed_inbox
    check_worker_results

    # NEW: If fleet messages arrived since last check, prepend summary (rate limited: 60s)
    check_fleet_messages_since_last
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream
    if [ "$has_messages" = true ]; then
      output_json
      exit 2  # asyncRewake — wake the session
    fi

=======
    # NEW: Check daemon inbox for pending messages
    local_inbox=$(curl -sf --max-time 2 "$DAEMON_URL/inbox" 2>/dev/null)
    local_count=$(echo "$local_inbox" | python3 -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo 0)
    if [ "$local_count" != "0" ] && [ -n "$local_count" ]; then
      echo "[FLEET] ${local_count} daemon message(s) waiting — injecting via rewake"
      exit 0  # rewake to process
    fi

    # NEW: If idle > 5min and fleet has pending work, suggest tasks
    suggest_fleet_tasks && exit 0

>>>>>>> Stashed changes
    # Nothing pending — stay idle
    exit 0
    ;;

  stop)
<<<<<<< Updated upstream
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
=======
    # NEW: Flush any pending outbox messages before session ends
    flush_outbox

    # NEW: Send going-offline notification to fleet
    notify_going_offline

    # NEW: Archive any unprocessed inbox messages
    archive_unprocessed_inbox

    echo "[FLEET] Session ending on ${NODE_ID}"
    ;;

  *)
    echo "[FLEET] Unknown hook type: $HOOK_TYPE"
    # Don't crash — exit 0 per invariant
    exit 0
>>>>>>> Stashed changes
    ;;
esac

exit 0
