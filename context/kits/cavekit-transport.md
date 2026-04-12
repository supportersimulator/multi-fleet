---
domain: fleet-transport
version: 1
status: brownfield
modules:
  - channels.py
  - nats_client.py
  - nats_cluster.py
  - discord_bridge.py
  - discovery.py
  - protocol.py
  - panel_bus_bridge.py
  - event_stream.py
  - rate_limit.py
---

# Cavekit: Fleet Communication Transport

## Scope

Fleet-level node-to-node messaging across a multi-machine cluster. Covers the 10-priority channel cascade (P0-P8), NATS pub/sub with JetStream persistence, peer discovery via heartbeats and UDP gossip, channel health scoring with automatic failover, rate limiting for MCP tool calls, Discord bridge for mobile command, and SSE/HTTP event streaming for IDE consumption.

Does NOT cover: event bus pub/sub semantics (see cavekit-events), probe engine, surgeon protocol, evidence ledger, or any business logic that rides on top of transport.

## Requirements

### R1: Self-Healing Channel Cascade (P0-P8)

**Description:** Messages to a target peer attempt delivery through an ordered list of channels. Each channel has a per-channel timeout. Known-broken channels are skipped. If a lower-priority channel succeeds and higher-priority channels failed, a self-heal repair is triggered for the broken channels.

**Acceptance Criteria:**
- [ ] Channel order is P0_discord, P0_cloud (conditional), P1_nats, P2_http, P2t_tailscale, P3_chief, P4_seed, P5_ssh, P6_wol, P7_git, P8_keystroke
- [ ] Each channel attempt is bounded by a configurable per-channel timeout (default 30s, P0_discord 10s)
- [ ] Channels marked as known-broken for a peer (within a staleness window) are skipped
- [ ] On first successful delivery, the cascade stops and returns the channel name and latency
- [ ] If delivery succeeds on P3+ while P1/P2 failed, an async heal task is triggered for the broken channels
- [ ] P0_cloud is attempted as a fallback after all local channels fail (even when not explicitly requested)
- [ ] P8_keystroke is attempted only after all P0-P7 channels fail, is rate-limited to 1 per 30s, and only works for the local node
- [ ] Every channel attempt records success or failure in the ChannelState tracker
- [ ] Message envelope is auto-populated with id, from, and timestamp if missing
- [ ] If all channels fail, the return value includes a list of per-channel error reasons

**Dependencies:** R3 (peer discovery for IP resolution), R5 (channel scoring for known-broken detection)

### R2: NATS Pub/Sub with Reconnect and Resubscribe

**Description:** The fleet daemon connects to one or more NATS servers with automatic reconnect and resubscription. JetStream provides persistent messaging for offline nodes. Cluster mode supports multi-seed failover.

**Acceptance Criteria:**
- [ ] Connection supports multiple NATS server URLs for cluster failover
- [ ] max_reconnect_attempts is set to infinite (-1) with a 2-second reconnect wait
- [ ] On reconnect, all NATS subscriptions are re-established (direct, broadcast, heartbeat, ack, session historian)
- [ ] Prior subscriptions are unsubscribed before resubscribing to prevent duplicate delivery
- [ ] Token authentication is supported via NATS_TOKEN env var or config.json protocol.natsToken field
- [ ] JetStream stream (FLEET) is created or found on connect with fleet.> subject filter and 24h retention
- [ ] If JetStream setup fails, the daemon continues with core pub/sub (non-fatal degradation)
- [ ] Disconnect and error callbacks log the event and increment error counters
- [ ] NATS cluster config generation produces valid nats-server.conf with cluster routes derived from fleet config
- [ ] [GAP] No test verifies that resubscription after reconnect actually receives messages that were published during the disconnect window

**Dependencies:** None (foundational)

### R3: Peer Discovery and Health Monitoring

**Description:** Peers are discovered through NATS heartbeats (every 10s) and UDP gossip (fanout-based). Unknown peers appearing in heartbeats are auto-added. Heartbeats carry node metadata including session list, VS Code state, git context, and protocol version.

