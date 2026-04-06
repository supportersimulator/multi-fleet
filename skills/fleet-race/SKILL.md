---
name: fleet-race
description: Competitive branch racing — 3 machines solve the same problem in isolated worktrees, then propose/rebut/converge to the strongest solution
---

# Fleet Race — Competitive Branch Racing

Three machines. One problem. Isolated branches. Each node independently solves the task, produces a `local_verdict` packet, then the fleet runs pairwise cross-rebuttal and chief synthesis to converge on the best approach. The value is in the disagreements — different machines hit different edge cases, make different assumptions, find different risks.

Triggers: "race on this task", "fleet race", "competitive race", "branch race".

## When to Use

- **Architecture decisions** — 3 independent designs expose hidden tradeoffs
- **Complex refactors** — different decomposition strategies reveal coupling
- **Performance optimization** — parallel profiling finds different bottlenecks
- **Security hardening** — independent threat modeling covers more surface area
- **Contentious choices** — when the "right" answer is unclear, race to find it

NOT for: simple bug fixes, config changes, documentation, anything with one obvious solution.

## Protocol (7 Steps)

### Step 1: Create Isolated Branches

Each node gets its own branch via Superset bridge (preferred — handles worktree + environment) or raw git worktree fallback. Creates branches: `race/<race_id>/mac1`, `race/<race_id>/mac2`, `race/<race_id>/mac3`.

```bash
curl -sf -X POST http://127.0.0.1:8855/race \
  -H "Content-Type: application/json" \
  -d '{"task":"Refactor orchestration loop","nodes":["mac1","mac2","mac3"],"timeout_s":1800,"use_superset":true}'
```

### Step 2: Dispatch Task to All Nodes

Initiator broadcasts the race brief via fleet daemon. Each node receives the same task description plus its assigned branch and a `respond_with: local_verdict` directive.

### Step 3: Independent Work (Default 30min Timeout)

Each node works independently. No communication between racers. Each runs local 3-Surgeons verification (sentinel + cross-exam) before submitting.

```bash
3s sentinel "race approach for ${RACE_ID}"
3s cross-exam "Is this approach sound?"
```

### Step 4: Collect Verdict Packets

Each node produces a `local_verdict` (see fleet-verdict skill) and submits it. Chief waits for all verdicts before proceeding.

```bash
curl -sf -X POST http://127.0.0.1:8855/race/${RACE_ID}/submit \
  -H "Content-Type: application/json" \
  -d "{\"node\":\"${MULTIFLEET_NODE_ID}\",\"result\":$(cat /tmp/fleet-verdict-${RACE_ID}.json)}"
```

### Step 5: Cross-Rebuttal (Pairwise Critiques)

Each node receives the OTHER nodes' verdicts for adversarial critique. mac1 critiques mac2+mac3, mac2 critiques mac1+mac3, etc. Each rebuttal identifies: (1) flaws they missed, (2) risks they underestimated, (3) strengths yours lacks.

### Step 6: Chief Synthesis — Merge Decision

Chief synthesizes all verdicts + rebuttals. Evaluates:

- **Confidence spread** — wide disagreement = request clarification
- **Risk intersection** — risks flagged by 2+ nodes are high priority
- **Assumption conflicts** — node A assumes X, node B's risks include "X may not hold"
- **Rebuttal severity** — critical flaws found in rebuttal block that approach
- **Test coverage** — highest passing tests with fewest assumptions wins tiebreakers

### Step 7: Execute Merge or Escalate

Clear winner with no critical dissent: auto-merge the winning branch. Contested: escalate to human with all verdicts, rebuttals, and chief analysis side by side.

```bash
# Auto-merge (clear winner)
git merge race/${RACE_ID}/mac2 --no-ff -m "fleet-race: ${RACE_ID} winner=mac2"

# Contested — daemon sends alert to Aaron with full evidence
```

## CLI Quick Reference

```bash
# Start a race
fleet_race task="Fix the auth flow" nodes="mac1,mac2,mac3" timeout=1800

# Check status
fleet_race_status race_id="a1b2c3d4" action="status"

# List active / cancel
fleet_race_status action="list"
fleet_race_status race_id="a1b2c3d4" action="cancel"
```

## Race Lifecycle

```
fleet_race "task" → broadcast race_start → create branches → dispatch to nodes
  → each node works in isolated worktree (racing)
  → verdicts collected → cross-rebuttal (pairwise critiques)
  → chief synthesis → merge or escalate → done
```

Status values: `starting` | `racing` | `cross_review` | `synthesizing` | `done` | `timed_out` | `failed`

## Integration

| System | Role in Race |
|--------|-------------|
| **Superset bridge** | Creates isolated workspaces per node (preferred over raw worktree) |
| **3-Surgeons** | Each node runs local cross-exam before submitting verdict |
| **fleet-verdict** | Verdict packet schema for structured output from each node |
| **fleet-rebuttal** | Pairwise critique protocol used in Step 5 |
| **fleet-send** | Transport layer for all race communication |
| **fleet-dispatch** | Tracked dispatch with lifecycle visibility for race tasks |

## Rules

- **Isolation is sacred** — nodes MUST NOT communicate during racing (Step 3)
- **No peeking** — nodes do not see other branches until cross-rebuttal
- **Dissent preserved** — disagreements surface to the human, never suppressed
- **Evidence required** — verdicts without test results or measurements are rejected
- **Timeout enforced** — nodes that miss deadline forfeit; race continues with remaining verdicts
- **One race per task** — duplicate races on the same task are rejected
