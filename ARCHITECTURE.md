# Multi-Fleet Architecture

## System Overview

Multi-Fleet is a distributed AI coordination platform for Claude Code sessions running across multiple machines. It provides guaranteed message delivery through a 9-priority self-healing fallback chain, autonomous task dispatch with session context inheritance, and fleet-wide intelligence through a probe-based evidence pipeline.

**By the numbers:** 112 Python modules. 34 MCP tools. 28 skills. 31 commands. 99 test files. ~2,000 test functions.

The system is organized into five architectural layers, each building on the one below it.

---

## Core Layers

### Layer 1: Transport (P0-P8 Cascade)

The transport layer guarantees message delivery across hostile network conditions using a 9-priority channel cascade. Each channel has independent failure detection, and the system tries them in priority order until one succeeds.

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

**Key modules:** `channels.py` (state machine, cascade logic), `nats_client.py` (NATS transport), `self_heal.py` (4-level repair escalation), `protocol.py` (communication invariants).

**Channel state machine:** Each (peer, channel) pair tracks health: HEALTHY -> DEGRADED (1 failure) -> BROKEN (3 failures) -> HEALING (repair initiated) -> HEALTHY (repair succeeds). BROKEN channels are skipped in the cascade. States auto-reset after 15 minutes.

**Self-healing flow:** When P3+ delivers (meaning P1/P2 are broken), the system auto-escalates: L1 log+alert -> L2 send repair instructions -> L3 spawn repair agent via SSH -> L4 surface to human (after 15+ min). Rate limited: 3 escalations/node/hour.

### Layer 2: Protocol (Packets + Registry)

The protocol layer defines the envelope format, packet types, schema validation, deduplication, and security.

**Packet envelope** (`packets.py`): Every message is a `FleetPacket` with: `id` (UUID for dedup), `version` ("fleet/v1"), `type`, `from_node`, `to_node`, `timestamp`, `payload` (opaque dict), `ttl_minutes`. Multi-Fleet routes envelopes but never interprets payloads -- payload schemas are owned by consumers (ContextDNA, 3-Surgeons, etc.).

**Packet registry** (`packet_registry.py`): Extensible type system with JSON Schema validation. Built-in types:

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

**Deduplication** (`DeduplicationFilter`): LRU-based filter (10K capacity) prevents duplicate processing. Every packet ID is checked before handling.

**Security** (`security.py`): HMAC-SHA256 signing on all NATS messages. Constant-time comparison. 5-minute replay window. Keys stored in macOS Keychain (`fleet_nerve_hmac_key`). Session gold sanitization strips sensitive data before publishing.

**Epistemic packets** (`epistemic_packet.py`): Extended packet format carrying evidence metadata -- confidence scores, source provenance, and evidence chain references for the intelligence layer.

### Layer 3: Intelligence (Probes + Synthesis)

The intelligence layer continuously scans fleet and codebase state, scores signals, builds evidence chains, and synthesizes decisions.

**Probe engine** (`probe_engine.py`): Registry-based execution framework. Probes register with category, severity, and trigger conditions. The engine runs probes by category, trigger, or individually, emitting `ProbeFinding` events.

**19 probes across two subsystems:**

*Fleet probes* (`probes/`):
| Probe | What it detects |
|-------|----------------|
| `agent_readiness` | Whether target node can accept work |
| `architectural_hotspots` | Files with high change frequency / coupling |
| `blast_radius` | How many files/systems a change affects |
| `branch_intent` | What a branch is trying to accomplish |
| `dependency_drift` | Version mismatches across nodes |
| `distributed` | Cross-node consistency issues |
| `fleet_health` | Overall fleet liveness and connectivity |
| `hidden_dependency` | Undeclared dependencies in code |
| `injection_prioritizer` | Which context to inject first |
| `invariant_coverage` | How well invariance gates cover the codebase |
| `mirror_drift` | Config/state divergence between nodes |
| `repeated_failure` | Recurring failure patterns |
| `repeated_failure_cluster` | Clustered failures indicating systemic issues |
| `security_surface` | Security exposure in changes |
| `stale_context` | Outdated context that needs refresh |
| `task_diff_mismatch` | Task description vs actual diff divergence |
| `worktree_contamination` | Cross-worktree state leaks |
| `worktree_divergence` | Worktrees drifting from base branch |

