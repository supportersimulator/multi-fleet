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
}

# --- Hook handlers ---

case "$HOOK_TYPE" in
  session-start)
    ensure_api_keys
    check_daemon_health
    check_daemon_inbox
    check_seed_inbox
    check_worker_results

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
