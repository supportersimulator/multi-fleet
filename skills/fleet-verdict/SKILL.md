---
name: fleet-verdict
description: Produce and send a structured verdict packet after local surgery — the atomic unit of cross-machine consensus in the 3x3x3 protocol
---

# Fleet Verdict

A verdict is the atomic output of one machine's local surgery on a task. Every machine in the fleet produces the same structured JSON verdict regardless of IDE, branch, or approach. The Chief Surgeon synthesizes verdicts; other machines critique them. This is the foundation of the 3x3x3 protocol.

## Verdict Packet Schema

Every verdict MUST conform to this schema. No optional fields — every key is required (use empty arrays/null for unused fields).

```json
{
  "protocol": "3s-lan/v1",
  "message_type": "local_verdict",
  "task_id": "task-abc123",
  "machine": "mac2",
  "branch": "feature/approach-b",
  "base": "main",
  "role": "implementation",
  "phase": "close",
  "head_surgeon": "atlas",
  "timestamp": "2026-04-05T14:30:00Z",
  "summary": "Refactored orchestration loop to reduce cross-agent chatter by 60%.",
  "proposed_changes": [
    "orchestrator/router.ts",
    "agents/consensus.ts"
  ],
  "assumptions": [
    "Redis pub/sub latency stays under 5ms on LAN",
    "No concurrent schema migrations during rollout"
  ],
  "risks": [
    "Retry semantics need integration test coverage",
    "Redis fallback path not fully exercised under load"
  ],
  "confidence": 0.78,
  "evidence": [
    "Measured 60% reduction in message volume in local test (N=50)",
    "All 71 existing tests pass on this branch"
  ],
  "dissent": [
    {
      "surgeon": "neurologist",
      "claim": "May hide coupling behind cleaner abstraction",
      "severity": "medium"
    }
  ],
  "questions_for_chief": [
    "Should we preserve the old retry path as fallback?",
    "Is 60% reduction sufficient or should we target 80%?"
  ],
  "files_touched": [
    "orchestrator/router.ts",
    "agents/consensus.ts",
    "tests/router.test.ts"
  ],
  "tests_run": 71,
  "tests_passed": 71,
  "recommended_action": "merge_if_mac1_approach_is_weaker"
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `protocol` | string | Always `"3s-lan/v1"` |
| `message_type` | string | Always `"local_verdict"` |
| `task_id` | string | Links verdict to the original task brief |
| `machine` | string | Node ID (`$MULTIFLEET_NODE_ID`) |
| `branch` | string | Git branch where work was done |
| `base` | string | Branch this was based on |
| `role` | string | Machine's assigned role for this task |
| `phase` | string | Surgery phase at completion: `close` |
| `head_surgeon` | string | Name of the head surgeon that synthesized |
| `timestamp` | string | ISO 8601 UTC |
| `summary` | string | 1-3 sentences: what was done and why |
| `proposed_changes` | string[] | Files this verdict recommends changing |
| `assumptions` | string[] | What this approach assumes to be true |
| `risks` | string[] | Known risks with this approach |
| `confidence` | float | 0.0-1.0 — how confident the local team is |
| `evidence` | string[] | Concrete measurements, test results, observations |
| `dissent` | object[] | Internal disagreements preserved from local surgery |
| `questions_for_chief` | string[] | Unresolved questions needing fleet-level input |
| `files_touched` | string[] | All files modified on this branch |
| `tests_run` | int | Number of tests executed |
| `tests_passed` | int | Number passing |
| `recommended_action` | string | What this machine thinks should happen next |

## How to Produce a Verdict

### Step 1: Run local surgery

Complete your local 3-Surgeons analysis on the assigned task. This means:
1. Head Surgeon triages the task brief
2. Local surgeons (Atlas, Cardiologist, Neurologist) investigate independently
3. Cross-examination happens within the local cell
4. Head Surgeon synthesizes a local conclusion

### Step 2: Collect verdict data

```bash
# Get current branch and files touched
BRANCH=$(git branch --show-current)
BASE="main"
FILES=$(git diff --name-only ${BASE}...HEAD | tr '\n' ',' | sed 's/,$//')

# Get test results
TEST_OUTPUT=$(cd /path/to/project && npm test 2>&1 || python -m pytest 2>&1)

# Get node ID
MACHINE=$MULTIFLEET_NODE_ID
```

### Step 3: Assemble the verdict JSON

Write the verdict to a file for sending:

```bash
cat > /tmp/fleet-verdict-${TASK_ID}.json << 'EOF'
{
  "protocol": "3s-lan/v1",
  "message_type": "local_verdict",
  "task_id": "<from task brief>",
  "machine": "<$MULTIFLEET_NODE_ID>",
  "branch": "<current branch>",
  "base": "main",
  "role": "<assigned role>",
  "phase": "close",
  "head_surgeon": "atlas",
  "timestamp": "<ISO 8601 UTC>",
  "summary": "<1-3 sentences>",
  "proposed_changes": ["<files>"],
  "assumptions": ["<list>"],
  "risks": ["<list>"],
  "confidence": 0.00,
  "evidence": ["<list>"],
  "dissent": [],
  "questions_for_chief": [],
  "files_touched": ["<files>"],
  "tests_run": 0,
  "tests_passed": 0,
  "recommended_action": "<recommendation>"
}
EOF
```

### Step 4: Validate the verdict

Before sending, verify all required fields are present and non-null:

```bash
python3 -c "
import json, sys
with open('/tmp/fleet-verdict-${TASK_ID}.json') as f:
    v = json.load(f)
