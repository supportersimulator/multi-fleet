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
REPAIR_PID_FILE="/tmp/fleet-repair-agent.pid"
REPO_ROOT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
FLEET_CONFIG="${REPO_ROOT}/.multifleet/config.json"
FLEET_MSG_DIR="${REPO_ROOT}/.fleet-messages"

# Channel health state (set by check_channel_health)
FLEET_CHANNELS_BROKEN=""
FLEET_CHANNELS_OK=1

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
    # Load API keys + fleet env before starting daemon
    if [ -f "$HOME/.fleet-nerve/env" ]; then
      export $(grep -v '^#' "$HOME/.fleet-nerve/env" | grep '=' | xargs) 2>/dev/null
    fi
    # Auto-detect NATS URL (SSH tunnel or direct)
    if lsof -i :4222 -sTCP:LISTEN >/dev/null 2>&1; then
      export NATS_URL="nats://127.0.0.1:4222"
    fi
    # Set node ID
    export MULTIFLEET_NODE_ID="${NODE_ID}"
    # Start daemon with full env
    nohup python3 "$daemon_script" serve >/tmp/fleet-daemon-${NODE_ID}.log 2>&1 &
    # Give it a moment to bind + connect NATS
    sleep 2
    if daemon_is_running; then
      emit "[FLEET] Daemon started (node=${NODE_ID}, nats=${NATS_URL:-auto})"
      return 0
    fi
    sleep 1
    if daemon_is_running; then
      emit "[FLEET] Daemon started (slow start)"
      return 0
    fi
  fi

  emit "[FLEET] Daemon not running -- start with: python3 tools/fleet_nerve_nats.py serve"
  return 1
}