*Code scanner probes* (`code_scanner/probes/`): `blast_radius`, `hidden_dependency`, `injection_prioritizer`, `mirror_drift`, `security_surface`, `stale_context` -- deeper static analysis variants.

**Signal scoring** (`probe_engine.py: SignalScorer`): Probe findings are scored by severity, recency, and relevance to produce prioritized signal lists for injection.

**Probe interpreter** (`probe_interpreter.py`): Translates raw probe results into actionable recommendations.

**Probe intensity** (`probe_intensity.py`): Adjusts probe depth based on context -- light scans during active coding, deep scans during idle periods.

**Evidence pipeline** (`evidence_ledger.py`): Hash-chain ledger providing tamper-evident audit trails. Every decision, probe finding, and synthesis result is recorded with cryptographic linking to the previous entry.

**Chief synthesis** (`synthesis.py`): The `ChiefSynthesis` engine aggregates verdicts from multiple nodes, runs pluggable analyzers, computes confidence-weighted decisions, and records results in the evidence ledger. The basic confidence analyzer weighs agreement level, node count, and individual confidence scores.

### Layer 4: Coordination (Liaison + Rebuttal + Leases)

The coordination layer manages multi-node decision-making, resource ownership, and fleet communication flow.

**Fleet liaison** (`liaison.py`): Background communication handler that manages fleet messages without interrupting active sessions. Messages are classified by urgency into escalation levels: SILENT (log only), INJECT (add to context), NOTIFY (alert user), INTERRUPT (break current work). The liaison batches low-priority messages and injects them during natural pauses.

**Cross-machine rebuttal** (`rebuttal.py`, `cross_rebuttal.py`): 7-phase state machine for structured multi-node critique and convergence:
1. **Propose** -- node submits a proposal with scope
2. **Review** -- other nodes examine the proposal
3. **Accept/Rebut** -- nodes accept or provide counter-arguments
4. **Counter-propose** -- alternative proposals surface
5. **Discuss** -- evidence-based discussion
6. **Resolve** -- chief or consensus determines outcome
7. **Archive** -- decision recorded in evidence ledger

Stale proposals auto-expire. Stats track accept/rebut ratios per node.

**Lease management** (`leases.py`): Distributed resource ownership with SQLite-backed state. Lease types: `short_task` (5 min), `long_task` (30 min), `exclusive` (60 min). Workers request leases from chief; chief grants based on availability. Leases auto-expire, can be renewed or released early. Prevents conflicting work on shared resources.

**Productive waiting** (`productive_wait.py`): When a session goes idle, the system automatically discovers and executes fleet backlog items. Channel repair takes priority over plan items. Ensures no node is ever truly idle.

**Race orchestrator** (`race_orchestrator.py`, `fleet_race.py`): Parallel execution of competing approaches across nodes, with the first successful result winning.

**Branch convergence** (`branch_convergence.py`): Manages merging work from multiple nodes back to a shared branch with conflict detection and resolution.

### Layer 5: Presentation (Dashboard + IDE Bridge)

The presentation layer surfaces fleet state to humans and IDE extensions.

**5-zone dashboard** (`dashboard.py`, `dashboard_html.py`, `dashboard_data.py`):
1. **Fleet overview** -- node count, health summary, uptime
2. **Node details** -- per-node status, active tasks, channel health
3. **Communication** -- message flow, channel usage, delivery stats
4. **Tasks** -- active/pending/completed work items
5. **Evidence** -- recent decisions, probe findings, synthesis results

Dark theme HTML with auto-refresh. Available at the daemon HTTP endpoint.

**IDE bridge** (`ide_bridge.py`): Translates fleet events into IDE-consumable formats. Provides:
- Status bar data (compact fleet health summary)
- Activity bar data (detailed node/task/message panels)
- Pending notifications (alerts requiring user attention)
- Event formatting for VS Code, Cursor, and other IDE extensions