**Acceptance Criteria:**
- [ ] Heartbeats are published every 10s on fleet.heartbeat with node ID, IP, protocol version, session list, VS Code state, and git context
- [ ] Unknown peers discovered via heartbeat are auto-added to the peer registry
- [ ] Protocol version is checked on heartbeat receipt; incompatible peers are logged as warnings but not rejected
- [ ] Fresh heartbeat receipt clears any stale repair state for that peer
- [ ] UDP gossip sends heartbeats to GOSSIP_FANOUT (3) random peers every 10s
- [ ] Gossip messages include a TTL (default 3) that is decremented on each hop; TTL=0 messages are not forwarded
- [ ] Gossip forwarding excludes the sender, origin, and all nodes in the gossip path
- [ ] Known peer IPs are aggregated from both NATS heartbeat data and static config
- [ ] [GAP] No mechanism detects and removes peers that have been permanently decommissioned (stale peer entries persist indefinitely)

**Dependencies:** R2 (NATS connection for heartbeat publishing)

### R4: Rate Limiting and Backoff for Idle Nodes

**Description:** MCP tool calls are rate-limited per tool name using a sliding-window algorithm. Self-heal probes skip vacation and backed-off nodes. Repair actions have per-peer cooldowns.

**Acceptance Criteria:**
- [ ] Each rate-limited tool has a configurable calls-per-minute limit (e.g., fleet_send: 30/min, fleet_broadcast: 10/min, fleet_wake: 3/min)
- [ ] Read-only tools (fleet_status, fleet_probe) have no rate limit
- [ ] The rate limiter uses a 60-second sliding window per tool name
- [ ] Denied calls return a reason string including the tool name and limit
- [ ] The rate limiter tracks allowed/denied counts as observable stats
- [ ] Self-heal cycles skip peers marked as on-vacation or backed-off
- [ ] Repair actions have a 300s (5 min) cooldown per peer per action type
- [ ] [GAP] P8 keystroke rate limit (1/30s) is tracked as a class-level variable rather than per-instance, which could cause issues if multiple CommunicationProtocol instances exist (unlikely but not prevented)

**Dependencies:** R1 (cascade uses repair cooldowns), R5 (self-heal skips based on score)

### R5: Channel Scoring and Automatic Failover

**Description:** Each peer has a per-channel health score derived from success/failure tracking. A standing self-heal loop (every 60s) probes peers and triggers repair when primary channels (P1/P2) are broken. Repair uses escalation levels and verifies fixes by re-probing.

**Acceptance Criteria:**
- [ ] ChannelState tracks per-peer, per-channel: status (ok/broken/unknown), last_success timestamp, last_failure timestamp, latency_ms, and fail_count
- [ ] A channel is considered known-broken only if its last failure was within a configurable staleness window (default 120s)
- [ ] Peer health score = total_channels - broken_channels (max score = len(ALL_CHANNELS))
- [ ] Self-heal loop starts after a 120s startup stabilization period
- [ ] Each self-heal cycle probes a dynamic subset of peers: max(10, sqrt(N)) peers per cycle
- [ ] Broken peers are always included in the probe set; remaining budget is filled with stale-then-fresh peers
- [ ] Self-heal probes P2 HTTP via health endpoint (5s timeout) and P1 NATS via heartbeat recency (45s threshold)
- [ ] If P1 is healthy, repair is skipped (P2 failure alone is tolerated behind routers)
- [ ] Repair is only triggered when both P1 and P2 are broken, subject to the 5-min cooldown
- [ ] Repair messages are sent via the best working channel (cascade order, excluding P7_git to prevent repo flooding)
- [ ] After repair, each previously-broken channel is re-probed to verify the fix; channel state is updated accordingly
- [ ] Channel state transitions (ok->broken, broken->ok) are logged
- [ ] Full channel state for all peers is queryable via get_channel_state() for the /channels HTTP endpoint

**Dependencies:** R1 (cascade used for repair delivery), R3 (heartbeat data for P1 health assessment)

## Out of Scope

- Message content encryption or payload schema validation (transport is content-agnostic)
- Fleet membership authorization or access control beyond NATS token auth
- Specific repair actions (restart_nats, restart_tunnel, etc.) -- those belong to self_heal.py domain
- Event bus pub/sub semantics, middleware, or namespace management (see cavekit-events)
- Dashboard rendering or panel lifecycle
- Probe engine, surgeon protocol, or evidence ledger logic
- Discord bot permissions management or Discord server setup

## Cross-References

- See also: cavekit-events.md (event bus that rides on this transport layer)
- self_heal.py implements the repair actions referenced by R5 repair escalation
- panel_bus_bridge.py bridges transport events to IDE panels (covered in cavekit-events R4)
