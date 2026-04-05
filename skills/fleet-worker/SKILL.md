---
name: fleet-worker
description: Session-driver worker system — dispatch tasks to tmux workers without disrupting the interactive session
---

# Fleet Worker

The key capability that solves typing disruption. Instead of spawning `claude -p` agents that compete with your interactive session, fleet-worker runs tasks in isolated tmux sessions. You keep typing while workers handle fleet tasks in the background.

## The Problem

When a fleet task arrives, the default behavior spawns `claude -p` in the foreground or as a subprocess. On the target machine, this can:
- Steal CPU/memory from the interactive session
- Cause VS Code to lag
- Create file conflicts if both sessions edit the same files

## The Solution: tmux Workers

```
Interactive Session (VS Code)     tmux Workers (background)
+-------------------------+       +------------------+
| You keep typing         |       | worker-0: task A |
| No disruption           |       | worker-1: task B |
| Full IDE experience     |       | worker-2: idle   |
+-------------------------+       +------------------+
                                  Isolated. No conflicts.
```

Workers run in detached tmux sessions. They have their own working directories, their own Claude processes, and their own output logs.

## Dispatching to a Worker

### From the local machine

```bash
# Dispatch a task to a local tmux worker
curl -sf -X POST http://127.0.0.1:8855/worker/dispatch -H "Content-Type: application/json" \
  -d '{
    "task": "Run the full test suite and report failures",
    "worktree": true,
    "maxTurns": 25
  }'
```

### From a remote machine

```bash
# Send a task that will be handled by the target's worker pool
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{
    "type": "task",
    "from": "<this-node>",
    "to": "<target-node>",
    "payload": {
      "subject": "Run tests",
      "body": "Run the full test suite and report failures",
      "useWorker": true
    }
  }'
```

Setting `useWorker: true` in the task payload routes it to the tmux worker pool instead of spawning an inline agent.

## Managing Workers

### List workers

```bash
# See all workers and their status
curl -s http://127.0.0.1:8855/worker/status | python3 -m json.tool
```

Response:
```json
{
  "pool": {
    "maxWorkers": 3,
    "active": 1,
    "idle": 2,
    "queued": 0
  },
  "workers": [
    {"id": "worker-0", "status": "running", "task": "Run tests", "elapsed": "45s", "tmuxSession": "fleet-worker-0"},
    {"id": "worker-1", "status": "idle", "lastTask": "Lint check", "completedAt": "5m ago"},
    {"id": "worker-2", "status": "idle"}
  ]
}
```

### Read worker output

```bash
# Stream live output from a worker
tmux capture-pane -t fleet-worker-0 -p | tail -20

# Read completed results
cat /tmp/atlas-agent-results/fleet-worker-0-<task-id>.log
```

### Attach to a worker (interactive debugging)

```bash
# Attach to see what a worker is doing live
tmux attach -t fleet-worker-0

# Detach without killing: Ctrl-b d
```

### Kill a stuck worker

```bash
# Kill a specific worker
curl -sf -X POST http://127.0.0.1:8855/worker/kill -H "Content-Type: application/json" \
  -d '{"workerId": "worker-0"}'

# Kill all workers
curl -sf -X POST http://127.0.0.1:8855/worker/kill-all
```

## Worker Lifecycle

```
Task arrives (useWorker: true)
  → Find idle worker in pool (or queue if all busy)
  → Create/reuse tmux session: fleet-worker-<N>
  → Optionally create git worktree for isolation
  → Inject session gold (what the interactive session is working on)
  → Run: claude -p "<task prompt>" in the tmux session
  → Stream output to /tmp/atlas-agent-results/
  → On completion: mark worker idle, send reply to sender
  → Worktree cleaned up (if temporary)
```

## Git Worktree Isolation

When `worktree: true` is set, the worker creates a temporary git worktree so it can edit files without conflicting with your interactive session:

```bash
# Worker automatically runs:
git worktree add /tmp/fleet-worktree-<id> HEAD
cd /tmp/fleet-worktree-<id>
# ... does its work ...
# On completion, changes are available as a diff or auto-merged
```

## Configuration

In `.multifleet/config.json`:

```json
{
  "worker": {
    "maxWorkers": 3,
    "useWorktree": true,
    "maxTurnsPerTask": 25,
    "taskTimeoutSeconds": 600,
    "resultDir": "/tmp/atlas-agent-results",
    "injectSessionGold": true,
    "tmuxPrefix": "fleet-worker"
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `maxWorkers` | `3` | Maximum concurrent tmux workers |
| `useWorktree` | `true` | Create git worktrees for file isolation |
| `maxTurnsPerTask` | `25` | Claude agent turn limit per task |
| `taskTimeoutSeconds` | `600` | Kill worker after this many seconds |
| `resultDir` | `/tmp/atlas-agent-results` | Where worker logs are written |
| `injectSessionGold` | `true` | Inject interactive session context into worker |
| `tmuxPrefix` | `"fleet-worker"` | Prefix for tmux session names |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FLEET_WORKER_MAX` | Override max workers (useful for low-memory machines) |
| `FLEET_WORKER_NO_WORKTREE` | Set to `1` to disable worktree isolation |

## When to Use This Skill

- Receiving fleet tasks while actively coding (no disruption)
- Running long tasks that shouldn't block the interactive session
- Parallel task execution on a single machine (up to `maxWorkers`)
- When file conflicts are a concern (enable worktree isolation)
- Debugging: attach to a worker's tmux session to see live progress
