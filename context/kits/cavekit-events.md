---
domain: fleet-events
version: 1
status: brownfield
modules:
  - event_schema.py
  - event_emitter.py
  - event_bus.py
  - event_bus_server.py
  - eventbus.py
  - event_registry.py
  - event_types.py
  - event_stream.py
---

# Cavekit: Fleet Event System

## Scope

In-process and cross-process event pub/sub for the multi-fleet IDE integration. Covers typed event emission, wildcard subscriptions, WebSocket and SSE streaming to IDE clients, event schema and registry, and the relationship between the legacy FleetEventBus (eventbus.py) and the new IDEEventBus (event_bus.py).

Does NOT cover: fleet transport channels or NATS messaging (see cavekit-transport), dashboard rendering, or probe/surgeon/evidence business logic that produces events.

## Requirements

### R1: Typed Event Emission with EventType Enum

**Description:** All events in the system conform to a universal EventEnvelope schema with typed event names drawn from an EventType enum. A factory function creates properly structured events with auto-generated IDs, timestamps, and optional quorum signal classification.

**Acceptance Criteria:**
- [ ] EventEnvelope is a dataclass with fields: id, type, ts, fleet_id, node_id, source, session_id, task_id, worktree_id, repo_id, signal_class, payload
- [ ] EventType enum defines all standardized event types across 5 planes: fleet, context, ghost, surgeon, probe, invariant, task, race, health, supervisor, synthesis, review (50+ members)
- [ ] EventSource enum identifies the emitting plane: multi-fleet, contextdna, ghost, 3-surgeons, superpowers, superset, probe-engine
- [ ] create_event() factory accepts EventType, EventSource, node_id, payload, task_id, session_id, signal_class and returns a populated EventEnvelope
- [ ] If signal_class is not provided, it is auto-classified via quorum.signal_class.classify_message (falling back to "tactical" on error)
- [ ] EventEnvelope serializes to/from dict and JSON (to_dict, to_json, from_dict, from_json)
- [ ] Convenience builders exist for common events: probe_finding_event, surgeon_phase_event, ghost_digest_event, task_accepted_event, task_blocked_event, race_event
- [ ] EventEmitter dispatches events to multiple sinks: EventBus WebSocket server (port 8031) and local JSONL log
- [ ] EventEmitter tracks stats: emitted, eventbus_ok, eventbus_fail, logged
- [ ] A module-level singleton emitter is available via get_emitter() / emit() / emit_typed()

**Dependencies:** None (foundational schema)

### R2: Pub/Sub with Wildcard Subscriptions

**Description:** The core EventBus provides in-process pub/sub with fnmatch-style wildcard pattern subscriptions, a ring buffer for recent events, and support for both persistent and one-shot handlers.

**Acceptance Criteria:**
- [ ] on(pattern, handler) subscribes a handler to events matching the pattern; patterns support * and ? wildcards via fnmatch
- [ ] once(pattern, handler) subscribes for exactly one matching event, then auto-unsubscribes
- [ ] off(pattern, handler) removes a specific handler from a pattern
- [ ] emit() creates a ContextDNA Event v1 dict (via event_types.create_event), appends to ring buffer, and dispatches to all matching handlers
- [ ] Handler exceptions are caught per-handler and never crash the bus; errors are counted in stats
- [ ] Async handlers (coroutines) are scheduled via asyncio.ensure_future
- [ ] Ring buffer has configurable max size (default 1000); oldest events are silently dropped when full, and drops are counted
- [ ] recent(limit, pattern) returns newest-first events from the ring buffer, optionally filtered by pattern
- [ ] Stats track: emitted, delivered, dropped, errors
- [ ] IDEEventBus wraps EventBus with per-namespace history rings (fleet, probe, surgeon, panel, evidence, quorum) each with configurable size (default 200)
- [ ] IDEEventBus.history(namespace, limit, pattern) returns namespace-filtered recent events for late-subscriber replay
- [ ] IDEEventBus supports a middleware pipeline: middleware functions receive (event_type, payload, metadata) and can transform or suppress (return None) events before emission
- [ ] IDEEventBus is a thread-safe singleton via IDEEventBus.get()
- [ ] Typed publish helpers exist for each namespace: fleet_peer_online, fleet_health, probe_finding, probe_complete, surgeon_cross_exam_started, surgeon_verdict, panel_registered, evidence_recorded, quorum_proposed, quorum_decided, etc.

**Dependencies:** R1 (events conform to EventEnvelope/Event v1 schema)

### R3: Event Bus Consolidation (Legacy to New)

**Description:** Two event bus systems coexist. The legacy FleetEventBus (eventbus.py) is a producer-only adapter used by 6 consumers (nats_client, supervisor, ide_bridge, liaison, probe_engine, fleet_race). The new IDEEventBus (event_bus.py) is the unified facade used by panel_bus_bridge. The legacy bus can be wired into the new bus via set_core_bus() / attach_fleet_daemon().

