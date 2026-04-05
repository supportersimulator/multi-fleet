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

## <HARD-GATE> Communication Protocol Invariants

**These are non-negotiable for ANY multi-fleet installation. Violating any invariant is a system failure.**

---

### 1. CHANNEL PRIORITY ORDER (mandatory sequence)

Every message MUST attempt delivery through ALL channels in this exact order. Never stop at first failure. Never skip channels. Never reorder.

```
P1: NATS pub/sub    (2s budget)   -- real-time, bidirectional
P2: HTTP direct     (3s budget)   -- peer-to-peer, no central dependency
P3: Chief relay     (3s budget)   -- queues for offline nodes
P4: Seed file       (2s budget)   -- SSH write to target's seed dir
P5: SSH direct      (8s budget)   -- reliable, works through firewalls
P6: Wake-on-LAN    (3s budget)   -- wake sleeping machines
P7: Git push        (10s budget)  -- guaranteed delivery (eventual)
P8: Direct text injection (LAST RESORT ONLY) -- into user's typing space
```
## Communication Protocol (8-Priority Fallback)

Every message tries all channels in order. First success wins. 30s timeout per channel.

| Priority | Channel | Latency | Description |
|----------|---------|---------|-------------|
| **P0** | Cloud relay | <200ms | AWS/cloud bridge — works across networks |
| **P1** | NATS pub/sub | <100ms | Real-time, bidirectional |
| **P2** | HTTP direct | <1s | Peer-to-peer, no central dependency |
| **P3** | Chief relay | 1-2s | Queues for offline nodes |
| **P4** | Seed file | next prompt | SSH write to target's seed dir |
| **P5** | SSH direct | 2-5s | Reliable, works through firewalls |
| **P6** | Wake-on-LAN | 10-60s | Wake sleeping machines |
| **P7** | Git push | async | Guaranteed delivery (eventual) |

**User input injection is LAST PRIORITY ONLY** — the system exhausts all automated channels before falling back to human-visible seed files or git.

**Secrets**: Keychain/AWS/env vars only. Never hardcode credentials in config or messages. SSH keys via `~/.ssh/`, API keys via env.

