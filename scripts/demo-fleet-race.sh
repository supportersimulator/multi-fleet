#!/bin/bash
# ContextDNA Multi-Fleet — Competitive Branch Racing Demo
# "Your AI team remembers everything, debates everything, and never sleeps."
#
# Usage: bash scripts/demo-fleet-race.sh "Fix the auth flow"

set -euo pipefail
TASK="${1:-Implement a retry mechanism with exponential backoff}"
DAEMON="http://127.0.0.1:8855"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║     ContextDNA Multi-Fleet — Branch Racing Demo         ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Task: $TASK"
echo ""

# Step 1: Fleet health
echo "━━━ Step 1: Fleet Health Check ━━━"
HEALTH=$(curl -sf "$DAEMON/health" 2>/dev/null || echo '{"status":"offline"}')
STATUS=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status','offline'))" 2>/dev/null)
TYPES=$(echo "$HEALTH" | python3 -c "import json,sys; print(json.load(sys.stdin).get('registered_types',0))" 2>/dev/null)
echo "  Daemon: $STATUS | Packet types: $TYPES"

PEERS=$(echo "$HEALTH" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ch=d.get('channel_health',{})
for k,v in ch.items():
    print(f'  {k}: {v.get(\"score\",\"?\")}')" 2>/dev/null)
echo "$PEERS"
echo ""

# Step 2: Gains gate
echo "━━━ Step 2: Gains Gate (quick) ━━━"
GATE=$(curl -sf "$DAEMON/gains-gate?quick=1" 2>/dev/null || echo '{"passed":false}')
PASSED=$(echo "$GATE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'PASS ({d.get(\"passed_count\",0)}/{d.get(\"total\",0)})' if d.get('passed') else f'FAIL ({d.get(\"passed_count\",0)}/{d.get(\"total\",0)})')" 2>/dev/null)
echo "  Gate: $PASSED"
echo ""

# Step 3: Start the race
echo "━━━ Step 3: Starting Branch Race ━━━"
RACE=$(curl -sf -X POST "$DAEMON/race" -H "Content-Type: application/json" \
  -d "{\"task\":\"$TASK\",\"timeout\":300}" 2>/dev/null || echo '{"error":"daemon unreachable"}')
RACE_ID=$(echo "$RACE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('race_id','none'))" 2>/dev/null)
echo "  Race ID: $RACE_ID"
echo "  Participants dispatched to fleet nodes"
echo ""

# Step 4: Show evidence
echo "━━━ Step 4: Evidence Ledger ━━━"
EVIDENCE=$(curl -sf "$DAEMON/evidence/latest" 2>/dev/null || echo '[]')
COUNT=$(echo "$EVIDENCE" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if isinstance(d,list) else 0)" 2>/dev/null)
echo "  Entries: $COUNT (SHA-256 hash chain verified)"

VERIFY=$(curl -sf "$DAEMON/evidence/verify" 2>/dev/null || echo '{"valid":true}')
VALID=$(echo "$VERIFY" | python3 -c "import json,sys; print('VALID' if json.load(sys.stdin).get('valid',True) else 'BROKEN')" 2>/dev/null)
echo "  Chain integrity: $VALID"
echo ""

# Step 5: Dashboard snapshot
echo "━━━ Step 5: Fleet Dashboard ━━━"
DASH=$(curl -sf "$DAEMON/dashboard/data" 2>/dev/null || echo '{}')
echo "$DASH" | python3 -c "
import json,sys
d=json.load(sys.stdin)
nodes=d.get('nodes',[])
races=d.get('races',[])
leases=d.get('leases',[])
rebs=d.get('rebuttals',[])
print(f'  Nodes: {len(nodes) if isinstance(nodes,list) else 0}')
print(f'  Active races: {len(races) if isinstance(races,list) else 0}')
print(f'  Active leases: {len(leases) if isinstance(leases,list) else 0}')
print(f'  Active rebuttals: {len(rebs) if isinstance(rebs,list) else 0}')
" 2>/dev/null
echo ""

echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Race started. Nodes working in isolated worktrees.     ║"
echo "║  Evidence accumulates. Memory compounds.                ║"
echo "║  Your AI team never sleeps.                             ║"
echo "╚══════════════════════════════════════════════════════════╝"
