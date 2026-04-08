#!/usr/bin/env bash
# Pre-demo setup — verify everything works, warm caches, seed evidence.
# Run 5 minutes before recording. Fixes what it can, warns about the rest.
#
# Usage: bash multi-fleet/scripts/demo-setup.sh
#        bash multi-fleet/scripts/demo-setup.sh --fix    # Auto-fix issues
#        bash multi-fleet/scripts/demo-setup.sh --seed    # Only seed demo data

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_ROOT/.multifleet/config.json"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

FIX=false
SEED_ONLY=false
[[ "${1:-}" == "--fix" ]] && FIX=true
[[ "${1:-}" == "--seed" ]] && SEED_ONLY=true

DAEMON="http://127.0.0.1:8855"
WEB="http://127.0.0.1:8856"
failures=()
warnings=()

ok()   { printf "  ${GREEN}PASS${NC}  %s\n" "$1"; }
fail() { printf "  ${RED}FAIL${NC}  %s\n" "$1"; failures+=("$1"); }
warn() { printf "  ${YELLOW}WARN${NC}  %s\n" "$1"; warnings+=("$1"); }
info() { printf "  ${DIM}INFO${NC}  %s\n" "$1"; }

ip_for() { python3 -c "import json; print(json.load(open('$CONFIG'))['nodes']['$1']['host'])" 2>/dev/null || echo ""; }

# ══════════════════════════════════════════════════════════════
# SEED DEMO DATA
# ══════════════════════════════════════════════════════════════
seed_demo_data() {
    echo ""
    printf "${BOLD}${CYAN}Seeding Demo Evidence${NC}\n"
    echo ""

    # Seed evidence entries so the demo has data to show
    for entry in \
        '{"type":"race_dispatch","task":"Implement retry with backoff","nodes":["mac1","mac2","mac3"],"timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
        '{"type":"probe_result","node":"mac1","score":92,"metric":"test_coverage","timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
        '{"type":"probe_result","node":"mac2","score":88,"metric":"lint_score","timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
        '{"type":"cross_review","reviewer":"mac1","reviewed":"mac2","verdict":"approve","timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}' \
        '{"type":"synthesis","winner":"mac2","confidence":0.73,"dissent_count":1,"timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'; do

        result=$(curl -sf -X POST "$DAEMON/evidence" \
            -H "Content-Type: application/json" \
            -d "$entry" 2>/dev/null || echo "failed")
        if [[ "$result" == "failed" ]]; then
            warn "Could not seed evidence entry (daemon may not support POST /evidence)"
            break
        else
            info "Seeded: $(echo "$entry" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("type","?"))' 2>/dev/null)"
        fi
    done

    # Warm the web dashboard
    curl -sf "$WEB/" > /dev/null 2>&1 && info "Dashboard cache warmed" || warn "Dashboard not reachable"
    curl -sf "$WEB/arbiter" > /dev/null 2>&1 && info "Arbiter view cache warmed" || warn "Arbiter view not reachable"

    echo ""
    printf "${GREEN}Demo data seeded.${NC}\n"
}

[[ "$SEED_ONLY" == true ]] && { seed_demo_data; exit 0; }

# ══════════════════════════════════════════════════════════════
echo ""
printf "${BOLD}${CYAN}══════════════════════════════════════════${NC}\n"
printf "${BOLD}${CYAN}  DEMO SETUP — Pre-Recording Checklist${NC}\n"
printf "${BOLD}${CYAN}══════════════════════════════════════════${NC}\n"
echo ""

# ── 1. Fleet Daemon ──
printf "${BOLD}[1/8] Fleet Daemon${NC}\n"
HEALTH=$(curl -sf --connect-timeout 3 "$DAEMON/health" 2>/dev/null || echo "")
if [ -n "$HEALTH" ]; then
    ok "Fleet daemon running on :8855"
else
    fail "Fleet daemon not running"
    if [[ "$FIX" == true ]]; then
        info "Attempting to start daemon..."
        cd "$REPO_ROOT"
        MULTIFLEET_NODE_ID=mac2 PYTHONPATH=multi-fleet nohup python3 -m multifleet.daemon > /tmp/fleet-daemon.log 2>&1 &
        sleep 2
        HEALTH=$(curl -sf --connect-timeout 3 "$DAEMON/health" 2>/dev/null || echo "")
        [ -n "$HEALTH" ] && ok "Daemon started successfully" || fail "Could not auto-start daemon"
    fi
fi

# ── 2. Web Server ──
printf "\n${BOLD}[2/8] Web Dashboard${NC}\n"
WEB_HEALTH=$(curl -sf --connect-timeout 3 "$WEB/health" 2>/dev/null || echo "")
if [ -n "$WEB_HEALTH" ]; then
    ok "Web server running on :8856"
else
    fail "Web server not running on :8856"
    if [[ "$FIX" == true ]]; then
        info "Attempting to start web server..."
        cd "$REPO_ROOT"
        MULTIFLEET_NODE_ID=mac2 PYTHONPATH=multi-fleet nohup python3 -m multifleet.web_server --port 8856 > /tmp/fleet-web.log 2>&1 &
        sleep 2
        WEB_HEALTH=$(curl -sf --connect-timeout 3 "$WEB/health" 2>/dev/null || echo "")
        [ -n "$WEB_HEALTH" ] && ok "Web server started" || fail "Could not auto-start web server"
    fi
fi

