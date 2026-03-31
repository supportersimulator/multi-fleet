---
name: productivity-view
description: Live fleet-wide productivity dashboard showing what every machine and agent is doing in real-time — the power of the fleet made visible
---

# Productivity View

Live visibility into what your entire fleet of Claude Code instances is doing right now. Not a summary — a real-time window into active work, running agents, backlog progress, and cross-machine coordination.

## When to Invoke

- **Always**: Set up via `/loop 1m /fleet-dashboard` for continuous monitoring
- **On demand**: When you want to see what the fleet is doing right now
- **After dispatching work**: To watch task agents execute across machines

## How It Works

The Fleet Nerve daemon on each machine (port 8855) exposes live APIs. This skill reads them and renders a productivity dashboard that shows the actual state of every node, every agent, every work item.

## Dashboard Sections

### 1. Node Identity & Context
Shows which machine you're on, its role (Chief/Worker), current git branch, and active sessions.

### 2. Current Work
What this node is actively working on — task titles, priorities, elapsed time. Pulled from the work coordination API (`/work`).

### 3. Agents in the Field
Autonomous `claude -p` task agents spawned by fleet messages. Shows:
- Task ID and running state
- Output size (how much work produced)
- Last line of output (what it just did)
- Completion status and duration

### 4. Recently Completed
Tasks that just finished — success or failure, with the final output line for quick triage.

### 5. 3-Surgeons Status
Health of the local surgical team — Cardiologist (external LLM), Neurologist (local LLM), Atlas (this session). All three must be reachable for full capability.

### 6. Fleet Activity
Every node in the fleet: online/offline, idle time, what they're working on. One line per machine — instant situational awareness.

### 7. Fleet Work in Progress
All active work items across ALL machines, sorted by priority. Shows which node owns each item and how long it's been running.

### 8. Backlog Progress
Visual progress bar of the fleet's work queue: how many items are pending, active, and done. The backlog shrinks as agents complete work.

### 9. Commentary
One-line narration of what just happened — task completions, new agents spawned, nodes coming online. Makes the dashboard feel alive.

## Setup

### Step 1: Ensure Fleet Nerve daemon is running

```bash
# Check if daemon is up
curl -s http://127.0.0.1:8855/health | python3 -m json.tool

# If not running, start it
MULTIFLEET_NODE_ID=$(hostname -s) python3 tools/fleet_nerve_daemon.py --port 8855 &
```

### Step 2: Start the productivity monitor

Use the `/loop` command to poll every minute:

```
/loop 1m /fleet-dashboard
```

Or manually:

```bash
curl -s http://127.0.0.1:8855/dashboard
```

### Step 3: Interpret the output

| Icon | Meaning |
|------|---------|
| :hospital: | Chief node (Redis, MLX, synthesis) |
| :zap: | Worker node (execution) |
| :microscope: | Worker node (execution, alternate) |
| :hourglass_flowing_sand: | Task in progress |
| :white_check_mark: | Task completed |
| :x: | Task failed |
| :heart: | Cardiologist (external LLM) |
| :crystal_ball: | Neurologist (local LLM) |
| :brain: | Atlas (this session) |

## API Reference

All endpoints on `http://127.0.0.1:8855`:

| Endpoint | What it returns |
|----------|----------------|
| `/dashboard` | Full rendered productivity view (markdown) |
| `/fleet/live` | JSON: all nodes, statuses, tasks, `fleetStatus` summary |
| `/health` | JSON: this node's health, sessions, surgeons, git |
| `/tasks/live` | JSON: active task agents with log tails |
| `/work` | JSON: this node's active work items |
| `/work/all` | JSON: all work items fleet-wide (24h window) |
| `/work/next` | JSON: next unclaimed backlog item |
| `/sessions/gold` | JSON: active session context (what the human is working on) |

## Why This Matters

Without the Productivity View, a fleet of 3+ machines running Claude Code is a black box. You send a task and hope it lands. You don't know if mac2 is idle or buried in work. You can't see the agent that's been running for 10 minutes on mac1.

With it, you see everything: which nodes are online, what they're working on, how the backlog is progressing, and when agents finish. It turns a collection of independent AI sessions into a visible, coordinated fleet.

## Key Principle

**This is not theatrical — it's operational.** Every line in the dashboard maps to a real API call returning real state. The node statuses come from UDP heartbeats. The task logs come from actual `claude -p` process output. The work items come from the coordination database. Nothing is simulated.
