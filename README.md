# Multi-Fleet

**Cross-machine AI collaboration for Claude Code, Cursor, VS Code, Codex, and Gemini.**

Real-time peer-to-peer messaging with a 9-priority self-healing fallback chain, session-aware autonomous task agents, HMAC-signed communication, and fleet-wide productivity visibility. Messages always deliver -- even when NATS is down, HTTP is blocked, and SSH is your only path.

Multi-Fleet is the first LLM-native fleet coordination system. Every node is independently capable. The fleet continuously self-heals toward ideal state. No central server required for basic operation.

```
    "Send this task to mac2"
        |
        v
    +-----------+     P0 Cloud      +-----------+
    |  mac1     | ---- P1 NATS ---->|  mac2     |
    |  (chief)  | ---- P2 HTTP ---->|  (worker) |
    |           | ---- P3 Relay --->|           |
    |  Claude   | ---- P4 Seed --->|  Claude   |
    |  Code     | ---- P5 SSH ---->|  Code     |
    |           | ---- P6 WoL ---->|           |
    |           | ---- P7 Git ---->|           |
    |           | ---- P8 Text --->|           |
    +-----------+                   +-----------+
    First success wins.             Agent spawns with
    Failed channels auto-repair.    session context.
```

---

## What's New in v5.0.0

- **1142+ tests** across the full suite
- **28+ MCP tools** for complete fleet operation via protocol
- **Fleet Liaison Agent** -- background comms handler that manages fleet communication without interrupting active sessions
- **Cross-machine rebuttal** -- 7-phase state machine for structured multi-node critique and convergence
- **Chief synthesis engine** -- pluggable analyzers that aggregate fleet-wide intelligence
- **100-node hierarchy** -- Chief/Captain/Worker roles for scalable fleet organization
- **Invariance gates** -- hard gates on send, repair, and merge operations to enforce safety
- **Productive waiting** -- sessions never idle; auto-discover and execute fleet backlog
- **HTML dashboard** -- dark theme, auto-refresh, live fleet visualization
- **Status aggregator + SSE event stream** -- real-time fleet state pushed to all consumers
- **Unified fleet CLI** -- 15 commands covering all fleet operations
- **Evidence ledger** -- hash-chain integrity for tamper-evident decision audit trails
- **Fleet doctor** -- 6 diagnostic checks for automated health verification

---

## Quick Start

```bash
# 1. Configure your fleet
cp config/config.template.json .multifleet/config.json
# Edit config.json -- add one entry per machine

# 2. Set your node identity and start
export MULTIFLEET_NODE_ID=mac2
python3 bin/fleet_nerve_mcp.py

# 3. Send a message (via MCP tools or direct HTTP)
curl -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d '{"type":"context","to":"mac1","payload":{"body":"Hello from mac2"}}'
```

That's it. The plugin auto-discovers skills, commands, hooks, and agents from `plugin.json`. Self-healing starts immediately.

---

## Features

### Communication

| Feature | Status | Description |
|---------|--------|-------------|
| P0-P8 fallback cascade | Stable | 9-priority delivery chain. Cloud, NATS, HTTP, Chief relay, seed file, SSH, WoL, Git push, direct text. First success wins |
| Self-healing channels | Stable | When P3+ delivers, broken P1/P2 channels auto-repair. 4-level escalation: notify, guide, background agent, SSH remote |
| HMAC message signing | Stable | HMAC-SHA256 on all NATS messages. Peer identity verification, replay prevention (5-min window), macOS Keychain storage |
| ACK protocol with retry | Stable | SQLite WAL for zero message loss. Exponential backoff on failed deliveries. Cross-device WAL replication |
| Message type routing | Stable | 7 message types (alert, task, reply, context, broadcast, sync, repair) with type-aware channel selection |

### Task Dispatch

| Feature | Status | Description |
|---------|--------|-------------|
| Session-aware agents (send_smart) | Stable | Task agents inherit context from target's active session via session historian gold extraction |
| Autonomous task execution | Stable | `claude -p` spawns on target with full context. Works without human interaction. Results return via Fleet Nerve |
| Work coordination | Stable | Fleet-wide task tracking prevents duplicate work. Claim/release/status across all nodes |
| Productive idle | Stable | Sessions idle >5min auto-pick up fleet backlog. Channel repair takes priority over plan items |

### Discovery and Monitoring

| Feature | Status | Description |
|---------|--------|-------------|
| Gossip heartbeat | Stable | UDP heartbeat every 10s with git branch/commit context. Negligible bandwidth at 100+ nodes (~38KB/min) |
| mDNS zero-config discovery | Planned | `_fleet-nerve._tcp` service discovery. Currently: static config + heartbeat-based peer registry |
| VS Code session detection | Stable | 3-method detection (PID files, JSONL mtime, process scan). Knows active vs idle vs closed |
| Proactive watchdog | Stable | Continuous health monitoring with threshold alerts and automatic repair triggers |
| Productivity dashboard | Stable | Live fleet-wide view of nodes, agents, tasks, and backlog in real-time |

