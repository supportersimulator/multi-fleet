#!/usr/bin/env bash
# Multi-Fleet hook runner — self-healing + communication protocols
# Activates automatically on plugin install. Zero config beyond MULTIFLEET_NODE_ID.
# Gracefully degrades if daemon not running.

set -euo pipefail
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
OUTBOX="/tmp/fleet-outbox-${NODE_ID}"
DAEMON_URL="http://127.0.0.1:8855"
REPAIR_PID_FILE="/tmp/fleet-repair-agent.pid"
REPO_ROOT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
FLEET_CONFIG="${REPO_ROOT}/.multifleet/config.json"
FLEET_MSG_DIR="${REPO_ROOT}/.fleet-messages"

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
OUTBOX="/tmp/fleet-outbox-${NODE_ID}"
FLEET_STATE="/tmp/fleet-state-${NODE_ID}"
LAST_INJECT_FILE="/tmp/fleet-last-inject-${NODE_ID}"
INBOX_ARCHIVE="/tmp/fleet-inbox-archive-${NODE_ID}"

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

  # Priority 1: Session-specific seed file (per-session targeting)
  if [ -n "$SESSION_SEED_FILE" ]; then
    if _read_and_archive_seed "$SESSION_SEED_FILE" "session ${_SAFE_SID:-unknown}"; then
      found=0
    fi
  fi

  # Priority 2: Node-level seed file (legacy fallback / broadcasts)
  if _read_and_archive_seed "$SEED_FILE" "node ${NODE_ID}"; then
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
  # Never logs key values. Startup-only refresh (3-surgeon consensus 2026-04-05).
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
  fi
}

