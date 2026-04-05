---
name: fleet-orchestrate
description: Orchestrate parallel task execution across multiple fleet nodes — scatter-gather, pipeline, fan-out/fan-in patterns with dedup, monitoring, and result synthesis
---

# Fleet Orchestrate

Coordinate multi-node parallel task execution. This skill teaches orchestration patterns for distributing work across the fleet, monitoring progress, collecting results, and synthesizing findings.

## When to Use

- Splitting a large task across multiple machines for parallel execution
- Running the same analysis on different codebases/modules across nodes
- Pipeline workflows where node B depends on node A's output
- Any task that benefits from multi-machine parallelism

## Prerequisites

- Fleet Nerve daemon running on port 8855 (local + peers)
- At least 2 nodes online (`curl -sf http://127.0.0.1:8855/fleet/live`)
- `fleet-swarm.sh` available at `scripts/fleet-swarm.sh`

---

## fleet-swarm.sh — The Bash Orchestration Path

The primary orchestration tool. Handles wake, dedup, dispatch, monitoring, and result collection in a single invocation.

### Modes

```bash
# Interactive: single task broadcast to all nodes
./scripts/fleet-swarm.sh "Run the full test suite and report failures"

# Auto: parse a plan file into subtasks, round-robin assign to nodes
./scripts/fleet-swarm.sh --auto docs/plans/my-plan.md

# Dry run: show what WOULD be dispatched without sending anything
./scripts/fleet-swarm.sh --dry-run "Analyze authentication middleware"

# Status: show fleet state and active tasks
./scripts/fleet-swarm.sh --status

# Active tasks: show running task agents
./scripts/fleet-swarm.sh --tasks
```

### Auto Mode Plan Format

Auto mode parses markdown plan files. Recognized formats:
- `- [ ] task description` (markdown checkboxes)
- `1. task description` or `1) task description` (numbered lists)
- `- task description` under headers containing "task", "step", "todo", "work", or "phase"

Tasks shorter than 10 characters are skipped. Duplicates are removed.

### What fleet-swarm.sh Does Internally

1. **Phase 1 — Wake Fleet**: Probes all peers via `/health`. Attempts WoL/SSH wake for sleeping nodes.
2. **Phase 2 — Dedup & Dispatch**: Checks `/work/all` for already-claimed tasks. Registers new tasks via `/work/start`. Sends via `fleet_nerve_send.py`.
3. **Phase 3 — Monitor**: Polls every 15s (max 10min). Shows live progress table from `/work/all`. Watches seed file replies.
4. **Phase 4 — Synthesize**: Collects replies, work item results, writes summary to `/tmp/fleet-swarm-<timestamp>/summary.md`.

---

## Orchestration Patterns

### Pattern 1: Scatter-Gather

Send the same task to all nodes, collect all results, synthesize.

**Use when**: You want diverse perspectives or need to search/analyze across multiple codebases.

```bash
# Scatter: same task to all nodes
./scripts/fleet-swarm.sh "Search for all hardcoded secrets and report locations"

# Results land in /tmp/fleet-swarm-<ts>/
# Each node's reply in /tmp/fleet-seed-<node-id>.md
# Synthesized summary in /tmp/fleet-swarm-<ts>/summary.md
```

**Manual scatter-gather** (when you need more control):

```bash
# 1. Probe fleet
FLEET=$(curl -sf http://127.0.0.1:8855/fleet/live)

# 2. Scatter tasks to each online node
for NODE in mac1 mac2 mac3; do
  IP=$(echo "$FLEET" | python3 -c "import sys,json; nodes=json.load(sys.stdin)['nodes']; print(next((n['ip'] for n in nodes if n['name']=='$NODE'),''))")
  [ -z "$IP" ] && continue

  # Register work to prevent duplicates
  curl -sf -X POST http://127.0.0.1:8855/work/start \
    -H "Content-Type: application/json" \
    -d "{\"title\": \"secret-scan-$NODE\", \"node_id\": \"$NODE\"}"

  # Dispatch
  curl -sf -X POST "http://${IP}:8855/message" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"task\",\"from\":\"$(hostname -s)\",\"to\":\"$NODE\",\"payload\":{\"subject\":\"Secret scan\",\"body\":\"Search for hardcoded secrets. Report to /tmp/fleet-worker-results/secret-scan-$NODE.md\"}}"
done

# 3. Gather: poll /work/all until all complete
watch -n 10 'curl -sf http://127.0.0.1:8855/work/all | python3 -m json.tool'
```

### Pattern 2: Pipeline (Sequential Dependency)

Node A produces output, Node B consumes it, Node C consumes B's output.

**Use when**: Tasks have sequential dependencies (analyze -> fix -> verify).