**EventBus** (`eventbus.py`, `event_bus_server.py`): Publish/subscribe event system. Components emit events (probe findings, message deliveries, state changes); consumers subscribe to event types. The event bus server exposes SSE (Server-Sent Events) for real-time streaming to dashboards and IDE extensions.

**Event stream** (`event_stream.py`): SSE endpoint for real-time fleet state pushed to all consumers.

**Status aggregator** (`status_aggregator.py`): Collects and merges status from all nodes into a unified fleet-wide view.

**Theater dashboard** (`theater_dashboard.py`, `theatrical.py`): Dramatic visualization of fleet operations -- probe hunts, synthesis decisions, and rebuttal debates presented as narrative.

---

## Module Map

### Transport (Layer 1)
| Module | Purpose |
|--------|---------|
| `channels.py` | Channel priorities, state machine, cascade logic |
| `nats_client.py` | NATS pub/sub transport |
| `protocol.py` | Communication invariants and protocol rules |
| `self_heal.py` | 4-level repair escalation |
| `discovery.py` | Peer discovery (heartbeat + config) |
| `health_monitor.py` | Continuous health monitoring |
| `supervisor.py` | Daemon process supervisor |
| `supervisor_watchdog.py` | Watchdog for supervisor itself |

### Protocol (Layer 2)
| Module | Purpose |
|--------|---------|
| `packets.py` | FleetPacket envelope dataclass |
| `packet_registry.py` | Type registry, JSON Schema validation, dedup |
| `packet_store.py` | Persistent packet storage |
| `epistemic_packet.py` | Evidence-carrying packet extension |
| `vision_packets.py` | Vision/planning packet types |
| `security.py` | HMAC signing, replay prevention |
| `sanitize.py` | Log and payload sanitization |
| `rate_limit.py` | Per-tool rate limiting |
| `store.py` | SQLite message/peer/WAL storage |

### Intelligence (Layer 3)
| Module | Purpose |
|--------|---------|
| `probe_engine.py` | Probe registry, runner, signal scorer |
| `probe_interpreter.py` | Probe result to recommendation |
| `probe_intensity.py` | Adaptive probe depth |
| `probe_context_factory.py` | Build context dicts for probes |
| `probes/` (19 modules) | Individual probe implementations |
| `code_scanner/` (6 modules) | Static analysis probes |
| `evidence_ledger.py` | Hash-chain tamper-evident ledger |
| `evidence_sync.py` | Cross-node evidence replication |
| `synthesis.py` | Chief verdict aggregation |
| `truth_ladder.py` | Evidence confidence ladder |
| `contextdna_analyzer.py` | ContextDNA integration analysis |
| `ghost_digest.py` | Ghost node detection |
| `ghost_pulse.py` | Ghost node liveness probing |

### Coordination (Layer 4)
| Module | Purpose |
|--------|---------|
| `liaison.py` | Background fleet communication handler |
| `rebuttal.py` | 7-phase proposal/critique state machine |
| `cross_rebuttal.py` | Cross-machine rebuttal orchestration |
| `leases.py` | Distributed resource ownership |
| `productive_wait.py` | Idle work discovery and execution |
| `coordinator.py` | Multi-node task coordination |
| `race_orchestrator.py` | Parallel competing execution |
| `fleet_race.py` | Race primitives |
| `branch_convergence.py` | Multi-node branch merging |
| `hierarchy.py` | Chief/Captain/Worker role assignment |
| `task_state.py` | SQLite-backed task lifecycle |
| `sessions.py` | Session detection and tracking |
| `worker.py` | Autonomous task execution |
| `invariance_gates.py` | Hard gates on send/repair/merge |
| `invariance_rules.py` | Gate rule definitions |
| `acceptance_gate.py` | Pre-dispatch validation |
| `gains_gate.py` | Post-phase health verification |
| `onboarding.py` | New node onboarding flow |