# ── 3. Node Reachability ──
printf "\n${BOLD}[3/8] Node Reachability${NC}\n"
reachable=0
for node in mac1 mac2 mac3; do
    ip=$(ip_for "$node")
    if [ -z "$ip" ]; then
        fail "$node — no IP in config"
        continue
    fi
    if curl -sf --connect-timeout 2 "http://$ip:8855/health" > /dev/null 2>&1; then
        ok "$node ($ip) — daemon responding"
        ((reachable++))
    else
        warn "$node ($ip) — not reachable (demo works with 1+ nodes)"
    fi
done
[ "$reachable" -eq 0 ] && fail "No nodes reachable — demo will show all offline"

# ── 4. NATS ──
printf "\n${BOLD}[4/8] NATS Server${NC}\n"
if nc -z 127.0.0.1 4222 2>/dev/null || lsof -i :4222 -sTCP:LISTEN > /dev/null 2>&1; then
    ok "NATS listening on :4222"
else
    warn "NATS not running — demo works without it (HTTP fallback)"
fi

# ── 5. Key Endpoints ──
printf "\n${BOLD}[5/8] API Endpoints${NC}\n"
for endpoint in "/health" "/api/status" "/api/probes" "/api/decisions"; do
    if curl -sf --connect-timeout 2 "$WEB$endpoint" > /dev/null 2>&1; then
        ok "GET $endpoint"
    else
        warn "GET $endpoint — not responding"
    fi
done

# Arbiter view
if curl -sf --connect-timeout 2 "$WEB/arbiter" > /dev/null 2>&1; then
    ok "GET /arbiter (Human Arbiter View)"
else
    warn "GET /arbiter — not responding"
fi

# ── 6. Evidence Ledger ──
printf "\n${BOLD}[6/8] Evidence Ledger${NC}\n"
EV=$(curl -sf "$DAEMON/evidence/latest" 2>/dev/null || echo "[]")
EV_COUNT=$(echo "$EV" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null || echo "0")
if [ "$EV_COUNT" -gt 0 ]; then
    ok "Evidence ledger has $EV_COUNT entries"
else
    warn "Evidence ledger empty — run --seed to populate"
fi

VER=$(curl -sf "$DAEMON/evidence/verify" 2>/dev/null || echo '{"valid":true}')
VALID=$(echo "$VER" | python3 -c "import json,sys; print('VALID' if json.load(sys.stdin).get('valid',True) else 'BROKEN')" 2>/dev/null || echo "VALID")
if [[ "$VALID" == "VALID" ]]; then
    ok "Evidence chain integrity: VALID"
else
    fail "Evidence chain integrity: BROKEN"
fi

# ── 7. Terminal Setup ──
printf "\n${BOLD}[7/8] Terminal Appearance${NC}\n"
COLS=$(tput cols 2>/dev/null || echo 80)
ROWS=$(tput lines 2>/dev/null || echo 24)
if [ "$COLS" -ge 80 ]; then
    ok "Terminal width: ${COLS} columns (need 80+)"
else
    warn "Terminal width: ${COLS} columns — resize to 80+ for demo"
fi
if [ "$ROWS" -ge 30 ]; then
    ok "Terminal height: ${ROWS} rows"
else
    warn "Terminal height: ${ROWS} rows — 30+ recommended"
fi
info "Suggested: dark background, 14-16pt monospace font"
info "Suggested: hide menu bar, no notifications during recording"

# ── 8. Dry Run ──
printf "\n${BOLD}[8/8] Demo Script Dry Run${NC}\n"
if [ -f "$SCRIPT_DIR/demo-fleet-race.sh" ]; then
    ok "demo-fleet-race.sh exists"
    if bash -n "$SCRIPT_DIR/demo-fleet-race.sh" 2>/dev/null; then
        ok "demo-fleet-race.sh syntax valid"
    else
        fail "demo-fleet-race.sh has syntax errors"
    fi
else
    fail "demo-fleet-race.sh not found"
fi

# ══════════════════════════════════════════════════════════════
# SEED
# ══════════════════════════════════════════════════════════════
if [ "$EV_COUNT" -eq 0 ]; then
    echo ""
    info "Seeding demo evidence data..."
    seed_demo_data
fi

# ══════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════
echo ""
printf "${BOLD}${CYAN}══════════════════════════════════════════${NC}\n"

if [ ${#failures[@]} -eq 0 ] && [ ${#warnings[@]} -eq 0 ]; then
    printf "${BOLD}${GREEN}  READY FOR RECORDING${NC}\n"
elif [ ${#failures[@]} -eq 0 ]; then
    printf "${BOLD}${YELLOW}  READY (with ${#warnings[@]} warnings)${NC}\n"
    for w in "${warnings[@]}"; do
        printf "  ${YELLOW}  ⚠ %s${NC}\n" "$w"
    done
else
    printf "${BOLD}${RED}  NOT READY — ${#failures[@]} failure(s)${NC}\n"
    for f in "${failures[@]}"; do
        printf "  ${RED}  ✗ %s${NC}\n" "$f"
    done
    if [ ${#warnings[@]} -gt 0 ]; then
        for w in "${warnings[@]}"; do
            printf "  ${YELLOW}  ⚠ %s${NC}\n" "$w"
        done
    fi
    echo ""
    info "Run with --fix to auto-repair what's possible"
fi

printf "${BOLD}${CYAN}══════════════════════════════════════════${NC}\n"
echo ""

# Quick reference
printf "${DIM}Quick commands:${NC}\n"
printf "${DIM}  Record demo:  bash multi-fleet/scripts/demo-fleet-race.sh --theatrical${NC}\n"
printf "${DIM}  Seed data:    bash multi-fleet/scripts/demo-setup.sh --seed${NC}\n"
printf "${DIM}  Dashboard:    open $WEB${NC}\n"
printf "${DIM}  Arbiter:      open $WEB/arbiter${NC}\n"
echo ""

exit ${#failures[@]}
