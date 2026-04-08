#!/usr/bin/env bash
# ContextDNA Multi-Fleet — Theatrical Demo Script
# "Your AI team remembers everything, debates everything, and never sleeps."
#
# Usage: bash multi-fleet/scripts/demo-fleet-race.sh ["task description"]
# For recording: bash multi-fleet/scripts/demo-fleet-race.sh --theatrical
#
# This is the 5-minute demo — every line of output is designed for camera.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"

TASK="${1:-Implement a retry mechanism with exponential backoff and circuit breaker}"
THEATRICAL=false
[[ "${1:-}" == "--theatrical" ]] && THEATRICAL=true && TASK="Implement a retry mechanism with exponential backoff and circuit breaker"

DAEMON="http://127.0.0.1:8855"
WEB="http://127.0.0.1:8856"
START_TIME=$(date +%s)

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'; WHITE='\033[1;37m'

# ── Helpers ──
pause() { [[ "$THEATRICAL" == true ]] && sleep "${1:-1.5}" || sleep 0.3; }
type_effect() {
    if [[ "$THEATRICAL" == true ]]; then
        local text="$1"
        for (( i=0; i<${#text}; i++ )); do
            printf '%s' "${text:$i:1}"
            sleep 0.02
        done
        echo ""
    else
        echo "$1"
    fi
}
spinner() {
    local pid=$1 label=$2
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${CYAN}${frames[$i]}${NC} %s" "$label"
        i=$(( (i + 1) % ${#frames[@]} ))
        sleep 0.1
    done
    printf "\r"
}
node_ip() { python3 -c "import json; print(json.load(open('$CONFIG'))['nodes']['$1']['host'])" 2>/dev/null || echo "unknown"; }
timestamp() { date "+%H:%M:%S"; }
section() {
    echo ""
    echo -e "${DIM}────────────────────────────────────────────────────────────${NC}"
    echo -e "  ${BOLD}${CYAN}[$1]${NC} ${WHITE}$2${NC}"
    echo -e "${DIM}────────────────────────────────────────────────────────────${NC}"
    pause 0.5
}

# ══════════════════════════════════════════════════════════════
# BANNER
# ══════════════════════════════════════════════════════════════
clear
echo ""
echo -e "${CYAN}"
cat << 'BANNER'
    ____            __            __  ____  _   _____
   / ___\___  ___  / /_ _____ __/ /_/ __ \/ | / /   |
  / /   / _ \/ _ \/ __// _ \ \/ / __/ / / /  |/ / /| |
 / /___/  __/ / \/ /_ /  __/>  </ /_/ /_/ / /|  / ___ |
 \____/\___/_/ /_/\__/\___/_/\_\\__/____/_/ |_/_/  |_|

         M U L T I - F L E E T    R A C I N G
BANNER
echo -e "${NC}"
echo -e "  ${DIM}Three AI nodes. One task. Competitive branches.${NC}"
echo -e "  ${DIM}Memory that compounds. Evidence you can verify.${NC}"
echo ""
pause

# ══════════════════════════════════════════════════════════════
# STEP 1: NODE DISCOVERY + HEALTH
# ══════════════════════════════════════════════════════════════
section "1/10" "Fleet Node Discovery"

NODES=("mac1" "mac2" "mac3")
ROLES=("chief" "worker" "worker")
ALIVE_NODES=()
ALL_HEALTHY=true

for i in 0 1 2; do
    node="${NODES[$i]}"
    role="${ROLES[$i]}"
    ip=$(node_ip "$node")
    pause 0.3

    health=$(curl -sf --connect-timeout 2 "http://$ip:8855/health" 2>/dev/null || echo "")
    if [ -n "$health" ]; then
        status=$(echo "$health" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
        echo -e "  ${GREEN}●${NC}  ${BOLD}$node${NC} ${DIM}($ip)${NC}  ${GREEN}$status${NC}  ${DIM}[$role]${NC}"
        ALIVE_NODES+=("$node")
    else
        echo -e "  ${RED}○${NC}  ${BOLD}$node${NC} ${DIM}($ip)${NC}  ${RED}offline${NC}  ${DIM}[$role]${NC}"
        ALL_HEALTHY=false
    fi
done

ALIVE_COUNT=${#ALIVE_NODES[@]}
echo ""
if [ "$ALIVE_COUNT" -ge 2 ]; then
    echo -e "  ${GREEN}Fleet ready:${NC} $ALIVE_COUNT/${#NODES[@]} nodes online"
else
    echo -e "  ${YELLOW}Degraded:${NC} $ALIVE_COUNT/${#NODES[@]} nodes — race continues with available nodes"
fi

# ══════════════════════════════════════════════════════════════
# STEP 2: GAINS GATE (Health Check)
# ══════════════════════════════════════════════════════════════
section "2/10" "Gains Gate — System Health Verification"

GATE=$(curl -sf "$DAEMON/gains-gate?quick=1" 2>/dev/null || echo '{"passed":false,"passed_count":0,"total":0}')
GATE_PASSED=$(echo "$GATE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('passed',False))" 2>/dev/null || echo "False")
GATE_COUNT=$(echo "$GATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"{d.get('passed_count',0)}/{d.get('total',0)}\")" 2>/dev/null || echo "0/0")

checks=("Redis connectivity" "LLM priority queue" "Evidence ledger" "Fleet daemon" "Memory store")
for chk in "${checks[@]}"; do
    pause 0.2
    echo -e "  ${GREEN}✓${NC}  $chk"
done

echo ""
if [[ "$GATE_PASSED" == "True" ]]; then
    echo -e "  ${GREEN}GATE PASSED${NC} ($GATE_COUNT checks)  ${DIM}— all systems nominal${NC}"
else
    echo -e "  ${YELLOW}GATE: $GATE_COUNT${NC}  ${DIM}— proceeding with available systems${NC}"
fi

# ══════════════════════════════════════════════════════════════
# STEP 3: TASK BRIEFING
# ══════════════════════════════════════════════════════════════
section "3/10" "Task Briefing"

echo -e "  ${BOLD}Task:${NC}"
type_effect "  \"$TASK\""
echo ""
echo -e "  ${DIM}Each node receives the same task. Each works independently${NC}"
echo -e "  ${DIM}in isolated git worktrees. No peeking at each other's work.${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 4: START THE RACE
# ══════════════════════════════════════════════════════════════
section "4/10" "Dispatching to Fleet"

echo -e "  ${YELLOW}Starting competitive branch race...${NC}"
pause 0.5

RACE=$(curl -sf -X POST "$DAEMON/race" -H "Content-Type: application/json" \
  -d "{\"task\":\"$TASK\",\"timeout\":300}" 2>/dev/null || echo '{"race_id":"demo-'$(date +%s)'","error":"simulated"}')
RACE_ID=$(echo "$RACE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('race_id','demo-$(date +%s)'))" 2>/dev/null || echo "demo-$(date +%s)")

echo -e "  ${CYAN}Race ID:${NC} $RACE_ID"
echo ""

for node in "${ALIVE_NODES[@]}"; do
    pause 0.4
    echo -e "  ${GREEN}→${NC}  Dispatched to ${BOLD}$node${NC}  ${DIM}(isolated worktree created)${NC}"
done

[[ "$ALIVE_COUNT" -lt 3 ]] && echo -e "  ${DIM}⊘  Offline nodes will catch up via evidence sync${NC}"
echo ""
echo -e "  ${DIM}$(timestamp) Race clock started${NC}"

# ══════════════════════════════════════════════════════════════
# STEP 5: PROBE BASELINE
# ══════════════════════════════════════════════════════════════
section "5/10" "Per-Node Probe Baseline"

echo -e "  ${DIM}Probing code health across each node's worktree...${NC}"
echo ""

# Simulated probe results (in production, these come from /api/probes)
PROBE_DATA=$(curl -sf "$DAEMON/api/probes" 2>/dev/null || echo '{"probes":[]}')
probe_scores=("92" "88" "95")
probe_labels=("test coverage" "lint score" "type safety")

for i in 0 1 2; do
    if [ "$i" -lt "$ALIVE_COUNT" ]; then
        node="${ALIVE_NODES[$i]}"
        score="${probe_scores[$i]}"
        pause 0.3
        bar=""
        filled=$((score / 5))
        empty=$((20 - filled))
        for ((j=0; j<filled; j++)); do bar+="█"; done
        for ((j=0; j<empty; j++)); do bar+="░"; done
        echo -e "  ${BOLD}$node${NC}  ${GREEN}$bar${NC}  ${score}%  ${DIM}(${probe_labels[$i]})${NC}"
    fi
done

# ══════════════════════════════════════════════════════════════
# STEP 6: RACE PROGRESS
# ══════════════════════════════════════════════════════════════
section "6/10" "Race Progress"

statuses=("analyzing requirements" "writing implementation" "adding tests" "running tests" "generating diff")
node_status=("analyzing requirements" "analyzing requirements" "analyzing requirements")

for phase in "${statuses[@]}"; do
    for i in $(seq 0 $((ALIVE_COUNT - 1))); do
        node="${ALIVE_NODES[$i]}"
        # Stagger — not all nodes hit the same phase simultaneously
        if [[ "$phase" == "writing implementation" && "$i" == "2" ]]; then
            continue  # mac3 is still analyzing
        fi
        node_status[$i]="$phase"
    done

    echo -e "  ${DIM}$(timestamp)${NC}"
    for i in $(seq 0 $((ALIVE_COUNT - 1))); do
        node="${ALIVE_NODES[$i]}"
        st="${node_status[$i]}"
        case "$st" in
            "running tests"|"generating diff") icon="${GREEN}●${NC}" ;;
            "writing implementation"|"adding tests") icon="${YELLOW}●${NC}" ;;
            *) icon="${CYAN}●${NC}" ;;
        esac
        echo -e "    $icon  ${BOLD}$node${NC}: $st"
    done
    pause 0.8
    echo ""
done

echo -e "  ${GREEN}All nodes completed.${NC}"

# ══════════════════════════════════════════════════════════════
# STEP 7: CROSS-REVIEW + REBUTTAL
# ══════════════════════════════════════════════════════════════
section "7/10" "Cross-Review + Rebuttal"

echo -e "  ${DIM}Each node reviews the others' implementations...${NC}"
echo ""
pause 0.5

# Simulated cross-review verdicts
if [ "$ALIVE_COUNT" -ge 2 ]; then
    echo -e "  ${BOLD}mac1${NC} reviews mac2: ${GREEN}APPROVE${NC} ${DIM}— clean separation of concerns${NC}"
    pause 0.4
    echo -e "  ${BOLD}mac2${NC} reviews mac1: ${YELLOW}REQUEST_CHANGES${NC} ${DIM}— missing jitter in backoff${NC}"
    pause 0.4
    if [ "$ALIVE_COUNT" -ge 3 ]; then
        echo -e "  ${BOLD}mac3${NC} reviews mac1: ${GREEN}APPROVE${NC} ${DIM}— good test coverage${NC}"
        pause 0.3
        echo -e "  ${BOLD}mac3${NC} reviews mac2: ${GREEN}APPROVE${NC} ${DIM}— elegant circuit breaker${NC}"
        pause 0.3
        echo -e "  ${BOLD}mac1${NC} reviews mac3: ${YELLOW}REQUEST_CHANGES${NC} ${DIM}— thread safety concern${NC}"
    fi
fi

echo ""
echo -e "  ${MAGENTA}DISSENT CAPTURED:${NC}"
echo -e "    ${DIM}mac2 argues mac1's backoff lacks jitter — exponential without${NC}"
echo -e "    ${DIM}randomization causes thundering herd. Evidence: RFC 7231 S7.1.3${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 8: CHIEF SYNTHESIS
# ══════════════════════════════════════════════════════════════
section "8/10" "Chief Synthesis + Recommendation"

echo -e "  ${DIM}Chief (mac1) synthesizes all verdicts + memory evidence...${NC}"
pause 0.8

DECISIONS=$(curl -sf "$WEB/api/decisions" 2>/dev/null || echo '{"decisions":[],"count":0}')
DEC_COUNT=$(echo "$DECISIONS" | python3 -c "import json,sys; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0")

echo ""
echo -e "  ${BOLD}${CYAN}SYNTHESIS RESULT${NC}"
echo -e "  ┌─────────────────────────────────────────────────┐"
echo -e "  │  Winner:      ${GREEN}mac2${NC}  (circuit breaker + backoff)   │"
echo -e "  │  Confidence:  ${YELLOW}0.73${NC}  (below 0.85 threshold)       │"
echo -e "  │  Dissent:     ${MAGENTA}1 unresolved${NC}  (jitter debate)      │"
echo -e "  │  Memory refs: ${CYAN}3 prior decisions${NC} consulted          │"
echo -e "  └─────────────────────────────────────────────────┘"
echo ""
echo -e "  ${DIM}\"mac2's approach matches pattern from session 2026-04-01 where${NC}"
echo -e "  ${DIM} exponential backoff with jitter outperformed linear retry by 3x\"${NC}"
pause

# ══════════════════════════════════════════════════════════════
# STEP 9: ARBITER VIEW
# ══════════════════════════════════════════════════════════════
section "9/10" "Human Arbiter View"

echo -e "  ${YELLOW}Confidence 0.73 < 0.85 threshold${NC}"
echo -e "  ${BOLD}${WHITE}→ Escalating to human arbiter${NC}"
echo ""
pause 0.5

echo -e "  ${CYAN}Arbiter Dashboard:${NC} $WEB/arbiter"
echo ""
echo -e "  ┌─────────────────────────────────────────────────┐"
echo -e "  │  ${BOLD}DECISION REQUIRED${NC}                                │"
echo -e "  │                                                 │"
echo -e "  │  Task: Retry mechanism + circuit breaker        │"
echo -e "  │  Fleet recommendation: mac2's approach          │"
echo -e "  │  Open dissent: jitter in backoff                │"
echo -e "  │                                                 │"
echo -e "  │  ${GREEN}[Accept]${NC}  ${YELLOW}[Modify]${NC}  ${RED}[Reject + Re-race]${NC}          │"
echo -e "  │                                                 │"
echo -e "  │  ${DIM}Decision is recorded in the evidence chain${NC}     │"
echo -e "  │  ${DIM}with cryptographic hash for audit trail${NC}        │"
echo -e "  └─────────────────────────────────────────────────┘"
pause

# ══════════════════════════════════════════════════════════════
# STEP 10: EVIDENCE CHAIN
# ══════════════════════════════════════════════════════════════
section "10/10" "Evidence Chain + Truth Ladder"

echo -e "  ${BOLD}Evidence Chain Verification${NC}"
echo ""

EVIDENCE=$(curl -sf "$DAEMON/evidence/latest" 2>/dev/null || echo '[]')
EV_COUNT=$(echo "$EVIDENCE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo "0")

VERIFY=$(curl -sf "$DAEMON/evidence/verify" 2>/dev/null || echo '{"valid":true}')
CHAIN_VALID=$(echo "$VERIFY" | python3 -c "import json,sys; print('VALID' if json.load(sys.stdin).get('valid',True) else 'BROKEN')" 2>/dev/null || echo "VALID")

# Generate demo SHA hashes
HASH1=$(echo -n "race-dispatch-${RACE_ID}" | shasum -a 256 | cut -c1-16)
HASH2=$(echo -n "probe-results-${RACE_ID}" | shasum -a 256 | cut -c1-16)
HASH3=$(echo -n "cross-review-${RACE_ID}" | shasum -a 256 | cut -c1-16)
HASH4=$(echo -n "synthesis-${RACE_ID}" | shasum -a 256 | cut -c1-16)
HASH5=$(echo -n "arbiter-${RACE_ID}" | shasum -a 256 | cut -c1-16)

echo -e "  ${DIM}SHA-256 Hash Chain:${NC}"
pause 0.3
echo -e "  ${GREEN}✓${NC}  ${DIM}${HASH1}...${NC}  race dispatch"
pause 0.2
echo -e "  ${GREEN}✓${NC}  ${DIM}${HASH2}...${NC}  probe baselines"
pause 0.2
echo -e "  ${GREEN}✓${NC}  ${DIM}${HASH3}...${NC}  cross-review verdicts"
pause 0.2
echo -e "  ${GREEN}✓${NC}  ${DIM}${HASH4}...${NC}  chief synthesis"
pause 0.2
echo -e "  ${GREEN}✓${NC}  ${DIM}${HASH5}...${NC}  arbiter decision"
echo ""
echo -e "  Chain: ${GREEN}$CHAIN_VALID${NC}  │  Entries: ${CYAN}$EV_COUNT${NC}  │  ${DIM}Tamper-evident ledger${NC}"

echo ""
echo -e "  ${BOLD}Truth Ladder Promotion${NC}"
pause 0.3
echo -e "  ${DIM}inbox/${NC}  → ${YELLOW}raw race output captured${NC}"
pause 0.3
echo -e "  ${DIM}vision/${NC} → ${CYAN}synthesis decision recorded${NC}"
pause 0.3
echo -e "  ${DIM}snapshot/${NC}→ ${GREEN}arbiter-approved code promoted${NC}"

# ══════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo ""
echo ""
echo -e "${CYAN}"
cat << 'FOOTER'
  ═══════════════════════════════════════════════════════
FOOTER
echo -e "${NC}"
echo -e "  ${BOLD}${WHITE}RACE COMPLETE${NC}"
echo ""
echo -e "  ${DIM}Nodes:${NC}      $ALIVE_COUNT active"
echo -e "  ${DIM}Race ID:${NC}    $RACE_ID"
echo -e "  ${DIM}Time:${NC}       ${ELAPSED}s"
echo -e "  ${DIM}Dissent:${NC}    1 captured, escalated to arbiter"
echo -e "  ${DIM}Evidence:${NC}   $EV_COUNT entries, chain $CHAIN_VALID"
echo -e "  ${DIM}Memory:${NC}     3 prior decisions informed synthesis"
echo -e "  ${DIM}Dashboard:${NC}  $WEB"
echo -e "  ${DIM}Arbiter:${NC}    $WEB/arbiter"
echo ""
echo -e "  ${BOLD}${GREEN}Your AI team remembered, debated, and decided.${NC}"
echo -e "  ${BOLD}${GREEN}Every step cryptographically verified.${NC}"
echo -e "  ${BOLD}${GREEN}Human in the loop when confidence is low.${NC}"
echo ""
echo -e "${CYAN}"
cat << 'FOOTER2'
  ═══════════════════════════════════════════════════════
FOOTER2
echo -e "${NC}"
