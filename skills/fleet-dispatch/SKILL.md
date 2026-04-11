---
name: fleet-dispatch
description: Remote worker dispatch — send tasks to fleet nodes and track results with priority routing
---

# Fleet Remote Dispatch

Dispatch sends a task prompt to a remote fleet node, spawns a Claude session-driver worker (or `claude -p` fallback), and tracks the result. Unlike `fleet-send` type=task, dispatch provides a tracked lifecycle with status polling.

## HTTP Endpoints

### POST /dispatch — Send a task

```bash
curl -sf -X POST http://127.0.0.1:8855/dispatch \
  -H "Content-Type: application/json" \
  -d '{
    "target": "mac2",
    "prompt": "Run the full test suite for context-dna and report failures",
    "timeout_s": 300,
    "priority": "normal"
  }'
```

Response: `{"dispatch_id": "a1b2c3d4e5f6", "status": "sent", "delivered": true}`

### GET /dispatches — List all dispatches

```bash
curl -sf http://127.0.0.1:8855/dispatches | python3 -m json.tool
```

Returns: `{"dispatches": {"a1b2c3d4e5f6": {"target": "mac2", "prompt": "Run...", "status": "sent", "priority": "normal"}}, "count": 1}`

### GET /dispatch/<id> — Get dispatch result

```bash
curl -sf http://127.0.0.1:8855/dispatch/a1b2c3d4e5f6 | python3 -m json.tool
```

Returns: `{"id": "a1b2c3d4e5f6", "target": "mac2", "status": "completed", "result": "...", "sent_at": ..., "completed_at": ...}`

## Dispatch Lifecycle

```
POST /dispatch → sent → [delivery via send_with_fallback] → target executes → task_result returned → completed
                      → delivery fails → status = "send_failed"
```

Status values: `sent`, `completed`, `send_failed`

## MCP Tool: fleet_worker_dispatch

The MCP server exposes dispatch as `fleet_worker_dispatch`:

```
fleet_worker_dispatch(target="mac2", task="Run tests", timeout="300")
```

This uses `FleetWorkerManager` which prefers session-driver (tmux-isolated) over `claude -p`.

## Dispatch vs fleet-send type=task

| Feature | fleet-send type=task | POST /dispatch |
|---------|---------------------|----------------|
| Tracking | No dispatch ID | Dispatch ID for polling |
| Status | Fire-and-forget | Queryable via GET /dispatch/<id> |
| Result capture | Via task_result message | Stored in `_dispatches` dict |
| Priority | N/A | `normal` or custom |

## Configuration

| Field | Default | Description |
|-------|---------|-------------|
| `timeout_s` | 300 | Max seconds to wait for task completion |
| `priority` | `"normal"` | Task priority (for future queue ordering) |

## When to Use

- Delegating implementation subtasks to other machines
- Running tests or builds on specific hardware
- Any remote work where you need to poll for the result
- Prefer over `fleet-send` when you need delivery confirmation and result tracking
