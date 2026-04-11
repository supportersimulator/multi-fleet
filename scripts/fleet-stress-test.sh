#!/bin/bash
# Run the 30-day stress simulation for multi-fleet.
#
# By default, runs only the fast tests (no @pytest.mark.slow).
# Pass --slow to include the full 30-day simulations.
#
# Usage:
#   ./multi-fleet/scripts/fleet-stress-test.sh          # fast tests only
#   ./multi-fleet/scripts/fleet-stress-test.sh --slow    # include 30-day sims
#   ./multi-fleet/scripts/fleet-stress-test.sh -k "dedup" # filter by name

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

MARKER_FILTER='-m "not slow"'
EXTRA_ARGS=()

for arg in "$@"; do
    if [ "$arg" = "--slow" ]; then
        MARKER_FILTER=""
    else
        EXTRA_ARGS+=("$arg")
    fi
done

cd "$REPO_ROOT"

echo "=== Multi-Fleet 30-Day Stress Test ==="
echo "Repo: $REPO_ROOT"
echo ""

if [ -n "$MARKER_FILTER" ]; then
    echo "Mode: fast (skipping @slow tests). Pass --slow for full simulation."
    PYTHONPATH="multi-fleet:." python3 -m pytest multi-fleet/tests/test_stress_30day.py \
        -v --tb=short -m "not slow" "${EXTRA_ARGS[@]}"
else
    echo "Mode: full (including 30-day simulations)"
    PYTHONPATH="multi-fleet:." python3 -m pytest multi-fleet/tests/test_stress_30day.py \
        -v --tb=short "${EXTRA_ARGS[@]}"
fi