### Presentation (Layer 5)
| Module | Purpose |
|--------|---------|
| `dashboard.py` | Dashboard controller |
| `dashboard_html.py` | HTML rendering (dark theme) |
| `dashboard_data.py` | Dashboard data aggregation |
| `dashboard_events.py` | Dashboard event handling |
| `ide_bridge.py` | IDE extension integration |
| `eventbus.py` | Pub/sub event system |
| `event_bus_server.py` | SSE event streaming server |
| `event_stream.py` | Real-time event stream |
| `event_registry.py` | Event type registry |
| `event_types.py` | Event type definitions |
| `status_aggregator.py` | Fleet-wide status merging |
| `visual.py` | Terminal visualization |
| `theater_dashboard.py` | Theatrical fleet visualization |
| `theatrical.py` | Narrative presentation |
| `node_overlay.py` | Node status overlay |
| `web_dashboard.py` | Web-based dashboard |

### Infrastructure
| Module | Purpose |
|--------|---------|
| `cli.py` | Unified fleet CLI (31 commands) |
| `mcp.py` | MCP protocol integration |
| `mcp_resources.py` | MCP resource definitions |
| `config.py` | Configuration loading and validation |
| `metrics.py` | Prometheus-compatible metrics |
| `prometheus_export.py` | Prometheus metric export |
| `audit.py` | Operation audit logging |
| `backup.py` | Fleet state backup/restore |
| `benchmark.py` | Performance benchmarking |
| `migration.py` | Schema migration |
| `doctor.py` | 6-check diagnostic suite |
| `replay.py` | Message replay for debugging |
| `snapshot_record.py` | Point-in-time state snapshots |
| `data_layer.py` | Data access abstraction |
| `superset_bridge.py` | Superset plugin integration |

---

## Data Flow

```
Probe scan
    |
    v
Signal scoring (severity x recency x relevance)
    |
    v
Evidence recording (hash-chain ledger)
    |
    v
Context injection (liaison batching + IDE bridge)
    |
    v
Chief synthesis (confidence-weighted verdicts)
    |
    v
Decision recording (evidence ledger + audit log)
    |
    v
Learning (repeated failure detection, probe intensity adjustment)
    |
    v
Repeat
```

The cycle is continuous. Probes run on triggers (message received, session idle, task complete) and on schedule (heartbeat interval). Findings accumulate in the evidence ledger, informing future probe intensity and synthesis confidence.

---

## Key Design Decisions

1. **Envelope-only transport.** Multi-Fleet validates the envelope (required fields, types, dedup) but never interprets payload contents. Payload schemas are owned by consumers. This keeps the transport layer stable while consumers evolve independently.

2. **SQLite over Redis for node-local state.** Each node stores messages, peers, WAL, tasks, and leases in SQLite. No external dependencies for basic operation. The fleet works without NATS, without a chief, without any central server.

3. **Channel cascade with health tracking.** Rather than one transport with fallback, the system maintains independent health state per (peer, channel) pair. Broken channels are skipped immediately, keeping delivery latency low even on degraded networks.

4. **Liaison as buffer.** The fleet liaison absorbs the firehose of fleet messages and presents only actionable items to the human. Without this layer, multi-node communication would constantly interrupt active coding sessions.

5. **Evidence-first decisions.** The chief synthesis engine requires evidence (probe findings, node verdicts) before making decisions. Confidence scores propagate through the system, so low-confidence decisions are flagged rather than silently applied.

6. **Productive idle.** Idle detection is a feature, not a bug. When a session goes idle, the system automatically picks up fleet backlog. Channel repair always takes priority over plan items.

7. **Hash-chain audit trail.** Every evidence entry links to the previous via hash chain. This provides tamper-evident history -- if any entry is modified, the chain breaks. Essential for multi-node trust.

---

## Testing

| Metric | Value |
|--------|-------|
| Test files | 99 |
| Test functions | ~2,000 |
| Coverage areas | Transport, protocol, probes, synthesis, rebuttal, leases, liaison, dashboard, IDE bridge, evidence, security, invariance, chaos, stress, E2E pipeline |

Tests include chaos testing (random failures during cascade), stress testing (30-day simulation), and full E2E pipeline tests (probe -> score -> evidence -> synthesize -> inject).