### Platform

| Feature | Status | Description |
|---------|--------|-------------|
| Cross-IDE support | Stable | Claude Code (native), Cursor, VS Code, Codex CLI, Gemini. Generated manifests from canonical source |
| 24 skills | Stable | Full fleet operation coverage including invariance gates, chain orchestration, verdicts |
| 18 commands | Stable | CLI-driven fleet operations |
| 2 agents | Stable | Fleet-coordinator (orchestration) and fleet-worker (autonomous execution) |
| 20 MCP tools | Stable | Full fleet operation via MCP protocol (fleet_send, fleet_task, fleet_status, etc.) |
| Per-session seed files | Stable | Messages arrive as `/tmp/fleet-seed-*.md`, injected on next prompt via hook |

### Testing

| Metric | Value |
|--------|-------|
| Test files | 25 |
| Test functions | 557 |
| Test lines | 6,191 |
| Coverage areas | Config, MCP, fallback cascade, self-healing, worker dispatch, heartbeat, HMAC, packets, packet registry, sanitize, store, leases, eventbus, coordinator, channel cascade, dedup, superset bridge, task state, doctor, audit, hooks, seed validation |

---

## Architecture

Multi-Fleet sits at Layer 4 of the 5-layer stack:

```
+------------------------------------------------------------------+
|  Layer 5: ContextDNA Chief                                       |
|  Authoritative memory, evidence synthesis, branch adjudication   |
+------------------------------------------------------------------+
|  Layer 4: Multi-Fleet            <-- this plugin                 |
|  Cross-machine coordination, Fleet Nerve, session awareness      |
+------------------------------------------------------------------+
|  Layer 3: Superset                                               |
|  Local parallel execution (worktrees, agents, concurrent spawn)  |
+------------------------------------------------------------------+
|  Layer 2: 3-Surgeons                                             |
|  Local truth protocol (3 LLMs cross-examine every decision)      |
+------------------------------------------------------------------+
|  Layer 1: Superpowers                                            |
|  Local captain (discipline, skills, workflow invariance)          |
+------------------------------------------------------------------+
```

### Fleet Nerve Daemon

Every machine runs a lightweight daemon (port 8855) with 4 background threads:

```
+-- Fleet Nerve Daemon (port 8855) ----------------------------+
|                                                               |
|  HTTP Server ---- /health, /message, /inbox, /peers, /stats  |
|       |           /sessions/gold, /work, /wal/*, /doctor      |
|       |                                                       |
|  +-- Background Threads ----------------------------------+   |
|  | 1. UDP Heartbeat Sender (10s) -- git-enriched packets  |   |
|  | 2. UDP Heartbeat Listener   -- peer liveness tracking  |   |
|  | 3. Idle Watcher (60s)       -- task suggestions + heal |   |
|  | 4. Outbox Retry (60s)       -- exponential backoff     |   |
|  +--------------------------------------------------------+   |
|                                                               |
|  SQLite Store -- messages, peers, outbox, WAL                 |
|                                                               |
|  Packet Registry -- 7 built-in types (ack, heartbeat,         |
|    lease_request, lease_grant, lease_release, repair,          |
|    sync_hold) with JSON Schema validation                     |
|                                                               |
|  Task State Machine -- durable SQLite-backed task lifecycle   |
|    (pending→claimed→running→done/failed/cancelled)            |
+---------------------------------------------------------------+
```

### Channel State Machine

Every (peer, channel) pair has exactly one state:

```
                 1 failure
  HEALTHY ----------------------> DEGRADED
     ^                               |
     |                               | 2 more failures (3 total)
     |                               v
     |         repair succeeds      BROKEN
     +--------------------------- HEALING <---- repair initiated
```

BROKEN channels are skipped in the cascade to save timeout budget. States auto-reset to HEALTHY after 15 minutes of no failures.

### Self-Healing Flow

```
Message delivers on P3+ (lower priority channel)
  --> Detects: P1/P2 are broken
  --> L1: Log + dashboard alert (immediate)
  --> L2: Send repair instructions via working channel (immediate)
  --> Wait 120s, probe P1/P2
  --> L3: Spawn repair agent on target via SSH (if still broken)
  --> Wait 300s, probe P1/P2
  --> L4: Surface commands to human (only after 15+ min failure)
```

Rate limit: 3 repair escalations per node per hour. Local-first principle: target fixes itself before remote intervention.

---

## Skills Reference