**Total budget: 30 seconds.** P8 (direct text injection into a user's active typing space) is ALWAYS last priority — it disrupts flow and is only acceptable when P1-P7 have all failed.

---

### 2. SELF-HEALING AFTER DELIVERY (automatic, not optional)

When a message succeeds on P3 or lower, the higher-priority channels (P1, P2) are broken. The system MUST:

1. **Immediately** use the working channel to send a repair request to the target node
2. The target node runs channel diagnostics and attempts to restore P1/P2
3. Fleet converges to all-green within minutes of any degradation
4. Repair attempts are logged with timestamps and outcomes

This is automatic — not a suggestion, not a best-effort. Every fallback delivery triggers self-heal. If self-heal fails, escalate via `fleet-repair` skill (4-level escalation: notify → guide → assist → remote).

---

### 3. IDLE PROTOCOL (mandatory for all fleet nodes)

When a fleet node has been idle >5 minutes with no active task, it MUST:

1. **Check for pending fleet messages** — process any queued context, task, or reply messages
2. **Review plan documents for unfinished work** — scan `docs/plans/` and active task registries
3. **Explore older plans and designs for opportunities** — look for deferred improvements, stale TODOs, enhancement ideas that align with current capabilities
4. **Report findings to fleet** — broadcast a summary of discoveries or completed opportunistic work

The fleet is proactively productive, not just reactive. Idle time is exploration time. No node should sit silent when there is discoverable work.

---

### 4. SECRETS INVARIANT (zero exceptions)

| Secret Type | Lookup Order | Never |
|-------------|-------------|-------|
| **API keys** | `$ENV_VAR` → macOS Keychain → AWS Secrets Manager | Never in code, never in config files |
| **IPs** | `.multifleet/config.json` only | Never hardcoded in scripts or skills |
| **Usernames** | `.multifleet/config.json` or `$USER` | Never hardcoded |

If a key is loaded from a fallback source (Keychain instead of env var, AWS instead of Keychain): **flag it in the delivery log** and recommend migration to the ideal source. Silent fallback without flagging is a violation.

---

### 5. COMMUNICATION INVARIANCE (every message, every time)

Every single message delivery attempt MUST satisfy ALL of the following:

- [ ] **Try all channels in priority sequence** — never stop at first failure, never skip
- [ ] **Log which channel succeeded** and total elapsed time
- [ ] **Trigger self-heal** for any broken channels discovered during the attempt
- [ ] **Complete within 30s total budget** — if budget exhausted, log failure with channel-by-channel timing
- [ ] **Return delivery receipt** — channel used, latency, any channels that failed

A message delivery that does not satisfy all five conditions is **not a valid delivery** regardless of whether the payload reached the target.

---

### </HARD-GATE>

## Self-Healing Invariant

> **The fleet MUST converge to ideal communication state.**

This is the core principle. Every session checks channel health on start. Every message delivery that falls through to non-ideal channels triggers background healing. The only acceptable steady state is **all channels working on all nodes**.

Concretely:
- **Session start**: daemon probes all peers on P0-P2. Failures logged + repair queued.
- **Message fallthrough**: delivery on P3+ triggers `fleet-repair` via the working channel.
- **Periodic health**: watchdog runs every 30s, auto-escalates through 4 repair levels (notify → guide → assist → remote).
- **Convergence check**: `fleet-check` against all peers verifies full chain. Run after any network change.

## Message Types

| Type | What happens | Disrupts session? |
|------|-------------|-------------------|
| `context` | Seed file, injected on next prompt | No |
| `task` | Spawns autonomous `claude -p` agent | No |
| `reply` | Seed file with ref to original | No |
| `alert` | Focuses VS Code + macOS notification | Yes |
| `sync` | Silent state bookkeeping | No |
| `broadcast` | Seed file on all nodes | No |

## All 12 Skills

| Skill | What It Does | When to Invoke |
|-------|-------------|----------------|
| **using-multi-fleet** | This skill. Architecture overview, role guide, skill index. | First time using multi-fleet, need orientation |
| **fleet-protocol** | CORE — self-healing communication invariant, channel priority (P0-P7), repair escalation, idle productivity, secrets protection. | Understanding how fleet communication works, debugging delivery, adding channels |
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

## Communication Protocol (INVARIANT)

The fleet uses a P0-P8 channel cascade with self-healing. Every session MUST follow this protocol.

### Channel Cascade

Messages try channels in strict priority order. 30-second timeout per channel. First success wins.

```
P0: In-process (same machine)     -- instant
P1: NATS pub/sub    (<100ms)      -- real-time, bidirectional
P2: HTTP direct     (<1s)         -- peer-to-peer, no central dependency
P3: Chief relay     (1-2s)        -- queues for offline nodes
P4: Seed file       (next prompt) -- SSH write to target's seed dir
P5: SSH direct      (2-5s)        -- reliable, works through firewalls
P6: Wake-on-LAN    (10-60s)      -- wake sleeping machines
P7: Git push        (async)       -- guaranteed delivery (eventual)
P8: Direct text     (manual)      -- LAST RESORT — copy/paste between sessions
```

**P8 direct text is LAST RESORT.** Never skip to P8. Always try P1-P7 first.

### Self-Healing Protocol

When a message succeeds on P3+, it means P1 and/or P2 are broken. The system MUST:

1. Deliver the message via the working channel (immediate)
2. Trigger repair for the broken P1/P2 channels (automatic)
3. Spawn background repair agents if needed (via `fleet-repair`)

**The fleet is not healthy until ALL nodes have P1+P2 operational.** A message that delivers via P5 is a success AND a symptom. Treat both.

### Idle Protocol

Any session idle for >5 minutes MUST pick up fleet work:
1. Check for pending fleet tasks in the inbox
2. If no tasks, check channel health to all peers
3. If any channels are broken, run repair
4. If everything is healthy, remain available for dispatch

### Channel State Inspection

```bash
# Check channel state for all peers
curl -s http://127.0.0.1:8855/channels | python3 -m json.tool

# Check specific peer
curl -s http://127.0.0.1:8855/channels/<peer-id> | python3 -m json.tool
## Idle Protocol

**When peers are idle**: suggest productive work from `docs/plans/`. Check backlog, propose next tasks, dispatch low-priority work to idle nodes.

**When YOU are idle**: study the big picture, check fleet health, propose improvements. Never just say "waiting" — always be productive:
- Run `fleet-check` against all peers
- Review `docs/plans/` for unstarted work
- Check `/tasks/live` for stuck agents
- Propose fleet improvements or optimizations

## Plugin Ecosystem

Multi-Fleet works alongside two other plugin systems. They complement each other:

| Plugin | Role | What It Does |
|--------|------|-------------|
| **Superpowers** | Process discipline | brainstorming → writing-plans → executing-plans → verification |
| **3-Surgeons** | Truth verification | sentinel for blast radius, cross-exam for decisions, gains-gate for quality |
| **Multi-Fleet** | Cross-machine coordination | distribute execution, aggregate results, fleet-wide visibility |

**How they interact:**
- Superpowers plans the work locally → Multi-Fleet distributes it across machines
- 3-Surgeons sentinel checks blast radius BEFORE fleet-wide operations
- Cross-exam validates architectural decisions that affect multiple nodes
- Each node runs its own Superpowers + 3-Surgeons independently; Multi-Fleet coordinates between them

## Quick Start for New Installs

### Step 1: Install the plugin

```bash
claude plugin add multi-fleet   # or clone into ~/.claude/plugins/
```

### Step 2: Configure fleet identity

```bash
# Create .multifleet/config.json in your project root
# Set your node ID:
export MULTIFLEET_NODE_ID=$(hostname -s | tr '[:upper:]' '[:lower:]')
# Edit .multifleet/config.json — add your node to the "nodes" object
```

### Step 3: Start the daemon

```bash
# The daemon runs on port 8855 — manages all message routing
node multi-fleet/bin/fleet-nerve-mcp  # or via LaunchAgent for persistence
```

### Step 4: Verify connectivity

```bash
bash scripts/fleet-test-fallback.sh          # Test all channels to all peers
bash scripts/fleet-test-fallback.sh mac1     # Test channels to specific node
```

### Step 5: Set up monitoring

```
/loop 1m /fleet-dashboard
```

Live fleet monitor — shows tasks dispatching, agents working, replies arriving. Silent when idle.

### Step 6: Verify health

```bash
curl -s http://127.0.0.1:8855/health | python3 -m json.tool
curl -s http://127.0.0.1:8855/fleet/live | python3 -m json.tool
```

## Key Principles

1. **Additive, not dependent** — fleet communication enriches what each machine can do independently. If the network goes down, every node continues working on its own.
2. **Always deliver** — the P0-P8 channel cascade ensures messages reach their target. Even if it takes git push as a last resort. P8 (direct text) only when everything else fails.
3. **Session awareness** — task agents inherit context from the target's active session. They don't work blind.
4. **Self-healing** — broken channels are detected and repaired automatically. When P3+ is used, repair of P1/P2 triggers immediately. The fleet is not healthy until ALL nodes have P1+P2.
5. **No disruption** — most message types are non-disruptive. Only `alert` interrupts the target session.
6. **Idle sessions work** — sessions idle >5min pick up fleet tasks and channel repairs. No wasted compute.
2. **Always deliver** — the 8-priority fallback chain ensures messages reach their target. Direct text injection is always last resort. See HARD-GATE section.
3. **Session awareness** — task agents inherit context from the target's active session. They don't work blind.
4. **Self-healing is mandatory** — broken channels are detected and repaired automatically. Not best-effort — invariant. See HARD-GATE section.
5. **No disruption** — most message types are non-disruptive. Only `alert` interrupts the target session. P8 (text injection) only when P1-P7 exhausted.
6. **Idle = explore** — no node sits silent when there is discoverable work. See HARD-GATE Idle Protocol.
7. **Secrets never in code** — env var → Keychain → AWS. Fallbacks flagged. See HARD-GATE Secrets Invariant.
2. **Always deliver** — the 8-priority fallback chain ensures messages reach their target. Even if it takes git push as a last resort.
3. **Session awareness** — task agents inherit context from the target's active session. They don't work blind.
4. **Self-healing** — broken channels are detected and repaired automatically. The fleet MUST converge to all-channels-working state.
5. **No disruption** — most message types are non-disruptive. Only `alert` interrupts the target session.
6. **Never idle** — idle machines study plans, check health, propose improvements. Idle is wasted capacity.
