#!/usr/bin/env bash
# demo-race.sh — Run competitive branch racing across the fleet
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
TASK="${1:-Demo: implement a fibonacci function with memoization and tests}"
TIMEOUT="${2:-120}"

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  COMPETITIVE BRANCH RACING — Fleet Demo${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${YELLOW}Task:${NC} $TASK"

# Check daemon
HEALTH=$(curl -sf http://127.0.0.1:8855/health 2>/dev/null) || { echo -e "${RED}✗ Daemon not running${NC}"; exit 1; }
NODE_ID=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin)['nodeId'])")
PEERS=$(echo "$HEALTH" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('peers',{})))")
echo -e "${GREEN}✓${NC} $NODE_ID online, ${GREEN}$PEERS${NC} peers"

# Discover and start
cd "$(dirname "$0")/.."
NODES=$(echo "$HEALTH" | python3 -c "
import sys,json; h=json.load(sys.stdin)
nodes=[h['nodeId']]+[n for n in h.get('peers',{}) if not n.startswith('test')]
print(','.join(nodes[:3]))
")
echo -e "Racers: ${GREEN}$NODES${NC}"

python3 -c "
import json
from multifleet.fleet_race import FleetRace
r = FleetRace(node_id='$NODE_ID')
result = r.start('$TASK', nodes='$NODES'.split(','), timeout_s=$TIMEOUT)
print(json.dumps(result, indent=2, default=str))
for node, p in result.get('participants',{}).items():
    s = p.get('status','?')
    icon = '✓' if s in ('dispatched','completed') else '✗'
    print(f'  {icon} {node}: {s}')
if result.get('winner'):
    print(f'  Winner: {result[\"winner\"]}')
"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