**Acceptance Criteria:**
- [ ] FleetEventBus (legacy) emits fleet.* events via HTTP POST to port 8031 or JSONL fallback
- [ ] FleetEventBus supports in-process direct emit when set_core_bus() injects the new EventBus instance (bypasses HTTP)
- [ ] FleetEventBus auto-broadcasts important event types (peer.online, peer.offline, lease.acquired, lease.released, health.degraded, channel.*) to all fleet nodes via daemon /broadcast endpoint
- [ ] FleetEventBus caches EventBus availability for 30s to avoid probing on every call
- [ ] JSONL fallback rotates at 10MB with 2 rotation files
- [ ] IDEEventBus.attach_fleet_daemon(fleet_eventbus) wires the legacy bus to publish through the new unified bus
- [ ] [GAP] 6 modules still import FleetEventBus directly from eventbus.py (nats_client, supervisor, ide_bridge, liaison, probe_engine, fleet_race); these have not been migrated to IDEEventBus
- [ ] [GAP] Two separate event type registries exist: EventType enum in event_schema.py (5-plane architecture types) and EventType dataclass in event_registry.py (GhostScan/fleet types with required_fields validation). These are not unified.
- [ ] [GAP] event_types.py defines a NAMESPACES dict and create_event factory that duplicates functionality in event_schema.py create_event; no single canonical event creation path exists
- [ ] [GAP] No automated test verifies that events emitted through FleetEventBus (legacy) are actually received by IDEEventBus subscribers when wired via set_core_bus()

**Dependencies:** R1 (both buses create events), R2 (new bus provides the unified pub/sub)

### R4: WebSocket and SSE Event Streaming for IDE Panels

**Description:** Events are streamed to IDE clients via WebSocket (port 8031) and HTTP Server-Sent Events (port 8033). The EventBusServer bridges WebSocket clients to the in-process EventBus. A PanelBusBridge subscribes to event namespaces and maintains per-panel data caches for dashboard components.

**Acceptance Criteria:**
- [ ] EventBusServer accepts WebSocket connections on port 8031 with a max of 10 concurrent clients
- [ ] Clients subscribe/unsubscribe to patterns via JSON messages: {"type": "subscribe", "patterns": [...]}
- [ ] Clients publish events via: {"type": "publish", "event": {"type": "...", "payload": {...}}}
- [ ] Matching events are pushed to subscribed clients as: {"type": "event", "event": {...}}
- [ ] WebSocket server exposes HTTP endpoints for non-WS clients: /health (bus stats), /events (recent 50), /stats
- [ ] SSEHandler streams all events as text/event-stream on a separate port (default 8032) with 30s keepalive comments
- [ ] event_stream.py provides a standalone SSE server on port 8033 with: /events (SSE stream with replay of last 50 events), /status (aggregator snapshot), /health, and /extension/* endpoints for VS Code
- [ ] event_stream.py push_event() distributes events to all connected SSE clients and a ring buffer of recent events (max 50)
- [ ] PanelBusBridge subscribes to fleet.*, probe.*, surgeon.*, evidence.*, quorum.* on the IDEEventBus
- [ ] PanelBusBridge maintains per-panel caches (fleet-status, probe-findings, surgeon-feed, evidence-timeline, quorum-pulse) each bounded to 100 events
- [ ] When a panel cache updates, PanelBusBridge emits panel.updated on the bus for auto-refresh
- [ ] get_panel(panel_id) returns cached events newest-first with updated_at timestamp and event_count
- [ ] get_timestamps() returns lightweight per-panel freshness data for cheap client-side polling
- [ ] PanelBusBridge is a thread-safe singleton via PanelBusBridge.get()
- [ ] [GAP] Three separate SSE/streaming implementations exist (SSEHandler in event_bus_server.py on port 8032, event_stream.py on port 8033, and WebSocket on port 8031) with no unified client discovery mechanism

**Dependencies:** R2 (EventBus core for in-process dispatch), R3 (legacy bus wiring for fleet.* events)

## Out of Scope

- NATS pub/sub transport and channel cascade (see cavekit-transport)
- Event persistence beyond JSONL fallback and JetStream (no database storage)
- Dashboard HTML rendering or VS Code extension client code
- Probe, surgeon, evidence, or quorum business logic (they are event producers/consumers, not part of the event system itself)
- Event replay from JSONL logs (write-only currently)
- Authentication or authorization for WebSocket/SSE connections

## Cross-References

- See also: cavekit-transport.md (NATS pub/sub and channel cascade that carries events across nodes)
- event_schema.py EventType enum is consumed by event_emitter.py, event_bus.py typed helpers, and all event-producing modules
- eventbus.py (legacy) FleetEventBus is imported by: nats_client.py, supervisor.py, ide_bridge.py, liaison.py, probe_engine.py, fleet_race.py
- event_bus.py (new) IDEEventBus is imported by: panel_bus_bridge.py
- event_registry.py validate_event() is available for payload validation but usage in production paths is not confirmed
