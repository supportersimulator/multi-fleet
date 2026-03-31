#!/usr/bin/env bash
# Multi-Fleet hook runner
# Checks fleet inbox on session start and injects messages as context

HOOK_TYPE="${1:-session-start}"
DAEMON_URL="http://127.0.0.1:8855"

case "$HOOK_TYPE" in
  session-start)
    # Check if daemon is running
    STATUS=$(curl -sf --max-time 2 "$DAEMON_URL/health" 2>/dev/null)
    if [ -z "$STATUS" ]; then
      echo "Multi-Fleet daemon not running on port 8855. Start with: python3 tools/fleet_nerve_daemon.py"
      exit 0
    fi

    # Check for pending messages
    INBOX=$(curl -sf --max-time 2 "$DAEMON_URL/inbox" 2>/dev/null)
    COUNT=$(echo "$INBOX" | python3 -c "import sys,json;print(json.load(sys.stdin).get('count',0))" 2>/dev/null)

    if [ "$COUNT" != "0" ] && [ -n "$COUNT" ]; then
      echo "[FLEET: $COUNT pending message(s)]"
    fi

    # Show fleet status
    FLEET=$(curl -sf --max-time 2 "$DAEMON_URL/fleet/live" 2>/dev/null)
    if [ -n "$FLEET" ]; then
      echo "$FLEET" | python3 -c "import sys,json;print(json.load(sys.stdin).get('fleetStatus',''))" 2>/dev/null
    fi
    ;;
esac
