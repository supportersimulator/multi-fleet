#!/bin/bash
# Pre-demo checklist — verifies entire fleet is ready for competitive branch race.
# Reads node IPs from .multifleet/config.json (no hardcoded addresses).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
PASS="${GREEN}PASS${NC}"; FAIL="${RED}FAIL${NC}"
failures=()

check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        printf "  ${PASS}  %s\n" "$label"
    else
        printf "  ${FAIL}  %s\n" "$label"
        failures+=("$label")
    fi
}

ip_for() { python3 -c "import json; print(json.load(open('$CONFIG'))['nodes']['$1']['host'])" 2>/dev/null; }

echo ""
printf "${BOLD}${CYAN}══════════════════════════════════════${NC}\n"
printf "${BOLD}${CYAN}  MULTI-FLEET PRE-DEMO CHECKLIST${NC}\n"
printf "${BOLD}${CYAN}══════════════════════════════════════${NC}\n\n"

# --- 1-3: Daemon health per node ---
printf "${BOLD}[1/8] Fleet Daemon Health${NC}\n"
for node in mac1 mac2 mac3; do
    ip=$(ip_for "$node")
    if [ -z "$ip" ]; then
        printf "  ${FAIL}  %s daemon (IP not found in config)\n" "$node"
        failures+=("$node daemon")
    else
        check "$node daemon ($ip)" curl -sf --connect-timeout 3 "http://$ip:8855/health"
    fi
done

# --- 4: NATS server ---
printf "\n${BOLD}[2/8] NATS Server${NC}\n"
check "NATS port 4222 listening" bash -c "nc -z 127.0.0.1 4222 2>/dev/null || lsof -i :4222 -sTCP:LISTEN >/dev/null 2>&1"

# --- 5: Git status ---
printf "\n${BOLD}[3/8] Git Status${NC}\n"
cd "$REPO_ROOT"
check "On main branch" bash -c '[ "$(git branch --show-current)" = "main" ]'
check "Working tree clean" git diff --quiet HEAD

# --- 6: Fleet doctor ---
printf "\n${BOLD}[4/8] Fleet Doctor${NC}\n"
check "Fleet doctor" bash -c "cd '$REPO_ROOT' && MULTIFLEET_NODE_ID=mac1 PYTHONPATH=multi-fleet python3 -m multifleet.doctor"

# --- 7: Superset bridge ---
printf "\n${BOLD}[5/8] Superset Bridge${NC}\n"
check "Superset bridge importable" bash -c "cd '$REPO_ROOT' && PYTHONPATH=multi-fleet python3 -c \"from multifleet.superset_bridge import SupersetBridge; b=SupersetBridge(); print(f'available={b.is_available()}')\""

# --- 8: NATS ping to each node ---
printf "\n${BOLD}[6/8] NATS Messaging${NC}\n"
for node in mac1 mac2 mac3; do
    check "NATS ping → $node" bash -c "cd '$REPO_ROOT' && PYTHONPATH=multi-fleet python3 -c \"
import nats, asyncio, json
async def ping():
    nc = await nats.connect('nats://127.0.0.1:4222')
    await nc.publish('fleet.$node.inbox', json.dumps({'type':'ping','from':'pre-demo'}).encode())
    await nc.flush()
    await nc.close()
asyncio.run(ping())
\""
done

# --- 9: Test suite ---
printf "\n${BOLD}[7/8] Test Suite${NC}\n"
test_output=$(cd "$REPO_ROOT" && python3 -m pytest multi-fleet/tests/ -q --tb=no 2>&1) || true
test_last=$(echo "$test_output" | tail -1)
if echo "$test_last" | grep -q "failed"; then
    printf "  ${FAIL}  Tests: %s\n" "$test_last"
    failures+=("Test suite: $test_last")
else
    printf "  ${PASS}  Tests: %s\n" "$test_last"
fi

# --- 10: Summary ---
printf "\n${BOLD}${CYAN}══════════════════════════════════════${NC}\n"
if [ ${#failures[@]} -eq 0 ]; then
    printf "${BOLD}${GREEN}  READY FOR DEMO${NC}\n"
else
    printf "${BOLD}${RED}  NOT READY — %d failure(s):${NC}\n" "${#failures[@]}"
    for f in "${failures[@]}"; do
        printf "    ${RED}• %s${NC}\n" "$f"
    done
fi
printf "${BOLD}${CYAN}══════════════════════════════════════${NC}\n\n"

exit ${#failures[@]}