check_channel_health() {
  # Quick probe of P1 (NATS), P2 (HTTP), P5 (SSH) channels — max 5s total
  local p1="SKIP" p2="SKIP" p5="SKIP"
  local broken=""

  # P2 HTTP: daemon health already tells us this — re-use the result
  local daemon_status
  daemon_status=$(curl -sf --max-time 2 "$DAEMON_URL/health" 2>/dev/null)
  if [ -n "$daemon_status" ]; then
    p2="OK"
  else
    p2="FAIL"
    broken="${broken} P2_http"
  fi

  # P1 NATS: ask daemon for NATS connection status
  if [ "$p2" = "OK" ]; then
    local nats_status
    nats_status=$(curl -sf --max-time 2 "$DAEMON_URL/health" 2>/dev/null \
      | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('nats','unknown'))" 2>/dev/null)
    if [ "$nats_status" = "connected" ] || [ "$nats_status" = "ok" ]; then
      p1="OK"
    else
      p1="FAIL"
      broken="${broken} P1_nats"
    fi
  else
    # Can't check NATS without daemon
    p1="FAIL"
    broken="${broken} P1_nats"
  fi

  # P5 SSH: quick probe to peers from config (max 2s per peer, background)
  if [ -f "$FLEET_CONFIG" ]; then
    local peers
    peers=$(python3 -c "
import json,sys
try:
    cfg=json.load(open('$FLEET_CONFIG'))
    for t in cfg.get('ssh_tunnels',{}).get('targets',[]):
        h=t.get('host','')
        u=t.get('user','')
        p=t.get('ssh_port',22)
        if h and h != 'PEER_HOSTNAME_OR_IP':
            print(f'{u}@{h}:{p}' if u else f'{h}:{p}')
except: pass
" 2>/dev/null)
    if [ -n "$peers" ]; then
      local ssh_ok=0
      while IFS= read -r peer; do
        local host port user_host
        host="${peer##*@}"
        port="${host##*:}"
        host="${host%%:*}"
        user_host="${peer%%:*}"
        if ssh -o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=no \
             -p "$port" "$user_host" "echo ok" &>/dev/null; then
          ssh_ok=1
          break
        fi
      done <<< "$peers"
      if [ "$ssh_ok" = "1" ]; then
        p5="OK"
      else
        p5="FAIL"
        broken="${broken} P5_ssh"
      fi
    fi
  fi

  echo "[FLEET] Channels: P1=${p1} P2=${p2} P5=${p5}"

  # Export for use by other functions
  FLEET_CHANNELS_BROKEN="$(echo "$broken" | xargs)"
  if [ -z "$FLEET_CHANNELS_BROKEN" ]; then
    FLEET_CHANNELS_OK=1
  else
    FLEET_CHANNELS_OK=0
  fi
}

maybe_spawn_repair_agent() {
  # Only spawn if P1 or P2 is broken and no repair agent already running
  [ "$FLEET_CHANNELS_OK" = "1" ] && return 0

  # Check for P1/P2 specifically — P5 alone doesn't warrant auto-repair
  local needs_repair=0
  for ch in $FLEET_CHANNELS_BROKEN; do
    case "$ch" in
      P1_nats|P2_http) needs_repair=1 ;;
    esac
  done
  [ "$needs_repair" = "0" ] && return 0

  # Check if repair agent already running
  if [ -f "$REPAIR_PID_FILE" ]; then
    local old_pid
    old_pid=$(cat "$REPAIR_PID_FILE" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
      echo "[FLEET] Repair agent already running (PID ${old_pid})"
      return 0
    fi
    # Stale PID file — remove it
    rm -f "$REPAIR_PID_FILE"
  fi

  echo "[FLEET] Spawning repair agent for broken channels: ${FLEET_CHANNELS_BROKEN}"
  claude -p "Fleet channel repair: broken channels [${FLEET_CHANNELS_BROKEN}] on node ${NODE_ID}. Diagnose and fix. Check NATS server status, daemon config at ${FLEET_CONFIG}, network connectivity. Restart services as needed. Report to fleet when fixed." </dev/null &>/dev/null &
  local repair_pid=$!
  echo "$repair_pid" > "$REPAIR_PID_FILE"
  echo "[FLEET] Repair agent spawned (PID ${repair_pid})"
}

check_fleet_inbox_deep() {
  # Fetch latest fleet messages from git and report unprocessed count
  if [ -d "$REPO_ROOT/.git" ] || [ -f "$REPO_ROOT/.git" ]; then
    git -C "$REPO_ROOT" fetch origin main --quiet 2>/dev/null &
    local fetch_pid=$!

    # Check for unprocessed messages while fetch runs
    local msg_count=0
    local subjects=""
    if [ -d "$FLEET_MSG_DIR" ]; then
      # Check all node directories for unprocessed messages
      for node_dir in "$FLEET_MSG_DIR"/*/; do
        [ -d "$node_dir" ] || continue
        for msg in "$node_dir"*.md "$node_dir"*.json; do
          [ -f "$msg" ] || continue
          msg_count=$((msg_count + 1))
          # Extract subject from first line or filename
          local subj
          subj=$(head -1 "$msg" 2>/dev/null | sed 's/^#* *//' | cut -c1-60)
          [ -z "$subj" ] && subj="$(basename "$msg")"
          subjects="${subjects}\n  - ${subj}"
        done
      done
    fi

    # Wait for fetch (max 3s, then move on)
    local waited=0
    while kill -0 "$fetch_pid" 2>/dev/null && [ "$waited" -lt 3 ]; do
      sleep 1
      waited=$((waited + 1))
    done
    # Kill fetch if still running — don't block the hook
    kill "$fetch_pid" 2>/dev/null
    wait "$fetch_pid" 2>/dev/null

    if [ "$msg_count" -gt 0 ]; then
      echo "[FLEET INBOX] ${msg_count} unprocessed message(s):$(echo -e "$subjects")"
    fi
  fi
}

maybe_seed_idle_repair() {
  # If this is the only active session and channels are broken,
  # write a seed file encouraging proactive repair
  [ "$FLEET_CHANNELS_OK" = "1" ] && return 0

  # Check if other sessions are active (via daemon fleet/live endpoint)
  local live_count=0
  local live_data
  live_data=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null)
  if [ -n "$live_data" ]; then
    live_count=$(echo "$live_data" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    nodes=d.get('nodes',[])
    print(len([n for n in nodes if n.get('status')=='online']))
except: print(0)
" 2>/dev/null)
  fi

  # Only seed if we appear to be the sole active node
  if [ "$live_count" -le 1 ]; then
    local seed_path="/tmp/fleet-idle-repair-seed-${NODE_ID}.md"
    cat > "$seed_path" <<SEED_EOF
# Fleet Idle Repair Opportunity

**Node**: ${NODE_ID}
**Broken channels**: ${FLEET_CHANNELS_BROKEN}
**Timestamp**: $(date -u +%Y-%m-%dT%H:%M:%SZ)

This node is the only active session. Broken fleet channels detected.
Proactive repair recommended before other nodes come online.

## Suggested actions
$(for ch in $FLEET_CHANNELS_BROKEN; do
  case "$ch" in
    P1_nats) echo "- **P1 NATS**: Check nats-server process, verify config in ${FLEET_CONFIG}" ;;
    P2_http) echo "- **P2 HTTP**: Fleet daemon not responding on port 8855, check process/logs" ;;
    P5_ssh)  echo "- **P5 SSH**: SSH connectivity to peers failed, check keys/network" ;;
  esac
done)
SEED_EOF
    echo "[FLEET] Idle repair seed written to ${seed_path}"
  fi
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

    # Channel health check (P1/P2/P5, max 5s)
    check_channel_health

    # Auto-spawn repair agent if P1/P2 broken
    maybe_spawn_repair_agent

    # Deep inbox check — fetch latest git messages
    check_fleet_inbox_deep

    # Idle protocol — seed repair if sole active node
    maybe_seed_idle_repair

    # Show fleet status
    FLEET=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null)
    if [ -n "$FLEET" ]; then
      echo "$FLEET" | python3 -c "import sys,json;s=json.load(sys.stdin).get('fleetStatus','');print(s) if s else None" 2>/dev/null
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

    # NEW: Check daemon inbox for pending messages
    local_inbox=$(curl -sf --max-time 2 "$DAEMON_URL/inbox" 2>/dev/null)
    local_count=$(echo "$local_inbox" | python3 -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo 0)
    if [ "$local_count" != "0" ] && [ -n "$local_count" ]; then
      echo "[FLEET] ${local_count} daemon message(s) waiting — injecting via rewake"
      exit 0  # rewake to process
    fi

    # NEW: If idle > 5min and fleet has pending work, suggest tasks
    suggest_fleet_tasks && exit 0

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
    ;;
esac

exit 0
