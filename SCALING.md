# Fleet Scaling Reference

Ops reference for fleet communication at different scales.

## Scale Tiers

| Tier | Nodes | Bottleneck | Action Required |
|------|-------|-----------|-----------------|
| Small | 1-3 | None | Default config works |
| Medium | 4-100 | Chief relay (P3), git commits (P7) | Enable NATS JetStream, enforce P7 rate limits |
| Large | 100-1000 | mDNS discovery, HTTP fanout | NATS clustering, static config fallback, gossip protocol |

## Channel Behavior at Scale

### P1 NATS — O(1) pub/sub
Native horizontal scaling. Single NATS server handles 1000+ nodes. Beyond that, NATS clustering (3-node) with no application changes. JetStream persists messages for offline nodes — they receive on reconnect.

### P2 HTTP — O(1) per message
Point-to-point, no scaling concern per-message. Broadcast via HTTP is O(N) — at 100+ nodes, use NATS broadcast (P1) as primary with async HTTP parallel fallback.

### P3 Chief Relay — single point
Current: single chief node (mac1) relays for offline peers. Scales to ~50 nodes before relay queue depth becomes a concern.

**Raft election plan (100+ nodes):**
- 3 chief-eligible nodes (config: `chief_candidates`)
- Raft leader election via NATS KV (built-in, no external deps)
- Leader handles relay queue; followers replicate WAL entries
- Leader failure: new election in <5s, WAL replay from replicas
- Not implemented yet — current single-chief is fine for <50 nodes

### P7 Git — rate limited
Git commits are expensive (disk I/O, network push, repo bloat). Rate limits enforced in `_send_p7_git()`:

| Limit | Value | Rationale |
|-------|-------|-----------|
| Per-target cooldown | 1 msg / 10 min | Prevents duplicate commits to same node |
| Global hourly cap | 6 commits / hr | Keeps repo history clean |
| Broadcast cooldown | 1 / hr | Single broadcast commit, not N commits |

Rate-limited messages queue in SQLite outbox (`fleet_nerve_store.py`) for delivery via other channels on next retry cycle. P7 is last-resort guaranteed delivery, not primary transport.

## Self-Heal

Random subset probing: each cycle selects min(10, fleet_size) peers at random. Probes P1 (NATS ping) then P2 (HTTP /health). Failed probes escalate through repair channels (P2 HTTP re-register, P3 chief relay, P5 SSH restart). Cooldown: 5 min per action per peer to prevent repair storms.

At 1000 nodes: 10/cycle = full fleet coverage in ~100 cycles (~100 min at 60s interval). Sufficient — healthy nodes self-report via heartbeat; probing catches silent failures only.

## Heartbeat — Gossip Protocol

Each node publishes heartbeat to `fleet.heartbeat` NATS subject every 30s. Heartbeats include: node_id, uptime, load, active_work, sequence number.

**Gossip protocol (fanout=3, TTL=3):**
- Each node forwards received heartbeats to 3 random peers (fanout)
- TTL decrements on each hop — dropped at 0
- Convergence: full fleet state in O(log N) hops
- At 1000 nodes: 3 hops = 27 nodes reached per heartbeat, full convergence in ~3 rounds

Stale detection: no heartbeat for 90s = `suspected`, 300s = `offline`. Offline triggers self-heal probe.

## Discovery

Three layers, checked in order:

1. **mDNS** (`_fleet._tcp.local`) — zero-config LAN discovery. Works for <100 nodes on same subnet. Broadcast storm risk at scale; disable via `FLEET_MDNS=0` for large deployments.
2. **NATS auto-discovery** — nodes publish presence on connect to `fleet.discovery`. Chief maintains peer registry. Scales to NATS server limits (1000+).
3. **Static config fallback** (`config/fleet.json`) — hardcoded peer list. Always works. Required for cross-subnet / cross-network nodes.

## Broadcast

**Primary:** NATS `fleet.all.*` subjects. O(1) publish, NATS handles fan-out to all subscribers. Works at any scale.

**Fallback:** Async HTTP parallel to all known peers. `asyncio.gather()` with per-peer timeout (3s). At 100 nodes: ~3s wall clock. At 1000: still ~3s (parallel), but 1000 outbound connections — set `FLEET_HTTP_BROADCAST_MAX=50` to batch.

## Work Coordination

Distributed backlog via NATS JetStream:

- Work items published to `fleet.work.available` stream
- Nodes claim items via compare-and-swap on NATS KV (`work:<id>` key)
- First writer wins — no distributed locks needed
- Progress updates on `fleet.work.progress` (pub/sub, no persistence needed)
- Completion ACK removes KV entry

SQLite `work_items` table provides local persistence — survives NATS disconnect. On reconnect, node re-publishes in-progress items to prevent duplicate claims.

At 1000 nodes: JetStream handles the load natively. KV compare-and-swap is O(1). No coordination bottleneck.