| Skill | Type | Description |
|-------|------|-------------|
| `using-multi-fleet` | Bootstrap | Architecture overview, role guide, skill index |
| `fleet-send` | Core | Send messages (context, task, alert, broadcast) with 9-priority fallback |
| `fleet-task` | Core | Dispatch autonomous session-aware work to another machine |
| `fleet-dispatch` | Core | Remote worker dispatch with priority routing and result tracking |
| `fleet-status` | Core | Quick health check -- who's online, idle, working |
| `fleet-check` | Core | Run full 7-channel communication test to a target node |
| `fleet-repair` | Core | 4-level repair escalation for broken channels |
| `fleet-wake` | Core | Wake sleeping machines via health check, SSH, or WoL |
| `fleet-tunnel` | Core | SSH tunnel management for restricted networks |
| `fleet-worker` | Core | tmux-isolated worker pool -- no interactive session disruption |
| `fleet-watchdog` | Core | Continuous health monitoring with auto-repair triggers |
| `fleet-idle` | Core | Productive idle -- automatic work discovery when nodes are idle |
| `fleet-ack` | Core | Delivery confirmation protocol -- ACK tracking, retry, failure alerting |
| `fleet-security` | Core | HMAC signing, replay prevention, peer validation, session gold sanitization |
| `productivity-view` | Core | Live fleet-wide dashboard of nodes, agents, and backlog |
| `fleet-chain` | Orchestration | Chain orchestration -- multi-step task dependencies with automatic sequencing |
| `fleet-orchestrate` | Orchestration | Parallel scatter-gather, pipeline, fan-out/fan-in across fleet nodes |
| `fleet-verdict` | Consensus | Structured verdict packets for cross-machine 3x3x3 consensus |
| `fleet-rebuttal` | Consensus | 4-phase cross-machine critique cycle converging on chief decision |
| `fleet-protocol` | Invariance | Self-healing communication invariant and background healing agents |
| `fleet-config-gate` | Hard Gate | Verify safety and blast radius before changing fleet configuration |
| `fleet-dispatch-gate` | Hard Gate | Verify target readiness and task safety before dispatching work |
| `fleet-post-verification` | Hard Gate | Verify fleet health after completing work before claiming done |
| `fleet-healer` | Invariance | Spawns background agents that auto-heal broken channels |

---

## Commands

| Command | Description |
|---------|-------------|
| `/fleet-send` | Send a message to a fleet peer |
| `/fleet-status` | Show fleet health summary |
| `/fleet-task` | Dispatch a task to a remote node |
| `/fleet-check` | Run channel diagnostics to a target |
| `/fleet-repair` | Trigger repair escalation |
| `/fleet-wake` | Wake a sleeping machine |
| `/fleet-tunnel` | Manage SSH tunnels |
| `/fleet-watchdog` | Start/stop health monitoring |
| `/fleet-worker` | Manage tmux worker sessions |
| `/fleet-dashboard` | Full fleet productivity dashboard |

---

## Agents

| Agent | Role |
|-------|------|
| `fleet-coordinator` | Orchestrates multi-node work: task decomposition, dispatch, result synthesis |
| `fleet-worker` | Executes dispatched tasks autonomously with session context awareness |

---

## Hooks

| Hook | Trigger | Purpose |
|------|---------|---------|
| `SessionStart` | New/resumed session | Ingest pending fleet messages from inbox |
| `UserPromptSubmit` | Every prompt | Relay fleet awareness into active session |
| `TeammateIdle` | Async rewake | Pick up queued work when session goes idle |
| `Stop` | Session end | Flush outbound message queue |

---

## IDE Support

Multi-Fleet runs natively on 5 IDEs through generated manifests:

| IDE | Config | Install |
|-----|--------|---------|
| **Claude Code** | `plugin.json` (native) | Auto-discovered |
| **Cursor** | `.cursor-plugin/plugin.json` | Copy to `~/.cursor/mcp.json` |
| **VS Code** | `.vscode/mcp.json.example` | Copy to `.vscode/mcp.json` |
| **Codex CLI** | `codex-config.toml.example` | Copy to project root |
| **Gemini** | `gemini-extension.json` | Reference as extension |

Regenerate all manifests from canonical source: `python3 scripts/build_manifests.py`

---

## Communication Protocol

### Channel Priority Table

| Priority | Channel | Timeout | Requirements |
|----------|---------|---------|--------------|
| **P0** | Cloud (RemoteTrigger) | 5s | Cloud API credentials. Explicit invocation or all-fail fallback only |
| **P1** | NATS pub/sub | 3s | NATS server reachable (port 4222) |
| **P2** | HTTP direct | 5s | Target daemon running (port 8855) |
| **P3** | Chief relay | 5s | Chief server running (port 8844) |
| **P4** | Seed file via SSH | 10s | SSH credentials, target awake |
| **P5** | SSH direct execution | 10s | SSH credentials |
| **P6** | Wake-on-LAN | 60s | WoL enabled, MAC address, wired network |
| **P7** | Git push | 30s | Git remote reachable |
| **P8** | Direct text input | 2s | osascript, VS Code focused. Rate limited: 1/30s |