required = ['protocol','message_type','task_id','machine','branch','base',
            'role','phase','head_surgeon','timestamp','summary',
            'proposed_changes','assumptions','risks','confidence',
            'evidence','dissent','questions_for_chief','files_touched',
            'tests_run','tests_passed','recommended_action']
missing = [k for k in required if k not in v or v[k] is None]
if missing:
    print(f'INVALID: missing fields: {missing}', file=sys.stderr)
    sys.exit(1)
if not (0.0 <= v['confidence'] <= 1.0):
    print(f'INVALID: confidence must be 0.0-1.0, got {v[\"confidence\"]}', file=sys.stderr)
    sys.exit(1)
print('Verdict valid.')
"
```

## How to Send a Verdict

Use `fleet-send` with `type: verdict`. The verdict JSON goes in the payload body.

```bash
# Send verdict to chief
VERDICT=$(cat /tmp/fleet-verdict-${TASK_ID}.json)
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"verdict\",
    \"from\": \"${MULTIFLEET_NODE_ID}\",
    \"to\": \"chief\",
    \"payload\": {
      \"subject\": \"Verdict for ${TASK_ID}\",
      \"body\": $(cat /tmp/fleet-verdict-${TASK_ID}.json),
      \"task_id\": \"${TASK_ID}\"
    }
  }"

# Or broadcast verdict to all nodes (for cross-critique phase)
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"verdict\",
    \"from\": \"${MULTIFLEET_NODE_ID}\",
    \"payload\": {
      \"subject\": \"Verdict for ${TASK_ID}\",
      \"body\": $(cat /tmp/fleet-verdict-${TASK_ID}.json),
      \"task_id\": \"${TASK_ID}\"
    }
  }"
```

## How Chief Collects Verdicts

The Chief Surgeon (or any coordinator node) collects verdicts from all machines:

```bash
# Check inbox for verdict messages
curl -sf http://127.0.0.1:8855/inbox | python3 -c "
import json, sys
msgs = json.load(sys.stdin)
verdicts = [m for m in msgs if m.get('type') == 'verdict']
print(f'Collected {len(verdicts)} verdicts')
for v in verdicts:
    body = v.get('payload', {}).get('body', {})
    print(f'  {body.get(\"machine\",\"?\")} — confidence: {body.get(\"confidence\",\"?\")} — {body.get(\"summary\",\"\")[:80]}')
"
```

### Verdict Synthesis (Chief)

Once all expected verdicts are collected, the Chief compares:

1. **Confidence spread** — if all machines agree (confidence >0.7), lean toward merge. If spread is wide, request rebuttals.
2. **Risk intersection** — risks flagged by 2+ machines are high-priority.
3. **Dissent preservation** — dissent from ANY machine's local surgery is surfaced to the human, never suppressed.
4. **Assumptions conflict** — if machine A assumes X and machine B's risks include "X may not hold", flag the contradiction.
5. **Recommended actions** — compare what each machine thinks should happen next.

## Verdict Storage

Verdicts are stored locally at `/tmp/fleet-verdicts/` for the duration of the task:

```bash
mkdir -p /tmp/fleet-verdicts
cp /tmp/fleet-verdict-${TASK_ID}.json /tmp/fleet-verdicts/${MACHINE}-${TASK_ID}.json
```

## Integration with Other Skills

- **fleet-rebuttal**: After verdicts are collected, the rebuttal phase sends each verdict to other machines for critique
- **fleet-send**: Transport layer for verdict delivery (type: `verdict`)
- **fleet-task**: The task brief that triggers local surgery and verdict production
- **fleet-dispatch**: Tracked dispatch can be used instead of fleet-send for lifecycle visibility
- **fleet-coordinator**: Coordinator agent orchestrates verdict collection and synthesis

## When to Use This Skill

- After completing local surgery on a fleet-assigned task
- When the Chief requests a formal verdict from your machine
- During Phase 2 of the cross-machine rebuttal protocol
- Any time structured output is needed for cross-machine comparison

## Rules

- Never send a verdict with `confidence: 1.0` — certainty is a red flag
- Never suppress local dissent — if a surgeon disagreed, it goes in the packet
- Always include evidence — a verdict without measurements is an opinion
- Validate before sending — malformed verdicts break the synthesis pipeline
- One verdict per machine per task — updates replace, not append
