# Architecture Overview

Multi-Fleet is a distributed AI coordination platform for Claude Code sessions running across multiple machines. It provides guaranteed message delivery through a 9-priority self-healing fallback chain, autonomous task dispatch with session context inheritance, and fleet-wide intelligence through a probe-based evidence pipeline.

**112 Python modules. 34 MCP tools. 28 skills. 31 commands. 99 test files. ~2,000 test functions.**

## 5-Layer Stack

```
+------------------------------------------------------------------+
|  Layer 5: Presentation                                           |
|  Dashboard, IDE bridge, SSE events, theater visualization        |
+------------------------------------------------------------------+
|  Layer 4: Coordination                                           |
|  Liaison, rebuttal, leases, productive waiting, race orchestrator|
+------------------------------------------------------------------+
|  Layer 3: Intelligence                                           |
|  19 probes, signal scoring, evidence ledger, chief synthesis     |
+------------------------------------------------------------------+
|  Layer 2: Protocol                                               |
|  Packet envelope, type registry, dedup, HMAC security            |
+------------------------------------------------------------------+
|  Layer 1: Transport                                              |
|  P0-P8 cascade, channel state machine, self-healing              |
+------------------------------------------------------------------+
```

See [5 Layers](layers.md) for detailed documentation of each layer.

## Fleet Nerve Daemon

Every machine runs a lightweight daemon (port 8855) with 4 background threads:

```
+-- Fleet Nerve Daemon (port 8855) ----------------------------+
|                                                               |
|  HTTP Server ---- /health, /message, /inbox, /peers, /stats  |
|       |           /sessions/gold, /work, /wal/*, /doctor      |
|                                                               |
|  +-- Background Threads ----------------------------------+   |
|  | 1. UDP Heartbeat Sender (10s) -- git-enriched packets  |   |
|  | 2. UDP Heartbeat Listener   -- peer liveness tracking  |   |
|  | 3. Idle Watcher (60s)       -- task suggestions + heal |   |
|  | 4. Outbox Retry (60s)       -- exponential backoff     |   |
|  +--------------------------------------------------------+   |
|                                                               |
|  SQLite Store -- messages, peers, outbox, WAL                 |
|  Packet Registry -- 7+ built-in types with JSON Schema        |
|  Task State Machine -- durable lifecycle tracking             |
+---------------------------------------------------------------+
```

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

## Key Design Decisions

1. **Envelope-only transport.** Multi-Fleet validates the envelope but never interprets payload contents. Payload schemas are owned by consumers. Transport stays stable while consumers evolve.

2. **SQLite over Redis for node-local state.** Each node stores everything in SQLite. No external dependencies for basic operation. The fleet works without NATS, without a chief, without any central server.

3. **Channel cascade with health tracking.** Independent health state per (peer, channel) pair. Broken channels skipped immediately, keeping delivery latency low on degraded networks.

4. **Liaison as buffer.** Absorbs the firehose of fleet messages and presents only actionable items. Without this, multi-node communication would constantly interrupt coding.

5. **Evidence-first decisions.** Chief synthesis requires evidence before deciding. Confidence scores propagate so low-confidence decisions are flagged, not silently applied.

6. **Hash-chain audit trail.** Every evidence entry links to the previous via hash chain. Tamper-evident history essential for multi-node trust.
