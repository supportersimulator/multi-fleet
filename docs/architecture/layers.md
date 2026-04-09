# 5 Architectural Layers

## Layer 1: Transport (P0-P8 Cascade)

Guarantees message delivery across hostile network conditions using a 9-priority channel cascade.

| Priority | Channel | Mechanism | Timeout |
|----------|---------|-----------|---------|
| P0 | Cloud (RemoteTrigger) | Cloud API | 5s |
| P1 | NATS pub/sub | NATS server (port 4222) | 3s |
| P2 | HTTP direct | Fleet Nerve daemon (port 8855) | 5s |
| P3 | Chief relay | Chief server (port 8844) | 5s |
| P4 | Seed file | SSH + `/tmp/fleet-seed-*.md` | 10s |
| P5 | SSH direct | SSH command execution | 10s |
| P6 | Wake-on-LAN | WoL magic packet | 60s |
| P7 | Git push | `.fleet-messages/` via git | 30s |
| P8 | Direct text | osascript keystroke injection | 2s |

### Channel State Machine

Every (peer, channel) pair tracks health:

```
                 1 failure
  HEALTHY ----------------------> DEGRADED
     ^                               |
     |                               | 2 more failures (3 total)
     |                               v
     |         repair succeeds      BROKEN
     +--------------------------- HEALING <---- repair initiated
```

BROKEN channels are skipped in the cascade. States auto-reset after 15 minutes.

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

Rate limit: 3 repair escalations per node per hour.

**Key modules:** `channels.py`, `nats_client.py`, `self_heal.py`, `protocol.py`

## Layer 2: Protocol (Packets + Registry)

Defines envelope format, packet types, schema validation, deduplication, and security.

### Packet Envelope

Every message is a `FleetPacket` with: `id` (UUID), `version` ("fleet/v1"), `type`, `from_node`, `to_node`, `timestamp`, `payload`, `ttl_minutes`.

### Built-in Packet Types

| Type | Purpose |
|------|---------|
| `ack` | Delivery confirmation |
| `heartbeat` | Liveness signal with git context |
| `lease_request` | Resource ownership request |
| `lease_grant` | Chief grants lease |
| `lease_release` | Voluntary lease release |
| `repair` | Channel repair instruction |
| `sync_hold` | Pause fleet operations for consensus |
| `context` | Passive context enrichment |
| `task` | Work dispatch |
| `alert` | High-priority notification |

### Security

- HMAC-SHA256 on all NATS messages with constant-time comparison
- 5-minute replay window
- macOS Keychain storage (`fleet_nerve_hmac_key`)
- Session gold sanitization strips sensitive data before publishing

**Key modules:** `packets.py`, `packet_registry.py`, `security.py`, `sanitize.py`

## Layer 3: Intelligence (Probes + Synthesis)

Continuously scans fleet and codebase state, scores signals, builds evidence chains, and synthesizes decisions.

### Probe Engine

19 probes across two subsystems (fleet probes and code scanner probes). See [Probe Engine](probes.md) for the full list.

### Signal Scoring

Probe findings are scored by `severity x recency x relevance` to produce prioritized signal lists for context injection.

### Chief Synthesis

Aggregates verdicts from multiple nodes, runs pluggable analyzers, computes confidence-weighted decisions, records in evidence ledger.

### Evidence Ledger

Hash-chain tamper-evident audit trail. Every decision, probe finding, and synthesis result is cryptographically linked to the previous entry.

**Key modules:** `probe_engine.py`, `evidence_ledger.py`, `synthesis.py`

## Layer 4: Coordination (Liaison + Rebuttal + Leases)

Manages multi-node decision-making, resource ownership, and fleet communication flow.

### Fleet Liaison

Background handler that classifies messages by urgency: SILENT, INJECT, NOTIFY, INTERRUPT. Batches low-priority messages and injects during natural pauses.

### Cross-Machine Rebuttal

7-phase state machine: Propose, Review, Accept/Rebut, Counter-propose, Discuss, Resolve, Archive.

### Lease Management

Distributed resource ownership via SQLite. Types: `short_task` (5 min), `long_task` (30 min), `exclusive` (60 min). Prevents conflicting work on shared resources.

### Productive Waiting

Idle sessions auto-discover and execute fleet backlog. Channel repair takes priority.

**Key modules:** `liaison.py`, `rebuttal.py`, `leases.py`, `productive_wait.py`

## Layer 5: Presentation (Dashboard + IDE Bridge)

Surfaces fleet state to humans and IDE extensions.

### 5-Zone Dashboard

1. Fleet overview (node count, health, uptime)
2. Node details (per-node status, tasks, channels)
3. Communication (message flow, delivery stats)
4. Tasks (active/pending/completed)
5. Evidence (decisions, findings, synthesis)

Dark theme HTML with auto-refresh.

### IDE Bridge

Translates fleet events into IDE-consumable formats: status bar, activity bar, notifications, SSE streaming. Works with Claude Code, VS Code, Cursor, Codex, and Gemini.

**Key modules:** `dashboard.py`, `ide_bridge.py`, `eventbus.py`, `status_aggregator.py`
