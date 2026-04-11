#!/bin/bash
# Run cross-machine E2E tests (requires live daemon on port 8855)
#
# Usage:
#   ./multi-fleet/scripts/run-e2e.sh           # Run all E2E tests
#   ./multi-fleet/scripts/run-e2e.sh -k probe  # Run only probe-related tests
#   ./multi-fleet/scripts/run-e2e.sh --dry-run  # Show what would run
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLEET_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$FLEET_ROOT")"

# Quick daemon check
if ! curl -sf "http://127.0.0.1:${FLEET_NERVE_PORT:-8855}/health" > /dev/null 2>&1; then
    echo "WARNING: Fleet daemon not running on port ${FLEET_NERVE_PORT:-8855}"
    echo "Tests marked @integration will be skipped."
    echo ""
fi

if [[ "${1:-}" == "--dry-run" ]]; then
    echo "Would run: PYTHONPATH=$FLEET_ROOT:$REPO_ROOT python3 -m pytest $FLEET_ROOT/tests/test_cross_machine_e2e.py -v -m integration"
    exit 0
fi

cd "$REPO_ROOT"
PYTHONPATH="$FLEET_ROOT:$REPO_ROOT" python3 -m pytest \
    "$FLEET_ROOT/tests/test_cross_machine_e2e.py" \
    -v -m integration \
    "$@"