validate_seed_file() {
  # Security: validate seed file before reading
  # - Must be a regular file (not symlink to outside /tmp)
  # - Owned by current user (prevent symlink attacks)
  # - Max 50KB
  local file="$1"
  [ -f "$file" ] || return 1
  [ -s "$file" ] || return 1

  # Reject symlinks pointing outside /tmp
  if [ -L "$file" ]; then
    local target
    target=$(readlink -f "$file" 2>/dev/null) || return 1
    case "$target" in
      /tmp/*) ;; # OK
      *) echo "[FLEET] SECURITY: seed file symlink outside /tmp: $file" >&2; return 1 ;;
    esac
  fi

  # Check ownership — must be current user
  local file_owner
  file_owner=$(stat -f '%u' "$file" 2>/dev/null || stat -c '%u' "$file" 2>/dev/null) || return 1
  if [ "$file_owner" != "$(id -u)" ]; then
    echo "[FLEET] SECURITY: seed file not owned by current user: $file" >&2
    return 1
  fi

  # Check size — max 50KB
  local file_size
  file_size=$(stat -f '%z' "$file" 2>/dev/null || stat -c '%s' "$file" 2>/dev/null) || return 1
  if [ "$file_size" -gt 51200 ]; then
    echo "[FLEET] SECURITY: seed file too large (${file_size}B > 50KB): $file" >&2
    return 1
  fi

  return 0
}

sanitize_seed_content() {
  # Strip ANSI escape sequences, null bytes, LLM role markers, and prompt injection patterns
  # from seed file content before it gets injected into Claude sessions via systemMessage.
  #
  # Defense layers:
  # 1. ANSI escapes — prevents terminal injection
  # 2. Null bytes — prevents string truncation attacks
  # 3. LLM role markers — prevents model role confusion (<|system|>, [INST], <<SYS>>, etc.)
  # 4. Prompt injection patterns — blocks override attempts (IGNORE ALL PREVIOUS, etc.)
  #
  # If Python sanitizer is available, use it (comprehensive regex-based). Otherwise,
  # fall back to sed-based stripping of the most critical patterns.
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if python3 -c "from multifleet.sanitize import sanitize_message" 2>/dev/null; then
    PYTHONPATH="$script_dir" python3 -c "
import sys
from multifleet.sanitize import sanitize_message
print(sanitize_message(sys.stdin.read()))
"
  else
    # Fallback: sed-based stripping of ANSI, null bytes, and critical LLM markers
    sed 's/\x1b\[[0-9;]*[a-zA-Z]//g; s/\x1b\][^\x07]*\x07//g; s/\x00//g' \
    | sed 's/<|system|>//gI; s/<|user|>//gI; s/<|assistant|>//gI; s/<|im_start|>//gI; s/<|im_end|>//gI' \
    | sed 's/\[INST\]//gI; s/\[\/INST\]//gI; s/<<SYS>>//gI; s/<\/SYS>>//gI'
  fi
}

check_seed_inbox() {
  local found=1
  # Check file-based seed (with security validation)
  if validate_seed_file "$SEED_FILE"; then
    emit "[FLEET INBOX] Messages for ${NODE_ID}:"
    emit "---"
    emit "$(sanitize_seed_content < "$SEED_FILE")"
    emit "---"
    mkdir -p "$SEED_ARCHIVE"
    mv "$SEED_FILE" "${SEED_ARCHIVE}/fleet-seed-${NODE_ID}-$(date +%s).md"
    found=0
  fi

  # Check git-based inbox (with security validation per file)
  if [ -d "$GIT_INBOX" ] && [ "$(ls -A "$GIT_INBOX" 2>/dev/null)" ]; then
    local count
    count=$(ls -1 "$GIT_INBOX" | wc -l | tr -d ' ')
    emit "[FLEET GIT INBOX] ${count} git message(s):"
    for f in "$GIT_INBOX"/*; do
      # Validate each file before reading
      if validate_seed_file "$f"; then
        emit "  $(basename "$f"): $(head -1 "$f" | sanitize_seed_content)"
      else
        emit "  $(basename "$f"): [SKIPPED — failed validation]"
      fi
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
  # Load keys: Keychain (fleet-nerve svc) -> Keychain (legacy) -> env file -> AWS SM
  # Never logs key values. Startup-only refresh.
  #
  # Key name mapping: generic names for open-source, with backward compatibility
  # for legacy Context_DNA_* names used in existing deployments.
  local needs_refresh=0
  local repo_root
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

  # Generic name -> legacy name mapping for backward compatibility.
  # We export the generic name (FLEET_OPENAI_API_KEY) and also set the legacy
  # name (Context_DNA_OPENAI) so existing code that reads the old name still works.
  local -a GENERIC_NAMES=(FLEET_OPENAI_API_KEY FLEET_DEEPSEEK_API_KEY)
  local -a LEGACY_NAMES=(Context_DNA_OPENAI Context_DNA_Deepseek)

  for i in 0 1; do
    local key_name="${GENERIC_NAMES[$i]}"
    local legacy_name="${LEGACY_NAMES[$i]}"
    local val=""

    # 1. Keychain — fleet-nerve service (canonical, try generic name first)
    val=$(security find-generic-password -s "fleet-nerve" -a "$key_name" -w 2>/dev/null) || true
    if [ -z "$val" ]; then
      # Try legacy name in keychain for backward compatibility
      val=$(security find-generic-password -s "fleet-nerve" -a "$legacy_name" -w 2>/dev/null) || true
    fi
    if [ -n "$val" ]; then
      export "$key_name=$val"
      export "$legacy_name=$val"  # backward compat
      continue
    fi

    # 2. Keychain — legacy (key name as service)
    val=$(security find-generic-password -s "$legacy_name" -a "$USER" -w 2>/dev/null) || true
    if [ -z "$val" ]; then
      val=$(security find-generic-password -s "$legacy_name" -a "atlas" -w 2>/dev/null) || true
    fi
    if [ -n "$val" ]; then
      export "$key_name=$val"
      export "$legacy_name=$val"  # backward compat
      # Migrate to canonical keychain location under generic name
      security delete-generic-password -s "fleet-nerve" -a "$key_name" 2>/dev/null || true
      security add-generic-password -s "fleet-nerve" -a "$key_name" -w "$val" 2>/dev/null || true
      continue
    fi

    # 3. Env file fallback — check both generic and legacy names
    if [ -f "$HOME/.fleet-nerve/env" ]; then
      val=$(grep "^${key_name}=" "$HOME/.fleet-nerve/env" 2>/dev/null | cut -d= -f2-) || true
      if [ -z "$val" ]; then
        val=$(grep "^${legacy_name}=" "$HOME/.fleet-nerve/env" 2>/dev/null | cut -d= -f2-) || true
      fi
      if [ -n "$val" ]; then
        export "$key_name=$val"
        export "$legacy_name=$val"  # backward compat
        # Promote to Keychain under generic name
        security delete-generic-password -s "fleet-nerve" -a "$key_name" 2>/dev/null || true
        security add-generic-password -s "fleet-nerve" -a "$key_name" -w "$val" 2>/dev/null || true
        continue
      fi
    fi

    needs_refresh=1
  done

  if [ "$needs_refresh" = "1" ] && command -v aws &>/dev/null; then
    for i in 0 1; do
      local key_name="${GENERIC_NAMES[$i]}"
      local legacy_name="${LEGACY_NAMES[$i]}"
      local existing
      existing=$(security find-generic-password -s "fleet-nerve" -a "$key_name" -w 2>/dev/null) || true
      [ -n "$existing" ] && continue

      # Try AWS SM with both generic and legacy secret names
      local aws_val=""
      aws_val=$(aws secretsmanager get-secret-value --secret-id "$key_name" --query 'SecretString' --output text 2>/dev/null) || true
      if [ -z "$aws_val" ]; then
        aws_val=$(aws secretsmanager get-secret-value --secret-id "$legacy_name" --query 'SecretString' --output text 2>/dev/null) || true
      fi
      if [ -n "$aws_val" ]; then
        security delete-generic-password -s "fleet-nerve" -a "$key_name" 2>/dev/null || true
        security add-generic-password -s "fleet-nerve" -a "$key_name" -w "$aws_val" 2>/dev/null || true
        export "$key_name=$aws_val"
        export "$legacy_name=$aws_val"  # backward compat
        emit "[FLEET] Refreshed ${key_name} from AWS Secrets Manager → Keychain"
      fi
    done
  fi
}

check_channel_health() {
  # Quick probe of P1 (NATS), P2 (HTTP), P5 (SSH) channels -- max 5s total
  local p1="SKIP" p2="SKIP" p5="SKIP"
  local broken=""

  # P2 HTTP: daemon health
  if daemon_is_running; then
    p2="OK"
  else
    p2="FAIL"
    broken="${broken} P2_http"
  fi

  # P1 NATS: ask daemon for NATS connection status
  if [ "$p2" = "OK" ]; then
    local nats_status
    nats_status=$(curl -sf --max-time 2 "$DAEMON_URL/health" 2>/dev/null \
      | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('nats','unknown'))" 2>/dev/null) || true
    if [ "$nats_status" = "connected" ] || [ "$nats_status" = "ok" ]; then
      p1="OK"
    else
      p1="FAIL"
      broken="${broken} P1_nats"
    fi
  else
    p1="FAIL"
    broken="${broken} P1_nats"
  fi

  # P5 SSH: quick probe to peers from config (max 2s per peer)
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
" 2>/dev/null) || true
    if [ -n "$peers" ]; then
      local ssh_ok=0
      while IFS= read -r peer; do
        local host port user_host
        host="${peer##*@}"
        port="${host##*:}"
        host="${host%%:*}"
        user_host="${peer%%:*}"
        # accept-new: trusts first connection (TOFU), rejects changed keys (MITM defense).
        # Override with FLEET_SSH_STRICT=no if host keys rotate frequently.
        if ssh -o ConnectTimeout=2 -o BatchMode=yes -o "StrictHostKeyChecking=${FLEET_SSH_STRICT:-accept-new}" \
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

  emit "[FLEET] Channels: P1=${p1} P2=${p2} P5=${p5}"

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

  # Check for P1/P2 specifically -- P5 alone doesn't warrant auto-repair
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
    old_pid=$(cat "$REPAIR_PID_FILE" 2>/dev/null) || true
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
      emit "[FLEET] Repair agent already running (PID ${old_pid})"
      return 0
    fi
    rm -f "$REPAIR_PID_FILE"
  fi

  # DISABLED: claude -p repair agent burns Anthropic weekly context limit.
  # Instead: log broken channels + write seed file for next human session.
  # See: 2026-04-15 context-limit audit.
  emit "[FLEET] Broken channels detected: ${FLEET_CHANNELS_BROKEN} — logged for next session (claude -p DISABLED)"
  echo "---
from: ${NODE_ID}
to: all
subject: broken channels detected — ${FLEET_CHANNELS_BROKEN}
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
---
Broken channels on ${NODE_ID}: ${FLEET_CHANNELS_BROKEN}
Auto-repair disabled to protect Anthropic context limit.
Fix manually or wait for next interactive session.
" > "${REPO_ROOT}/.fleet-messages/all/$(date +%Y%m%d-%H%M%S)-${NODE_ID}-broken-channels.md"
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
      for node_dir in "$FLEET_MSG_DIR"/*/; do
        [ -d "$node_dir" ] || continue
        for msg in "$node_dir"*.md "$node_dir"*.json; do
          [ -f "$msg" ] || continue
          msg_count=$((msg_count + 1))
          local subj
          subj=$(head -1 "$msg" 2>/dev/null | sed 's/^#* *//' | cut -c1-60) || true
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
    kill "$fetch_pid" 2>/dev/null || true
    wait "$fetch_pid" 2>/dev/null || true

    if [ "$msg_count" -gt 0 ]; then
      emit "[FLEET INBOX] ${msg_count} unprocessed message(s):$(echo -e "$subjects")"
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
  live_data=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null) || true
  if [ -n "$live_data" ]; then
    live_count=$(echo "$live_data" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    nodes=d.get('nodes',[])
    print(len([n for n in nodes if n.get('status')=='online']))
except: print(0)
" 2>/dev/null) || true
  fi

  # Only seed if we appear to be the sole active node
  if [ "${live_count:-0}" -le 1 ]; then
    local seed_path="/tmp/fleet-idle-repair-seed-${NODE_ID}.md"
    local repair_actions=""
    for ch in $FLEET_CHANNELS_BROKEN; do
      case "$ch" in
        P1_nats) repair_actions="${repair_actions}\n- **P1 NATS**: Check nats-server process, verify config in ${FLEET_CONFIG}" ;;
        P2_http) repair_actions="${repair_actions}\n- **P2 HTTP**: Fleet daemon not responding on port 8855, check process/logs" ;;
        P5_ssh)  repair_actions="${repair_actions}\n- **P5 SSH**: SSH connectivity to peers failed, check keys/network" ;;
      esac
    done
    cat > "$seed_path" <<SEED_EOF
