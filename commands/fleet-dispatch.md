---
description: "Dispatch a task to a specific fleet node — with pre-flight safety checks via dispatch gate"
disable-model-invocation: true
---

# /fleet-dispatch

Dispatch a task to a specific fleet node with automatic pre-flight safety checks.

## Usage

```
/fleet-dispatch <node> <task-description>    Dispatch task with safety gate
/fleet-dispatch --force <node> <task>        Skip dispatch gate (use sparingly)
/fleet-dispatch --status <task-id>           Check dispatch status
```

## How It Works

1. Runs the **fleet-dispatch-gate** skill to verify target node readiness and task safety
2. If the gate passes, dispatches the task via the fleet daemon
3. Returns a task ID for tracking

The dispatch gate checks:
- Target node is online and healthy
- Target node has capacity (not overloaded)
- Task description is safe (no destructive ops without confirmation)
- Communication channel is available

## Examples

```bash
# Dispatch a task to mac2
/fleet-dispatch mac2 "Run the test suite and report results"

# Force dispatch (skips gate)
/fleet-dispatch --force mac3 "Quick health check"

# Check status
/fleet-dispatch --status task-abc123
```
