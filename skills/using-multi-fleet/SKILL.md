---
name: using-multi-fleet
description: Bootstrap skill — teaches Multi-Fleet cross-machine coordination, fleet roles, transport layer, and when to invoke other skills
---

# Using Multi-Fleet

## What It Is

Multi-Fleet is a cross-machine coordination layer for Claude Code. It enables multiple machines running independent Claude Code sessions to collaborate in real-time through peer-to-peer messaging, autonomous task agents, and fleet-wide visibility.

## Architecture

```
Layer 5: ContextDNA Chief ─── authoritative memory, evidence synthesis
Layer 4: Multi-Fleet ──────── cross-machine coordination (THIS PLUGIN)
Layer 3: Superset ─────────── local parallel execution
Layer 2: 3-Surgeons ───────── local truth protocol
Layer 1: Superpowers ──────── local captain
```

**Operating rule:** Every machine stays independently capable. The chief machine is preferred for synthesis, not required for basic operation.

## Fleet Roles

| Role | What it does | Example |
|------|-------------|---------|
| **Chief** | Runs Redis, MLX LLM, synthesis. Authoritative memory. | mac1 |
| **Worker** | Executes tasks, reports back. Full local capability. | mac2, mac3 |

## Transport Layer

```
Primary:   Direct HTTP peer-to-peer (port 8855) — no single point of failure
Secondary: Chief relay (port 8844) — queues for offline nodes
Tertiary:  Redis pub/sub — broadcast at scale
```

## Message Types

| Type | What happens | Disrupts session? |
|------|-------------|-------------------|
| `context` | Seed file, injected on next prompt | No |
| `task` | Spawns autonomous `claude -p` agent | No |
| `reply` | Seed file with ref to original | No |
| `alert` | Focuses VS Code + notification | Yes |
| `sync` | Silent state bookkeeping | No |
| `broadcast` | Seed file on all nodes | No |

## Available Skills

| Skill | When to Use |
|-------|-------------|
| **productivity-view** | See what the fleet is doing — live dashboard |
| **fleet-send** | Send a message to another machine |
| **fleet-status** | Quick check: who's online, who's idle |
| **fleet-wake** | Wake a sleeping or idle machine |
| **fleet-task** | Dispatch autonomous work to another node |

## Quick Start

```bash
# 1. Check fleet health
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;print(json.load(sys.stdin)['fleetStatus'])"

# 2. Start live monitoring
/loop 1m /fleet-dashboard

# 3. Send a task to another machine
curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
  -d '{"type":"task","from":"mac3","to":"mac1","payload":{"subject":"run tests","body":"Run the full test suite and report results"}}'
```

## Key Principle

Fleet communication is **additive** — it enriches what each machine can do independently. No machine depends on the fleet to function. If the network goes down, every node continues working on its own.
