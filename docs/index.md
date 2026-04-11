# Multi-Fleet

**Cross-machine AI collaboration for Claude Code, Cursor, VS Code, Codex, and Gemini.**

Real-time peer-to-peer messaging with a 9-priority self-healing fallback chain, session-aware autonomous task agents, HMAC-signed communication, and fleet-wide productivity visibility. Messages always deliver -- even when NATS is down, HTTP is blocked, and SSH is your only path.

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

## Why Multi-Fleet?

Multi-Fleet is the first LLM-native fleet coordination system. Every node is independently capable. The fleet continuously self-heals toward ideal state. No central server required for basic operation.

| Aspect | Multi-Fleet | Typical multi-agent frameworks |
|--------|-------------|-------------------------------|
| **Delivery guarantee** | 9-priority fallback chain. Messages deliver even on hostile networks | Single transport. If it fails, message is lost |
| **Self-healing** | Broken channels auto-repair through 4-level escalation | Manual restart required |
| **Session awareness** | Task agents inherit live session context from target machine | Agents start cold with no context |
| **LLM-native** | Built for AI IDE sessions. Hooks, skills, seed files, prompt injection | Generic RPC/message queue adapted for AI |
| **Security** | HMAC signing, replay prevention, log sanitization, keychain storage | Often plaintext or basic auth |
| **Idle productivity** | Idle sessions auto-pick up fleet backlog | Idle = wasted |

## Quick Install

```bash
pip install multifleet
```

Then configure and start:

```bash
export MULTIFLEET_NODE_ID=mac1
fleet status
fleet-nerve
```

See the [Installation Guide](getting-started/install.md) for full setup, or jump to the [Quick Start](getting-started/quickstart.md) for a 5-minute demo.

## By the Numbers

| Metric | Value |
|--------|-------|
| Python modules | 112 across 5 layers |
| MCP tools | 34 |
| Skills | 28 |
| Commands | 31 |
| Test files | 99 |
| Test functions | ~2,000 |
| Communication channels | 9 (P0-P8) |
| Probe engine | 19 probes |

## What's New in v5.0.0

- **NATS clustering** -- each node runs its own nats-server, clustered with all peers. Zero single point of failure.
- **Trip mode** -- one command locks fleet for remote operation: keepalive, API failover, wake-on-LAN, power state management.
- **Probe engine** -- 19 probes with signal scoring, evidence pipeline, and adaptive intensity.
- **Chief synthesis engine** -- pluggable analyzers that aggregate fleet-wide intelligence with confidence-weighted verdicts.
- **IDE bridge** -- status bar, activity bar, and notification integration for VS Code, Cursor, and others.
- **Fleet Liaison Agent** -- background comms handler that manages fleet communication without interrupting active sessions.
- **Cross-machine rebuttal** -- 7-phase state machine for structured multi-node critique and convergence.
- **100-node hierarchy** -- Chief/Captain/Worker roles for scalable fleet organization.
- **Evidence ledger** -- hash-chain integrity for tamper-evident decision audit trails.
