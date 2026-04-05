---
name: fleet-dispatch-gate
description: HARD-GATE — verify target node readiness and task safety before dispatching work to remote fleet nodes
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# Fleet Dispatch Gate

## Purpose

Dispatching work to a remote node that is offline, busy, or conflicting is worse than not dispatching at all. This gate ensures every remote task dispatch is safe, appropriate, and reversible before it leaves this node.

<HARD-GATE>
Do NOT dispatch tasks to remote nodes until this gate passes. A failed dispatch to an offline node wastes time. A conflicting dispatch to a busy node destroys work.
</HARD-GATE>

## Auto-Invocation Triggers

Invoke this skill when ANY of these signals are present:

| Signal | Examples |
|--------|----------|
| Task targeting another node | `fleet-send`, `fleet-task`, daemon dispatch |
| Remote code execution | Asking another node to run commands, edit files, deploy |
| Multi-node coordination | Work that requires sequencing across 2+ nodes |
| Delegation of implementation | Sending a plan or subtask to a remote Claude session |

**If even ONE signal is present, this gate fires.**

## The 4-Check Protocol

### Check 1: Is the target node online and has an active session?

Query the fleet daemon for node status before dispatching anything.

```bash
# Check node health via daemon
curl -sf http://127.0.0.1:8855/status | python3 -c "
import sys, json
status = json.load(sys.stdin)
for node in status.get('nodes', []):
    print(f\"{node['id']}: online={node.get('online', False)} session={node.get('session_active', False)}\")
"

# Or via NATS heartbeat check
nats sub 'fleet.heartbeat.TARGET_NODE' --count=1 --timeout=5s
```

**If target is offline**: GATE FAILS. Do not dispatch. Options:
- Wake the node first (`fleet-wake`) and re-check
- Queue the task for later delivery
- Execute locally instead

**If target has no active session**: GATE FAILS for interactive tasks. Background tasks may still proceed if the daemon can handle them without a Claude session.

### Check 2: Is the task appropriate for remote execution?

Not all work belongs on a remote node. Evaluate:

| Appropriate for Remote | NOT Appropriate for Remote |
|----------------------|--------------------------|
| Independent subtasks with clear scope | Tasks requiring access to local-only resources |
| Work in the remote node's project focus | Tasks needing real-time back-and-forth with Aaron |
| Parallelizable implementation work | Security-sensitive operations (secrets, auth) |
| Testing/verification of remote systems | Tasks that modify shared state (Redis, git main) |

**If the task requires local resources or shared state**: GATE FAILS. Execute locally.

### Check 3: Could this task conflict with work the target is doing?

Check the target node's current activity before dispatching competing work.

```bash
# Check what the target is currently working on
curl -sf http://127.0.0.1:8855/status | python3 -c "
import sys, json
status = json.load(sys.stdin)
for node in status.get('nodes', []):
    if node['id'] == 'TARGET_NODE':
        print(f\"Current task: {node.get('current_task', 'idle')}\")
        print(f\"Focus: {node.get('focus', 'none')}\")
"
```

**Conflict indicators**:
- Target is editing files your task also needs to edit
- Target is on the same git branch your task will modify
- Target is running tests that your task's changes would invalidate
- Target is mid-implementation on a dependent component

**If conflict detected**: GATE FAILS. Options:
- Wait for current work to complete
- Coordinate sequencing (target finishes first, then your dispatch)
- Redesign the task to avoid overlapping files/branches

### Check 4: Is the task reversible if it fails?

Every remote dispatch must have a rollback path. Remote failures are harder to diagnose and recover from than local ones.

| Reversibility | Examples | Gate Decision |
|--------------|---------|---------------|
| **Easy** | New file creation, additive changes, branch work | PASS |
| **Moderate** | Config changes with backup, refactors with tests | PASS with caution |
| **Hard** | Destructive git ops, production deploys, schema migrations | FAIL unless emergency |
| **Impossible** | External API calls with side effects, data deletion | FAIL — execute locally with supervision |

**If task is not reversible**: GATE FAILS. Execute locally where you can monitor and intervene.

## Emergency Bypass Protocol

For urgent fleet operations (node is about to lose work, connectivity window closing):

1. **Declare emergency** — state what is urgent and why bypass is needed
2. **Dispatch with monitoring** — send the task but actively monitor via `fleet-watchdog`
3. **Post-dispatch verification** — run `fleet-post-verification` immediately after completion
4. **Log the bypass** — record why the gate was bypassed for future reference

## Integration

```
task identified → fleet-dispatch-gate → [dispatch to remote node] → fleet-post-verification
```

This gate fires BEFORE any remote dispatch. It pairs with `fleet-post-verification` which fires AFTER remote work claims completion.

## What NOT to Do

- **Do NOT dispatch to a node you have not checked** — "it was online last time" is not verification.
- **Do NOT dispatch conflicting work** — two nodes editing the same files is a merge nightmare.
- **Do NOT dispatch irreversible tasks remotely** — if it cannot be undone, do it locally.
- **Do NOT skip this gate for "simple" tasks** — simple tasks to offline nodes still fail.
