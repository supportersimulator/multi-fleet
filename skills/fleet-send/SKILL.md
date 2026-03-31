---
name: fleet-send
description: Send a message to another machine in the fleet — context, task, alert, or broadcast
---

# Fleet Send

Send a message to one or more machines in the fleet. Messages are routed by type — each type has different delivery semantics.

## Usage

### Direct peer-to-peer (preferred)

```bash
# Send context (passive — injected on next prompt)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"context","from":"<this-node>","to":"<target>","payload":{"subject":"...","body":"..."}}'

# Send task (autonomous — spawns agent on target)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"task","from":"<this-node>","to":"<target>","payload":{"subject":"...","body":"..."}}'

# Send alert (interrupt — focuses VS Code)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"alert","from":"<this-node>","to":"<target>","payload":{"subject":"...","body":"..."}}'
```

### Via CLI script

```bash
CHIEF_INGEST_URL=http://<chief-ip>:8844 ./scripts/fleet-send.sh <target> "subject" "body" [priority]
./scripts/fleet-send.sh all "subject" "body"  # broadcast
```

## Message Types

| Type | Delivery | When to use |
|------|----------|-------------|
| `context` | Seed file, next prompt | Enriching another session's context |
| `task` | Spawns `claude -p` agent | Autonomous work on another machine |
| `reply` | Seed file with `ref` | Responding to a specific message |
| `alert` | Focus VS Code + notification | Urgent, needs human attention |
| `broadcast` | All nodes, seed file | Fleet-wide announcements |

## Choosing the Right Type

- **"Pull latest and review"** → `task` (autonomous, no human needed)
- **"FYI: I fixed the auth bug"** → `context` (passive enrichment)
- **"STOP: conflicting verdicts on branch X"** → `alert` (needs attention now)
- **"Everyone pull main"** → `broadcast`
