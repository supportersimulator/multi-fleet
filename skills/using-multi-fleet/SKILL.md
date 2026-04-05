---
name: using-multi-fleet
description: Bootstrap skill — teaches Multi-Fleet cross-machine coordination, fleet roles, transport layer, and when to invoke other skills
---

# Using Multi-Fleet

## What It Is

Multi-Fleet is a cross-machine coordination layer for Claude Code. It enables multiple machines running independent Claude Code sessions to collaborate in real-time through peer-to-peer messaging, autonomous task agents, and fleet-wide visibility.

## Architecture

```
Layer 5: ContextDNA Chief --- authoritative memory, evidence synthesis
Layer 4: Multi-Fleet -------- cross-machine coordination (THIS PLUGIN)
Layer 3: Superset ----------- local parallel execution
Layer 2: 3-Surgeons --------- local truth protocol
Layer 1: Superpowers -------- local captain
```

**Operating rule:** Every machine stays independently capable. The chief machine is preferred for synthesis, not required for basic operation.

## Fleet Roles

| Role | What it does | Example |
|------|-------------|---------|
| **Chief** | Runs Redis, MLX LLM, synthesis. Authoritative memory. Message relay for offline nodes. | mac1 |
| **Worker** | Executes tasks, reports back. Full local capability. Independent when chief is down. | mac2, mac3 |

## Transport Layer (7-Priority Fallback)

Every message tries all channels in order. First success wins.

```
P1: NATS pub/sub    (<100ms)  -- real-time, bidirectional
P2: HTTP direct     (<1s)     -- peer-to-peer, no central dependency
P3: Chief relay     (1-2s)    -- queues for offline nodes
P4: Seed file       (next prompt) -- SSH write to target's seed dir
P5: SSH direct      (2-5s)    -- reliable, works through firewalls
P6: Wake-on-LAN    (10-60s)  -- wake sleeping machines
P7: Git push        (async)   -- guaranteed delivery (eventual)
```

**Self-healing:** When a message succeeds on P3+, it means P1-P2 are broken. The system automatically sends a repair request via the working channel.

## Message Types

| Type | What happens | Disrupts session? |
|------|-------------|-------------------|
| `context` | Seed file, injected on next prompt | No |
| `task` | Spawns autonomous `claude -p` agent | No |
| `reply` | Seed file with ref to original | No |
| `alert` | Focuses VS Code + macOS notification | Yes |
| `sync` | Silent state bookkeeping | No |
| `broadcast` | Seed file on all nodes | No |

## All 11 Skills

| Skill | What It Does | When to Invoke |
|-------|-------------|----------------|
| **using-multi-fleet** | This skill. Architecture overview, role guide, skill index. | First time using multi-fleet, need orientation |
| **fleet-send** | Send a message (context, task, alert, broadcast) to another machine. | Need to communicate with another node |
| **fleet-task** | Dispatch autonomous work to another machine with session-aware agents. | Need work done on another node without human interaction |
| **fleet-status** | Quick health check: who's online, idle, working. | Before dispatching work, checking connectivity |
| **fleet-check** | Run the full 7-channel communication test to a target. | Diagnosing delivery failures, verifying setup |
| **fleet-repair** | 4-level repair escalation for broken channels (notify, guide, assist, remote). | Channels are broken, self-healing triggered |
| **fleet-wake** | Wake a sleeping machine via health check, SSH, or WoL magic packet. | Target node is offline, need it for a task |
| **fleet-tunnel** | SSH tunnel management for restricted networks. | Firewall blocks ports 4222/8844/8855 |
| **fleet-worker** | tmux-isolated worker pool for tasks that shouldn't disrupt interactive sessions. | Running fleet tasks without IDE lag |
| **fleet-watchdog** | Continuous background health monitoring with auto-repair triggers. | Understanding why nodes go offline, tuning thresholds |
| **productivity-view** | Live fleet-wide dashboard showing nodes, agents, backlog, and coordination. | Watching fleet operations in real-time |

## Quick Start

### Step 1: Check fleet health

```bash
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;print(json.load(sys.stdin)['fleetStatus'])"
# -> "3/3 online: mac1 active, mac2 idle 5m, mac3 working"
```

### Step 2: Start live monitoring

```
/loop 1m /fleet-dashboard
```

This makes your screen a live fleet monitor. You see tasks dispatching, agents working, replies arriving. Silent when idle.

### Step 3: Send a message

```bash
# Context message (passive)
curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
  -d '{"type":"context","from":"mac3","to":"mac1","payload":{"subject":"Auth fix","body":"Fixed the token race in middleware.py"}}'

# Task message (spawns autonomous agent)
curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
  -d '{"type":"task","from":"mac3","to":"mac1","payload":{"subject":"Run tests","body":"Run the full test suite and report results"}}'
```

### Step 4: Check task results

```bash
# On the target machine
curl -s http://127.0.0.1:8855/tasks/live | python3 -m json.tool

# Or read the log file
cat /tmp/atlas-agent-results/fleet-task-<id>.log
```

## Common Workflows

### Dispatch work to another machine

1. Check fleet status: is the target online?
2. If offline, wake it: `fleet-wake`
3. Send the task: `fleet-send` with type `task`
4. Monitor progress: `/fleet-dashboard`
5. Read results when complete

### Debug why a message didn't arrive

1. Check fleet status: is the target showing as online?
2. Run the 7-channel test: `fleet-check` against the target
3. Look at which channels passed/failed
4. If P1-P2 fail but P5 works: use `fleet-tunnel` to set up SSH tunnels
5. If all fail: target may be off the network entirely

### Fleet-wide operation (all machines)

```bash
# Wake all nodes, dispatch tasks, monitor progress
bash scripts/fleet-swarm.sh "Your task description here"
```

This handles wake, dispatch, monitoring, and result collection automatically.

## Error Handling Basics

| Symptom | Diagnosis | Skill to Use |
|---------|-----------|-------------|
| `curl: (7) Failed to connect to 127.0.0.1 port 8855` | Local daemon not running | Restart daemon manually or via LaunchAgent |
| Node shows `offline` in fleet-status | Target unreachable | `fleet-wake` then `fleet-check` |
| Message delivered via P5 (SSH) instead of P1 (NATS) | NATS or HTTP broken | `fleet-check` then `fleet-repair` |
| Task agent spawned but no results | Agent may be stuck or timed out | Check `/tasks/live`, read `/tmp/atlas-agent-results/` |
| All nodes show `offline` except self | Network issue or config wrong | Check `.multifleet/config.json` peer entries |

## Key Principles

1. **Additive, not dependent** — fleet communication enriches what each machine can do independently. If the network goes down, every node continues working on its own.
2. **Always deliver** — the 7-priority fallback chain ensures messages reach their target. Even if it takes git push as a last resort.
3. **Session awareness** — task agents inherit context from the target's active session. They don't work blind.
4. **Self-healing** — broken channels are detected and repaired automatically through the repair escalation system.
5. **No disruption** — most message types are non-disruptive. Only `alert` interrupts the target session.