# Fleet Idle Repair Opportunity

**Node**: ${NODE_ID}
**Broken channels**: ${FLEET_CHANNELS_BROKEN}
**Timestamp**: $(date -u +%Y-%m-%dT%H:%M:%SZ)

This node is the only active session. Broken fleet channels detected.
Proactive repair recommended before other nodes come online.

## Suggested actions
$(echo -e "$repair_actions")
SEED_EOF
    emit "[FLEET] Idle repair seed written to ${seed_path}"
  fi
}

# --- Hook handlers ---

case "$HOOK_TYPE" in
  session-start)
    # 1. Start daemon if not running (non-fatal — continue without daemon)
    start_daemon_if_needed || true

    # 2. Ensure API keys available (non-fatal — keys may be unavailable in test)
    ensure_api_keys || true

    # 3. Check inbox for pending messages
    check_seed_inbox || true
    check_daemon_inbox || true
    check_worker_results || true

    # 4. Fleet health summary
    fleet_health_summary || true

    # 5. Channel health check (P1/P2/P5, max 5s)
    check_channel_health || true

    # 6. Auto-spawn repair agent if P1/P2 broken
    maybe_spawn_repair_agent || true

    # 7. Deep inbox check -- fetch latest git messages
    check_fleet_inbox_deep || true

    # 8. Idle protocol -- seed repair if sole active node
    maybe_seed_idle_repair || true

    # 9. Auto-pull if behind origin
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

    # Coordinator hint on first prompt of a session
    if [ "${FLEET_COORDINATOR_ENABLED:-true}" = "true" ]; then
      FLEET_COORD_HINT_FILE="/tmp/fleet-coord-hint-${NODE_ID}"
      if [ ! -f "$FLEET_COORD_HINT_FILE" ]; then
        touch "$FLEET_COORD_HINT_FILE"
        emit "[FLEET] Tip: run fleet_probe or /fleet-check to initialize fleet coordination"
      fi
    fi

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
