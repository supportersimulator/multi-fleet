#!/bin/bash
# Detect drift between tools/ and multifleet/ copies
# Usage: ./multi-fleet/scripts/check-drift.sh
#
# Compares files that exist in both tools/ (legacy standalone) and
# multi-fleet/multifleet/ (canonical package). Non-zero diff = drift.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

echo "=== tools/ ↔ multifleet/ Drift Report ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo ""

TOTAL=0
DRIFTED=0
MISSING=0

check_pair() {
    local tools_name="$1"
    local multi_name="$2"
    local TOOLS="tools/${tools_name}.py"
    local MULTI="multi-fleet/multifleet/${multi_name}.py"
    TOTAL=$((TOTAL + 1))

    if [ ! -f "$TOOLS" ]; then
        echo "SKIP  $tools_name: tools/ copy not found"
        return
    fi

    if [ ! -f "$MULTI" ]; then
        echo "MISS  $tools_name → $MULTI not found"
        MISSING=$((MISSING + 1))
        return
    fi

    local DIFF_LINES
    DIFF_LINES=$(diff "$TOOLS" "$MULTI" | wc -l | tr -d ' ')
    if [ "$DIFF_LINES" -eq 0 ]; then
        echo "OK    $tools_name ($DIFF_LINES lines diff)"
    else
        echo "DRIFT $tools_name: $DIFF_LINES lines different"
        DRIFTED=$((DRIFTED + 1))
        diff "$TOOLS" "$MULTI" | head -5 | sed 's/^/      /'
        echo "      ..."
    fi
}

check_pair "fleet_nerve_config"      "config"
check_pair "fleet_nerve_nats"        "nats_client"
check_pair "fleet_nerve_security"    "security"
check_pair "fleet_nerve_store"       "store"
check_pair "fleet_remote_worker"     "worker"
check_pair "multifleet_coordinator"  "coordinator"
check_pair "multifleet_packet_store" "packet_store"
check_pair "fleet_nerve_visual"      "visual"

echo ""
echo "=== Summary ==="
echo "Total checked: $TOTAL"
echo "Identical:     $((TOTAL - DRIFTED - MISSING))"
echo "Drifted:       $DRIFTED"
echo "Missing:       $MISSING"

if [ $DRIFTED -gt 0 ] || [ $MISSING -gt 0 ]; then
    echo ""
    echo "ACTION: multi-fleet/multifleet/ is canonical. Sync tools/ copies or delete them."
    exit 1
fi