```bash
# Stage 1: Node A analyzes
curl -sf -X POST http://$NODE_A_IP:8855/message \
  -H "Content-Type: application/json" \
  -d '{
    "type": "task", "from": "coordinator", "to": "mac1",
    "payload": {
      "subject": "Pipeline stage 1: analyze auth module",
      "body": "Analyze auth module for vulnerabilities. Write findings to /tmp/fleet-worker-results/pipeline-stage1.md. Include specific file:line references."
    }
  }'

# Stage 2: After stage 1 completes, read its output and dispatch stage 2
# (Monitor /work/all or watch for seed reply, then dispatch)
curl -sf -X POST http://$NODE_B_IP:8855/message \
  -H "Content-Type: application/json" \
  -d '{
    "type": "task", "from": "coordinator", "to": "mac2",
    "payload": {
      "subject": "Pipeline stage 2: fix findings",
      "body": "Read findings from mac1 at /tmp/fleet-worker-results/pipeline-stage1.md (SSH or request via fleet). Apply fixes. Write results to /tmp/fleet-worker-results/pipeline-stage2.md."
    }
  }'
```

**Pipeline monitoring**: Track each stage as a separate work item. The coordinator watches for stage N completion before dispatching stage N+1.

### Pattern 3: Fan-Out / Fan-In

Split a task into N subtasks, dispatch each to a different node, merge results.

**Use when**: A task naturally decomposes into independent subtasks (one module per node, one test suite per node).

```bash
# Best approach: write a plan file and use --auto
cat > /tmp/fan-out-plan.md << 'EOF'
## Tasks
- [ ] Analyze and test the authentication module (context-dna/auth/)
- [ ] Analyze and test the memory module (memory/)
- [ ] Analyze and test the fleet communication layer (tools/fleet_nerve_*)
EOF

./scripts/fleet-swarm.sh --auto /tmp/fan-out-plan.md
# Subtasks are round-robin assigned to online nodes
# Results collected and synthesized automatically
```

**Fan-in synthesis**: After all subtasks complete, the coordinator reads all results from `/tmp/fleet-swarm-<ts>/` and produces a unified report. For complex synthesis, dispatch a final "merge" task to the strongest node.

---

## Work Deduplication via /work/start

Every dispatched task MUST be registered with the daemon's work tracker to prevent duplicate execution.

```bash
# Register before dispatching
curl -sf -X POST http://127.0.0.1:8855/work/start \
  -H "Content-Type: application/json" \
  -d '{"title": "unique-task-name", "node_id": "mac2"}'
# Returns: {"status": "started", "work_id": "...", "node_id": "mac2", "title": "..."}

# Check if a task is already claimed
curl -sf http://127.0.0.1:8855/work/all | python3 -c "
import sys, json
items = json.load(sys.stdin).get('items', [])
claimed = [i for i in items if i.get('title') == 'unique-task-name' and i.get('status') == 'in_progress']
print('claimed' if claimed else 'available')
"

# Mark complete when done
curl -sf -X POST http://127.0.0.1:8855/work/update \
  -H "Content-Type: application/json" \
  -d '{"work_id": "...", "status": "completed", "result_summary": "Found 3 issues, all fixed"}'
```

**Dedup invariant**: `fleet-swarm.sh` checks `/work/all` before every dispatch. If a task title matches an `in_progress` item, it is skipped. This prevents re-dispatching on retry or when multiple coordinators operate concurrently.

---

## Integration with send_smart()

For session-aware routing, use `send_smart` instead of raw HTTP. It probes the target, checks session state, and picks the optimal delivery channel.

```bash
# Via CLI with --smart flag
python3 tools/fleet_nerve_send.py --smart --type task mac2 "Run analysis on auth module"

# send_smart automatically:
# 1. Probes target health
# 2. Checks if target has an active session (for gold injection)
# 3. Routes through the P0-P8 cascade
# 4. Returns delivery status + channel used + routing metadata
```

```python
from tools.fleet_nerve_send import send_smart

result = send_smart("mac2", {
    "type": "task",
    "from": "mac1",
    "to": "mac2",
    "payload": {
        "subject": "Cross-exam auth module",
        "body": "Full cross-examination of authentication middleware."
    }
})
# result: {"delivered": True, "channel": "nats", "routing": "session-aware"}
```