### Message Types

| Type | Channels | Behavior |
|------|----------|----------|
| `alert` | P1 only, 3x retry | Must confirm delivery. macOS notify on failure |
| `task` | P1-P3 | Needs active session. Queues on chief if none |
| `reply` | P1-P2 | Sender waiting. Fast channels only |
| `context` | P1-P4 | Passive enrichment. Any channel works |
| `broadcast` | P1 | Fire-and-forget to all peers |
| `sync` | P1-P3 | Silent bookkeeping |
| `repair` | P1-P5 | Uses whatever works. Critical for self-healing |

### Security

- **HMAC-SHA256** on all NATS messages with constant-time comparison
- **Peer identity verification** -- unknown senders rejected
- **Replay prevention** -- 5-minute timestamp window
- **Key storage** -- macOS Keychain (`fleet_nerve_hmac_key`), env var override for CI
- **Session gold sanitization** -- only safe metadata published (node_id, topic_keywords, idle_s)
- **Log invariant** -- no message bodies, API keys, tokens, or SSH material in logs

Full specification: [COMMS-PROTOCOL.md](COMMS-PROTOCOL.md)

---

## Scaling

| Component | 3 nodes | 100+ nodes | Approach |
|-----------|---------|------------|----------|
| Broadcast | Loop POST (~15ms) | Parallel async HTTP (~50ms) | Pluggable transport |
| Discovery | Static config | Dynamic | mDNS or chief registry |
| Heartbeat | UDP to all (~negligible) | UDP to all (~38KB/min) | Still negligible at 100+ |
| Chief relay | Single instance | Redis cluster | Or gossip protocol (SWIM) |
| Message priority | 4-tier queue | Same | Alerts before heartbeats at any scale |

---

## What Makes This Different

| Aspect | Multi-Fleet | Typical multi-agent frameworks |
|--------|-------------|-------------------------------|
| **Delivery guarantee** | 9-priority fallback chain. Messages deliver even on hostile networks | Single transport. If it fails, message is lost |
| **Self-healing** | Broken channels auto-repair through 4-level escalation | Manual restart required |
| **Session awareness** | Task agents inherit live session context from target machine | Agents start cold with no context |
| **LLM-native** | Built for AI IDE sessions. Hooks, skills, seed files, prompt injection | Generic RPC/message queue adapted for AI |
| **Zero-config discovery** | Heartbeat-based peer registry. Plug in a node, it appears | Manual service registration |
| **Security** | HMAC signing, replay prevention, log sanitization, keychain storage | Often plaintext or basic auth |
| **Idle productivity** | Idle sessions auto-pick up fleet backlog | Idle = wasted |
| **Invariance gates** | Hard gates verify safety before config changes, dispatch, and completion | Ship and hope |

---

## File Layout

```
multi-fleet/
  skills/              24 skill definitions
  commands/            18 command definitions
  agents/              2 agent definitions (coordinator, worker)
  hooks/               4 lifecycle hooks (SessionStart, UserPromptSubmit, TeammateIdle, Stop)
  multifleet/          22 core modules (channels, config, coordinator, discovery, doctor,
                         eventbus, leases, mcp, nats_client, packet_registry, packet_store,
                         packets, sanitize, security, self_heal, sessions, store,
                         superset_bridge, task_state, visual, worker, audit)
  config/              Template config + IDE adapter manifests
  scripts/             Build manifests, setup, utilities
  tests/               557 tests across 25 files (6,191 lines)
  bin/                 fleet-nerve-mcp entrypoint
  package.json         Plugin metadata + MCP server definition
  COMMS-PROTOCOL.md    Canonical communication specification
  INSTALL.md           Per-IDE installation guide
  CHANGELOG.md         Version history
  LICENSE              MIT
```

---

## Requirements

- Python 3.10+
- `nats-py` (`pip install nats-py`)
- SSH key access between nodes
- NATS server on chief node (`brew install nats-server` or `apt install nats-server`)

---

## Documentation

| Document | Contents |
|----------|----------|
| [INSTALL.md](INSTALL.md) | Per-IDE installation for Claude Code, Cursor, VS Code, Codex, Gemini |
| [COMMS-PROTOCOL.md](COMMS-PROTOCOL.md) | Full communication specification: channels, state machine, repair, security, observability |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |

---

## License

MIT. See [LICENSE](LICENSE).
