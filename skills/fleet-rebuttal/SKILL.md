---
name: fleet-rebuttal
description: Cross-machine rebuttal protocol — the 4-phase cycle where machines produce verdicts, critique each other, and converge on a chief decision
---

# Fleet Rebuttal

The cross-machine rebuttal protocol is the multi-machine analogue of 3-Surgeons cross-examination. Instead of 3 LLMs on one machine, 3 machines each run local surgery, produce structured verdicts, then critique each other's work. The Chief synthesizes everything into a final decision with preserved dissent.

This is the core of the 3x3x3 vision: same hospital, same protocol, different operating rooms, same patient.

## The 4-Phase Protocol

### Phase 1: Chief Assigns Task Brief

The Chief (human or coordinator node) sends an identical task brief to all machines. Each machine receives the same objective but works independently in its own worktree/branch.

**Task Brief Schema:**

```json
{
  "protocol": "3s-lan/v1",
  "message_type": "task_brief",
  "task_id": "task-abc123",
  "from": "chief",
  "objective": "Improve orchestration reliability without increasing complexity",
  "scope": ["orchestrator/", "agents/", "tests/"],
  "constraints": [
    "Must not break existing 71 tests",
    "No new external dependencies",
    "Performance budget: <5ms added latency"
  ],
  "context": "Current orchestrator has 3 known retry bugs. See issue #42.",
  "deadline_utc": "2026-04-05T18:00:00Z",
  "expected_output": "verdict"
}
```

**How to send the task brief:**

```bash
# Broadcast task brief to all fleet nodes
TASK_ID="task-$(date +%s | tail -c 7)"
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"task_brief\",
    \"from\": \"chief\",
    \"payload\": {
      \"subject\": \"Task brief: ${TASK_ID}\",
      \"body\": {
        \"protocol\": \"3s-lan/v1\",
        \"message_type\": \"task_brief\",
        \"task_id\": \"${TASK_ID}\",
        \"from\": \"chief\",
        \"objective\": \"<objective here>\",
        \"scope\": [\"<dirs>\"],
        \"constraints\": [\"<list>\"],
        \"context\": \"<background>\",
        \"deadline_utc\": \"<ISO 8601>\",
        \"expected_output\": \"verdict\"
      }
    }
  }"
```

### Phase 2: Local Surgery + Verdict

Each machine receives the task brief and runs its full local 3-Surgeons protocol:

1. Head Surgeon triages the brief
2. Local surgeons investigate independently in their own worktree
3. Cross-examination within the local cell
4. Head Surgeon produces a **verdict packet** (see fleet-verdict skill)

Each machine works in its own branch:
- mac1: `feature/${TASK_ID}-mac1`
- mac2: `feature/${TASK_ID}-mac2`
- mac3: `feature/${TASK_ID}-mac3`

When local surgery is complete, each machine sends its verdict to the Chief (or broadcasts to all nodes).

**Timeline:** Machines work in parallel. Chief sets a deadline in the task brief. Verdicts arriving after deadline are accepted but marked `late: true`.

### Phase 3: Cross-Machine Critique

This is where the 3x3x3 protocol diverges from single-machine surgery. The Chief sends each machine's verdict to the OTHER machines for critique.

**Critique routing (round-robin):**
- mac1's verdict goes to mac2 and mac3
- mac2's verdict goes to mac1 and mac3
- mac3's verdict goes to mac1 and mac2

**Critique Request Schema:**

```json
{
  "protocol": "3s-lan/v1",
  "message_type": "critique_request",
  "task_id": "task-abc123",
  "from": "chief",
  "to": "mac2",
  "verdict_under_review": {
    "machine": "mac1",
    "summary": "...",
    "proposed_changes": ["..."],
    "assumptions": ["..."],
    "risks": ["..."],
    "confidence": 0.82,
    "evidence": ["..."]
  },
  "instructions": "Review this verdict. Challenge assumptions. Identify risks the author missed. State whether you agree, disagree, or partially agree. Be specific."
}
```

**How to send critique requests:**

```bash
# Chief sends mac1's verdict to mac2 for critique
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d "{
    \"type\": \"critique_request\",
    \"from\": \"chief\",
    \"to\": \"mac2\",
    \"payload\": {
      \"subject\": \"Critique mac1 verdict for ${TASK_ID}\",
      \"body\": $(cat /tmp/fleet-verdicts/mac1-${TASK_ID}.json),
      \"task_id\": \"${TASK_ID}\",
      \"instructions\": \"Review this verdict. Challenge assumptions. Identify missed risks. State agree/disagree/partial.\"
    }
  }"
```

**Critique Response Schema:**

```json
{
  "protocol": "3s-lan/v1",
  "message_type": "critique",
  "task_id": "task-abc123",
  "from": "mac2",
  "reviewing": "mac1",
  "stance": "partial_agree",
  "agreements": [
    "Router simplification is correct — we independently reached the same conclusion"
  ],
  "challenges": [
    {
      "target_assumption": "Redis pub/sub latency stays under 5ms",
      "challenge": "Under 3-node load we measured 12ms P99 — this assumption is unsafe",
      "evidence": "Load test on mac2: p50=2ms, p95=7ms, p99=12ms (N=1000)",
      "severity": "high"
    }
  ],
  "missed_risks": [
    "No consideration of partial network partition between mac1 and mac3"
  ],
  "alternative_approach": "Use NATS JetStream with ack instead of Redis pub/sub for guaranteed delivery",
  "confidence_adjustment": -0.15,
  "recommendation": "merge_with_modifications"
}
```

### Phase 4: Chief Synthesis

The Chief collects all verdicts and all critiques, then produces a final decision.

**Chief Decision Schema:**