**When to use send_smart vs direct send**:
- `send_smart`: Tasks that benefit from session gold injection (the target's active context enriches the task)
- Direct send: Simple broadcasts, alerts, or tasks where session context is irrelevant

---

## Progress Monitoring

### Live progress table

```bash
# From any node — shows all fleet work items
curl -sf http://127.0.0.1:8855/work/all | python3 -c "
import sys, json, time
items = json.load(sys.stdin).get('items', [])
now = time.time()
print(f'{\"NODE\":<8} {\"TASK\":<45} {\"STATUS\":<12} {\"ELAPSED\":>8}')
for i in items[:20]:
    elapsed = int(now - i.get('started_at', now))
    m, s = divmod(elapsed, 60)
    print(f'{i.get(\"node_id\",\"?\"):<8} {i.get(\"title\",\"?\")[:44]:<45} {i.get(\"status\",\"?\"):<12} {m}m{s:02d}s')
"
```

### Per-node health during orchestration

```bash
# Check active sessions and task agents on each node
curl -sf http://127.0.0.1:8855/fleet/live | python3 -c "
import sys, json
for n in json.load(sys.stdin).get('nodes', []):
    name = n.get('name', '?')
    sessions = n.get('activeSessions', 0)
    tasks = len([t for t in n.get('tasks', []) if t.get('running')])
    print(f'{name}: {sessions} sessions, {tasks} active tasks')
"
```

### Result files

- **fleet-swarm.sh results**: `/tmp/fleet-swarm-<timestamp>/summary.md`
- **Worker results**: `/tmp/fleet-worker-results/<task-name>-<timestamp>.md`
- **Agent results**: `/tmp/atlas-agent-results/fleet-task-<id>.log`
- **Seed replies**: `/tmp/fleet-seed-<node-id>.md`

---

## Error Handling

### Node failure mid-task

A node may go offline, crash, or become unresponsive during task execution.

**Detection**: Monitor `/work/all` — if a task stays `in_progress` beyond timeout (default 600s), it is likely stuck.

**Recovery**:
1. Probe the node: `curl -sf --max-time 5 http://<node-ip>:8855/health`
2. If unreachable: mark work item as failed, reassign to another node
3. If reachable but stuck: kill the worker (`POST /worker/kill`), reassign

```bash
# Mark failed work item
curl -sf -X POST http://127.0.0.1:8855/work/update \
  -H "Content-Type: application/json" \
  -d '{"work_id": "stuck-task-id", "status": "failed", "result_summary": "Node went offline"}'

# Reassign to a different node
curl -sf -X POST http://$ALTERNATE_NODE_IP:8855/message \
  -H "Content-Type: application/json" \
  -d '{"type":"task","from":"coordinator","to":"mac3","payload":{"subject":"Reassigned: original task","body":"Original node failed. Complete this task: ..."}}'

# Re-register work under the new node
curl -sf -X POST http://127.0.0.1:8855/work/start \
  -H "Content-Type: application/json" \
  -d '{"title": "reassigned-task", "node_id": "mac3"}'
```

### Partial results

Some nodes complete, others fail. The swarm still produces value.

**Strategy**:
1. Collect all completed results normally
2. For failed tasks: check if partial output exists in `/tmp/fleet-worker-results/` on the failed node (SSH if reachable)
3. Synthesize with explicit gaps: "mac1: complete, mac2: complete, mac3: FAILED — no results for module X"
4. Optionally reassign failed tasks to idle nodes for retry

### Timeout

`fleet-swarm.sh` polls for 10 minutes (40 polls x 15s). If tasks are still running:

- Results collected so far are saved to `/tmp/fleet-swarm-<ts>/`
- Work items remain `in_progress` in the daemon's tracker
- Re-run `./scripts/fleet-swarm.sh --status` later to check completion
- Or manually poll: `curl -sf http://127.0.0.1:8855/work/all`

### All nodes offline

`fleet-swarm.sh` exits with error if zero nodes are online after wake attempts. Fix: wake machines manually, check network, verify daemon is running on each.

---

## Orchestration Checklist

Before dispatching a swarm:

1. [ ] `fleet-swarm.sh --status` — verify nodes are online
2. [ ] `fleet-swarm.sh --dry-run "task"` — preview what would be dispatched
3. [ ] Check `/work/all` for conflicting in-progress tasks
4. [ ] Ensure task descriptions are maximally specific (target nodes have ZERO shared context)
5. [ ] For auto mode: verify plan file has recognizable task format

After swarm completes:

1. [ ] Read `/tmp/fleet-swarm-<ts>/summary.md`
2. [ ] Check for failed/partial results
3. [ ] Reassign failed tasks if needed
4. [ ] Archive results: `mv /tmp/fleet-swarm-<ts>/ /tmp/fleet-swarm-archive/`

---

## Integration with Other Skills

- **fleet-send**: Underlying transport for all dispatches (P0-P8 cascade)
- **fleet-task**: Single-node autonomous task dispatch (orchestrate wraps this for multi-node)
- **fleet-worker**: tmux worker pool on each node (set `useWorker: true` for isolation)
- **fleet-status**: Pre-flight fleet health check
- **fleet-wake**: Wake sleeping nodes before dispatch
- **fleet-check**: Diagnose broken channels if sends fail
- **fleet-repair**: Auto-triggered when sends fall back to P3+
