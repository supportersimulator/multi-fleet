---
name: fleet-race
description: Competitive Branch Racing — parallel problem-solving across fleet nodes with cross-review and synthesis
---

# Fleet Race — Competitive Branch Racing

One command triggers parallel work on the same problem across multiple machines. Each node works in an isolated worktree. The fleet formally proposes, rebuts, and converges to the best solution.

## Quick Start

```bash
# Start a race (all available nodes)
fleet_race task="Fix the auth flow"

# Start a race on specific nodes
fleet_race task="Optimize the database queries" nodes="mac1,mac2,mac3"

# Check race progress
fleet_race_status race_id="a1b2c3d4" action="status"

# List all races
fleet_race_status action="list"
```

## HTTP Endpoints

### POST /race — Start a race

```bash
curl -sf -X POST http://127.0.0.1:8855/race \
  -H "Content-Type: application/json" \
  -d '{
    "task": "Fix the auth flow",
    "nodes": ["mac1", "mac2", "mac3"],
    "timeout_s": 600
  }'
```

Response: `{"race_id": "a1b2c3d4", "status": "racing", "participants": {...}}`

### GET /race/<id> — Race status

```bash
curl -sf http://127.0.0.1:8855/race/a1b2c3d4
```

### GET /races — List all races

```bash
curl -sf http://127.0.0.1:8855/races
```

### POST /race/<id>/submit — Submit node result

```bash
curl -sf -X POST http://127.0.0.1:8855/race/a1b2c3d4/submit \
  -H "Content-Type: application/json" \
  -d '{
    "node": "mac2",
    "result": {
      "summary": "Fixed auth by replacing JWT validation",
      "findings": "Root cause was expired key rotation",
      "branch": "race/a1b2c3d4/mac2"
    }
  }'
```

## Race Lifecycle

```
fleet_race "task" → broadcast race_start → acquire leases → dispatch to nodes
    → each node works in isolated worktree (racing)
    → first completion triggers cross-review (proposals sent for rebuttal)
    → remaining nodes complete → synthesis runs
    → winner selected → evidence recorded → done
```

Status values: `starting`, `racing`, `cross_review`, `synthesizing`, `done`, `timed_out`, `failed`

## How It Works

1. **Broadcast**: Sends `race_start` packet to all participants
2. **Lease**: Acquires a lease per node to prevent duplicate work
3. **Dispatch**: Sends task via Superset bridge (isolated workspace) or fleet message fallback
4. **Race**: Each node works independently on the same problem
5. **Cross-review**: First completion triggers proposal to other nodes for rebuttal
6. **Synthesis**: Compares all approaches, picks winner, records dissent
7. **Evidence**: Every step recorded in tamper-evident evidence ledger

## When to Use

- Exploring multiple solution approaches to a hard problem
- Validating an approach by having other nodes independently solve it
- Finding the fastest/cleanest solution across different machines
- Any task where diversity of approach produces better outcomes