```json
{
  "protocol": "3s-lan/v1",
  "message_type": "chief_decision",
  "task_id": "task-abc123",
  "from": "chief",
  "timestamp": "2026-04-05T17:45:00Z",
  "verdicts_received": ["mac1", "mac2", "mac3"],
  "critiques_received": 6,
  "recommended_plan": {
    "action": "merge_mac1_with_modifications",
    "branch": "feature/task-abc123-mac1",
    "modifications": [
      "Replace Redis pub/sub with NATS JetStream (from mac2 critique)",
      "Add partition tolerance test (from mac3 critique)"
    ]
  },
  "strongest_alternative": {
    "machine": "mac3",
    "summary": "Full rewrite using event sourcing — cleaner but 3x scope",
    "preserve_as": "feature/task-abc123-mac3-alternative"
  },
  "risks": [
    {
      "risk": "Redis latency under load exceeds assumption",
      "flagged_by": ["mac2", "mac3"],
      "mitigation": "Switch to NATS JetStream per mac2 recommendation"
    }
  ],
  "unresolved_dissent": [
    {
      "from": "mac3",
      "claim": "Event sourcing would prevent this class of bug entirely",
      "chief_note": "Valid but out of scope for this task. Track as future work."
    }
  ],
  "confidence": 0.81,
  "next_steps": [
    "mac1: apply modifications and push updated branch",
    "mac2: write NATS JetStream integration test",
    "mac3: preserve alternative branch, write partition test"
  ]
}
```

**How the Chief synthesizes:**

```bash
# Collect all verdicts and critiques
mkdir -p /tmp/fleet-synthesis/${TASK_ID}
ls /tmp/fleet-verdicts/*-${TASK_ID}.json
ls /tmp/fleet-critiques/*-${TASK_ID}.json

# Synthesis checklist:
# 1. Compare confidence scores across verdicts
# 2. Identify assumption conflicts (A assumes X, B's risks include "X may fail")
# 3. Weight critiques by evidence quality (measurements > opinions)
# 4. Risks flagged by 2+ machines are HIGH priority
# 5. Dissent from ANY source is preserved, never suppressed
# 6. Pick recommended plan based on: evidence strength > confidence > simplicity
# 7. Preserve strongest alternative as a named branch
# 8. Assign next steps to specific machines
```

## Message Types Summary

| Type | Sender | Receiver | Phase | Purpose |
|------|--------|----------|-------|---------|
| `task_brief` | Chief | All machines | 1 | Assign objective and constraints |
| `local_verdict` | Each machine | Chief | 2 | Structured result of local surgery |
| `critique_request` | Chief | Each machine | 3 | Send another machine's verdict for review |
| `critique` | Each machine | Chief | 3 | Structured critique of another machine's verdict |
| `chief_decision` | Chief | All machines | 4 | Final synthesis with plan, alternatives, dissent |
| `merge_candidate` | Chief | Specific machine | 4 | Request to prepare branch for merge |

## Timing and Deadlines

| Phase | Typical Duration | Timeout |
|-------|-----------------|---------|
| Phase 1: Brief distribution | <30s | 2min |
| Phase 2: Local surgery + verdict | 10-60min | Set in brief |
| Phase 3: Cross-critique | 5-15min | 20min |
| Phase 4: Chief synthesis | 5-10min | 15min |

## Handling Edge Cases

### Machine offline during Phase 2
- Chief proceeds with available verdicts after deadline
- Late verdicts accepted but marked `late: true`
- Minimum 2 verdicts required for Phase 3 to proceed

### Machine disagrees with Chief decision
- Dissent is recorded in `unresolved_dissent`
- Machine may request a re-evaluation with new evidence
- Chief can grant a "second opinion" round (Phase 3 repeats)
- Human always has final authority

### Conflicting critiques
- If mac2 says "merge mac1" and mac3 says "reject mac1", both critiques go to Chief
- Chief weighs by evidence quality, not vote count
- Unresolvable conflicts escalated to human with full context

### No clear winner
- Chief may request a "reconciliation branch" — one machine cherry-picks the best from all approaches
- Or Chief may declare "needs more investigation" and assign a follow-up task brief

## Integration with Other Skills

- **fleet-verdict**: Produces the verdict packets consumed by Phase 2-3
- **fleet-send**: Transport layer for all message types
- **fleet-task**: Can be used to trigger local surgery on remote machines
- **fleet-coordinator**: Agent that orchestrates the 4-phase cycle
- **fleet-dispatch**: Tracked dispatch alternative for Phase 1 distribution
- **fleet-status**: Monitor which machines have submitted verdicts

## Running a Full Rebuttal Cycle

Quick reference for the coordinator:

```bash
# Phase 1: Send task brief to all machines
TASK_ID="task-$(date +%s | tail -c 7)"
# (use broadcast with type: task_brief)

# Phase 2: Wait for verdicts
# Poll inbox for verdict messages matching TASK_ID
watch -n 30 "curl -sf http://127.0.0.1:8855/inbox | python3 -c \"
import json,sys
v=[m for m in json.load(sys.stdin) if m.get('type')=='verdict']
print(f'{len(v)} verdicts received')
\""

# Phase 3: Distribute critiques (after all verdicts collected)
# For each verdict, send to other machines as critique_request

# Phase 4: Collect critiques and synthesize
# Write chief_decision and broadcast to all machines
```

## Rules

- Chief NEVER overrides without stating why — transparency is mandatory
- Dissent is ALWAYS preserved — suppressing disagreement violates the 3-Surgeons principle
- Evidence outweighs confidence — a 0.6 confidence verdict with measurements beats a 0.9 opinion
- Minimum 2 machines must participate for a valid rebuttal cycle
- Critiques must be specific — "I disagree" is not a valid critique
- The human has final authority over the Chief at all times
- No machine critiques its own verdict — that is what local surgery already did
