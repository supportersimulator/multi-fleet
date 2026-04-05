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

## The Two-Channel Principle

Every fleet operation has two distinct channels:

| Channel | Purpose | Target | Example |
|---------|---------|--------|---------|
| **Action** | Execute the operation | Fleet daemon (port 8855) | POST /message, POST /task |
| **Display** | Show results to user | Interactive session | Dashboard render, status table |

Never conflate them. The daemon processes action silently. The session presents results visually. A `/fleet-task` dispatches via action channel, then polls via display channel. A `/fleet-status` only uses display channel. Mixing channels causes double-execution or silent drops.

## Transport Layer — 7-Priority Chain

```
P1: Direct HTTP peer-to-peer (port 8855) ─── fastest, no intermediary
P2: SSH tunnel relay ──────────────────────── restricted networks, encrypted
P3: Chief relay (port 8844) ───────────────── queues for offline nodes
P4: Redis pub/sub ─────────────────────────── broadcast at scale
P5: File-based seed (seed.md) ─────────────── last resort, always works
P6: Clipboard relay ───────────────────────── manual fallback
P7: Email/webhook ─────────────────────────── extreme fallback, external
```

The fleet-check skill tests all 7 channels. The fleet-repair skill escalates through 4 levels when channels fail. The fleet-watchdog monitors channels continuously and auto-triggers repair.

## Message Types

| Type | What happens | Disrupts session? |
|------|-------------|-------------------|
| `context` | Seed file, injected on next prompt | No |
| `task` | Spawns autonomous `claude -p` agent | No |
| `reply` | Seed file with ref to original | No |
| `alert` | Focuses VS Code + notification | Yes |
| `sync` | Silent state bookkeeping | No |
| `broadcast` | Seed file on all nodes | No |

## Gate Trigger Table

<EXTREMELY-IMPORTANT>
If you think there is even a 1% chance a fleet gate should fire, you ABSOLUTELY MUST invoke it. This is not optional.
</EXTREMELY-IMPORTANT>

| Signal | Required Skill | Priority |
|--------|---------------|----------|
| Sending task/message to another machine | **fleet-dispatch-gate** | HARD-GATE |
| Dispatching broadcast to entire fleet | **fleet-dispatch-gate** | HARD-GATE |
| Changing fleet config (roles, ports, transport) | **fleet-config-gate** | HARD-GATE |
| Modifying daemon settings or transport chain | **fleet-config-gate** | HARD-GATE |
| Adding/removing a machine from fleet | **fleet-config-gate** | HARD-GATE |
| About to claim fleet operation "complete" | **fleet-post-verification** | HARD-GATE |
| Worker task finished, reporting results | **fleet-post-verification** | HARD-GATE |
| Channel test failure detected | **fleet-repair** | Escalation |
| Watchdog threshold breached | **fleet-repair** | Escalation |
| Periodic health check (every 10m) | **fleet-watchdog** | Automatic |

## Available Skills (14)

| Skill | When to Use |
|-------|-------------|
| **using-multi-fleet** | Bootstrap — you are reading this now |
| **productivity-view** | See what the fleet is doing — live dashboard |
| **fleet-send** | Send a message to another machine |
| **fleet-status** | Quick check: who's online, who's idle |
| **fleet-wake** | Wake a sleeping or idle machine |
| **fleet-task** | Dispatch autonomous work to another node |
| **fleet-check** | Run the full 7-channel communication test |
| **fleet-repair** | 4-level repair escalation for broken channels |
| **fleet-worker** | Dispatch tmux worker tasks without disrupting session |
| **fleet-tunnel** | SSH tunnel management for restricted networks |
| **fleet-watchdog** | Proactive health monitoring with auto-repair |
| **fleet-dispatch-gate** | HARD-GATE before sending cross-machine operations |
| **fleet-config-gate** | HARD-GATE before changing fleet configuration |
| **fleet-post-verification** | HARD-GATE before claiming fleet operation complete |

## Decision Flowchart

```
Any signal from gate trigger table?
  YES -> Invoke the required skill (HARD-GATE — blocks until gate passes)
  NO  -> Is this a cross-machine operation?
    YES -> Does it change fleet config or topology?
      YES -> fleet-config-gate (mandatory)
      NO  -> fleet-dispatch-gate (mandatory) then fleet-send/fleet-task
    NO  -> Is this a health/monitoring check?
      YES -> fleet-status (quick) or fleet-check (full 7-channel)
      NO  -> Is this a repair situation?
        YES -> fleet-repair (4-level escalation)
        NO  -> Proceed without fleet consultation
```

## Session-Driver Workers

The **fleet-worker** skill enables tmux-based background agents that execute tasks without disrupting the interactive session. Workers are the local equivalent of fleet-task -- same concept, but on the current machine.

**When to use workers:**
- Long-running tasks (test suites, builds, deployments)
- Parallel local work while interactive session continues
- Tasks that need their own Claude Code context

**Worker lifecycle:**
```
Dispatch (fleet-worker) -> tmux session created -> claude -p executes
  -> Results written to /tmp/atlas-agent-results/
  -> Session polls or gets notified on completion
```

**Rules:**
- Max 5 concurrent workers per machine (prevent resource exhaustion)
- Each worker gets a named tmux session (`fleet-worker-{id}`)
- Workers inherit the project's .claude/ config but NOT interactive context
- Results are always written to disk -- never rely on tmux scrollback

## Watchdog

The **fleet-watchdog** skill runs continuous health monitoring. It checks channel availability, node responsiveness, and transport chain integrity on a configurable interval.

**What watchdog monitors:**
- Node heartbeat (is each machine responding?)
- Channel health (which of the 7 transport paths are working?)
- Message delivery rate (are messages arriving within SLA?)
- Worker status (are dispatched tasks completing?)

**Thresholds and escalation:**
```
Green:  All channels healthy, all nodes responding      -> no action
Yellow: 1-2 channels degraded, all nodes responding     -> log warning
Orange: Primary channel down OR 1 node unresponsive     -> trigger fleet-repair L1
Red:    >2 channels down OR chief unreachable           -> trigger fleet-repair L3
```

**Activation:** `/loop 10m /fleet-check` or invoke fleet-watchdog skill directly for persistent monitoring.

## Quick Start

```bash
# 1. Check fleet health
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;print(json.load(sys.stdin)['fleetStatus'])"

# 2. Full 7-channel test
/fleet-check

# 3. Start live monitoring
/loop 10m /fleet-check

# 4. Send a task to another machine
/fleet-task

# 5. Dispatch a local worker
/fleet-worker
```

## Key Principle

Fleet communication is **additive** -- it enriches what each machine can do independently. No machine depends on the fleet to function. If the network goes down, every node continues working on its own. The fleet amplifies; it never constrains.
