#!/usr/bin/env bash
# Multi-Fleet Full Capability Demo
# Shows fleet health, probes, rebuttal, arbiter, evidence, and dashboard.
#
# Usage: bash multi-fleet/scripts/demo-multifleet.sh [--theatrical]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"
THEATRICAL=false
[[ "${1:-}" == "--theatrical" ]] && THEATRICAL=true

DAEMON="http://127.0.0.1:8855"
WEB="http://127.0.0.1:8856"

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'; WHITE='\033[1;37m'

pause() { [[ "$THEATRICAL" == true ]] && sleep "${1:-1.5}" || sleep 0.3; }
section() {
    echo ""
    echo -e "${DIM}────────────────────────────────────────────────────────────${NC}"
    echo -e "  ${BOLD}${CYAN}[$1]${NC} ${WHITE}$2${NC}"
    echo -e "${DIM}────────────────────────────────────────────────────────────${NC}"
    pause 0.5
}
timestamp() { date "+%H:%M:%S"; }

# ══════════════════════════════════════════════════════════════
# BANNER
# ══════════════════════════════════════════════════════════════
clear 2>/dev/null || true
echo ""
echo -e "${CYAN}"
cat << 'BANNER'
    __  ___      ____  _       ________             __
   /  |/  /_  __/ / /_(_)     / ____/ /__  ___  ___/ /_
  / /|_/ / / / / / __/ /_____/ /_  / / _ \/ _ \/ __/ __/
 / /  / / /_/ / / /_/ /_____/ __/ / /  __/  __/ /_/ /_
/_/  /_/\__,_/_/\__/_/     /_/   /_/\___/\___/\__/\__/

      C A P A B I L I T Y   D E M O
BANNER
echo -e "${NC}"
echo -e "  ${DIM}Fleet coordination. AI debate. Human oversight. Verified decisions.${NC}"
echo ""
pause

# ══════════════════════════════════════════════════════════════
# STEP 1: Fleet Health (Fleet Doctor)
# ══════════════════════════════════════════════════════════════
section "1/6" "Fleet Health — discovering nodes and checking vitals"
echo -e "  ${DIM}> Every session starts by checking the fleet.${NC}"
echo -e "  ${DIM}> Nodes that are down get self-healed automatically.${NC}"
pause

if curl -sf --connect-timeout 3 "$DAEMON/health" >/dev/null 2>&1; then
    HEALTH=$(curl -sf "$DAEMON/health")
    echo -e "  ${GREEN}LOCAL DAEMON${NC}  $(echo "$HEALTH" | python3 -c "import sys,json; h=json.load(sys.stdin); print(f'status={h.get(\"status\",\"?\")}, peers={h.get(\"peers\",\"?\")}')" 2>/dev/null || echo "running")"
else
    echo -e "  ${YELLOW}LOCAL DAEMON${NC}  offline (demo continues with simulated data)"
fi

# Check each node from config
if [[ -f "$CONFIG" ]]; then
    for node in $(python3 -c "import json; [print(n) for n in json.load(open('$CONFIG')).get('nodes',{})]" 2>/dev/null); do
        ip=$(python3 -c "import json; print(json.load(open('$CONFIG'))['nodes']['$node']['host'])" 2>/dev/null || echo "")
        if [[ -n "$ip" ]] && curl -sf --connect-timeout 2 "http://$ip:8855/health" >/dev/null 2>&1; then
            echo -e "  ${GREEN}$node${NC}  ($ip) reachable"
        else
            echo -e "  ${YELLOW}$node${NC}  ($ip) unreachable — would trigger self-heal"
        fi
    done
else
    echo -e "  ${DIM}  (no config found — showing simulated fleet)${NC}"
    echo -e "  ${GREEN}mac1${NC}  192.168.1.100  reachable"
    echo -e "  ${GREEN}mac2${NC}  192.168.1.101  reachable"
    echo -e "  ${YELLOW}mac3${NC}  192.168.1.102  self-healing..."
fi
pause

# ══════════════════════════════════════════════════════════════
# STEP 2: Probe Scan
# ══════════════════════════════════════════════════════════════
section "2/6" "Probe Scan — analyzing codebase for issues"
echo -e "  ${DIM}> Probes scan for drift, stale locks, config mismatches.${NC}"
echo -e "  ${DIM}> Each finding gets severity + evidence.${NC}"
pause

echo -e "  ${MAGENTA}PROBE${NC}  config_drift ........... ${GREEN}PASS${NC}  configs match across nodes"
pause 0.3
echo -e "  ${MAGENTA}PROBE${NC}  stale_locks ............ ${GREEN}PASS${NC}  no GPU locks >60s"
pause 0.3
echo -e "  ${MAGENTA}PROBE${NC}  version_skew ........... ${YELLOW}WARN${NC}  mac3 is 1 commit behind"
pause 0.3
echo -e "  ${MAGENTA}PROBE${NC}  memory_bridge .......... ${GREEN}PASS${NC}  all nodes share context"
pause 0.3
echo -e "  ${MAGENTA}PROBE${NC}  evidence_integrity ..... ${GREEN}PASS${NC}  hash chain valid"
echo ""
echo -e "  ${DIM}Result: 4 pass, 1 warning. Fleet is healthy.${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 3: Rebuttal Session
# ══════════════════════════════════════════════════════════════
section "3/6" "Rebuttal — AI nodes debate the best approach"
TASK="Add retry logic with exponential backoff to the API client"
echo -e "  ${DIM}> Task: ${TASK}${NC}"
echo -e "  ${DIM}> Each node proposes independently, then cross-examines.${NC}"
pause

echo -e "  ${CYAN}[TASK BRIEF]${NC}  Broadcasting to fleet..."
pause 0.5
echo -e "  ${GREEN}mac1${NC}  Verdict: Use tenacity library with jittered backoff"
echo -e "         ${DIM}Confidence: 0.85 | Evidence: 3 prior implementations found${NC}"
pause 0.5
echo -e "  ${GREEN}mac2${NC}  Verdict: Custom retry with circuit breaker pattern"
echo -e "         ${DIM}Confidence: 0.78 | Evidence: matches existing error handling style${NC}"
pause 0.5
echo -e "  ${GREEN}mac3${NC}  Critique: mac1 adds dependency; mac2 reinvents wheel"
echo -e "         ${DIM}Recommends: stdlib urllib3.Retry — zero new deps${NC}"
pause

echo ""
echo -e "  ${BOLD}${MAGENTA}[CHIEF DECISION]${NC}  Synthesizing..."
pause 0.8
echo -e "  ${WHITE}Decision: Use urllib3.Retry (mac3's approach)${NC}"
echo -e "  ${DIM}Rationale: Zero new deps, proven pattern, 2/3 nodes agree on simplicity${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 4: Human Arbiter View
# ══════════════════════════════════════════════════════════════
section "4/6" "Human Arbiter — you approve or override"
echo -e "  ${DIM}> AI decides, humans validate. Every decision waitable.${NC}"
pause

echo -e "  ${BOLD}${WHITE}ARBITER VIEW${NC}"
echo -e "  ┌─────────────────────────────────────────────────────┐"
echo -e "  │  Decision: Use urllib3.Retry for API client         │"
echo -e "  │  Confidence: 0.82 (weighted)                        │"
echo -e "  │  Nodes agreed: 2/3                                  │"
echo -e "  │  Risk: LOW — no new dependencies                    │"
echo -e "  │                                                     │"
echo -e "  │  ${GREEN}[APPROVE]${NC}  ${YELLOW}[MODIFY]${NC}  ${RED}[REJECT]${NC}                       │"
echo -e "  └─────────────────────────────────────────────────────┘"
pause 0.8
echo -e "  ${GREEN}>>> Human approved. Decision locked to evidence ledger.${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 5: Evidence Trail
# ══════════════════════════════════════════════════════════════
section "5/6" "Evidence Ledger — hash-chained audit trail"
echo -e "  ${DIM}> Every decision is cryptographically chained.${NC}"
echo -e "  ${DIM}> Tamper-proof. Auditable. Reproducible.${NC}"
pause

HASH1=$(echo -n "genesis" | shasum -a 256 | cut -c1-12)
HASH2=$(echo -n "${HASH1}:task_brief" | shasum -a 256 | cut -c1-12)
HASH3=$(echo -n "${HASH2}:verdicts" | shasum -a 256 | cut -c1-12)
HASH4=$(echo -n "${HASH3}:chief_decision" | shasum -a 256 | cut -c1-12)
HASH5=$(echo -n "${HASH4}:human_approved" | shasum -a 256 | cut -c1-12)

echo -e "  ${DIM}#0${NC}  ${HASH1}  ${DIM}genesis${NC}"
echo -e "  ${DIM}#1${NC}  ${HASH2}  task_brief: ${TASK:0:40}..."
echo -e "  ${DIM}#2${NC}  ${HASH3}  verdicts: 3 nodes responded"
echo -e "  ${DIM}#3${NC}  ${HASH4}  chief_decision: urllib3.Retry"
echo -e "  ${DIM}#4${NC}  ${HASH5}  ${GREEN}human_approved${NC}: arbiter confirmed"
echo ""
echo -e "  ${DIM}Chain integrity: ${GREEN}VALID${NC} — all hashes verify${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 6: Dashboard (5-Zone)
# ══════════════════════════════════════════════════════════════
section "6/6" "Fleet Dashboard — live 5-zone operational view"
echo -e "  ${DIM}> Real-time fleet status across all dimensions.${NC}"
pause

echo ""
echo -e "  ${BOLD}${WHITE}FLEET DASHBOARD${NC}  $(timestamp)"
echo -e "  ┌──────────────┬──────────────┬──────────────┐"
echo -e "  │ ${CYAN}NODES${NC}        │ ${CYAN}TASKS${NC}        │ ${CYAN}HEALTH${NC}       │"
echo -e "  │ 3 online     │ 1 active     │ ${GREEN}98% uptime${NC}   │"
echo -e "  │ 0 degraded   │ 12 completed │ 1 warning    │"
echo -e "  ├──────────────┼──────────────┼──────────────┤"
echo -e "  │ ${CYAN}EVIDENCE${NC}     │ ${CYAN}MEMORY${NC}       │              │"
echo -e "  │ 47 decisions │ 1.2k entries │              │"
echo -e "  │ chain: valid │ shared: yes  │              │"
echo -e "  └──────────────┴──────────────┴──────────────┘"
pause

# ══════════════════════════════════════════════════════════════
# FINALE
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${DIM}────────────────────────────────────────────────────────────${NC}"
echo ""
echo -e "  ${BOLD}${GREEN}Demo complete.${NC}"
echo ""
echo -e "  ${WHITE}What you just saw:${NC}"
echo -e "  ${DIM}1.${NC} Fleet health — automatic node discovery and self-healing"
echo -e "  ${DIM}2.${NC} Probe scan — continuous codebase analysis"
echo -e "  ${DIM}3.${NC} AI rebuttal — multi-node debate with evidence"
echo -e "  ${DIM}4.${NC} Human arbiter — you stay in control"
echo -e "  ${DIM}5.${NC} Evidence ledger — cryptographic decision trail"
echo -e "  ${DIM}6.${NC} Fleet dashboard — full operational awareness"
echo ""
echo -e "  ${CYAN}https://multi-fleet.dev${NC}"
echo -e "  ${DIM}Your AI coding team gets smarter every day.${NC}"
echo ""
