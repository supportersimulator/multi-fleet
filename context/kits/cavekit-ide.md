---
domain: ide-integration
version: 1.0.0
status: brownfield
modules:
  - ide_panels.py
  - ide_bridge.py
  - web_dashboard.py
  - web_server.py
  - dashboard.py
  - dashboard_components.py
  - dashboard_data.py
  - dashboard_html.py
  - dashboard_events.py
  - arbiter_view.py
  - beta_dashboard.py
  - regression_dashboard.py
  - theater_dashboard.py
  - theatrical.py
  - panel_bus_bridge.py
depends_on:
  - cavekit-events
  - cavekit-evidence
  - cavekit-probes
  - cavekit-synthesis
---

# Cavekit: IDE Integration

## Scope

Visual presentation layer for the multi-fleet system. Covers structured JSON panel endpoints, HTTP API routes, browser dashboards (web and terminal), theatrical CLI output, the human arbiter decision interface, and real-time event streaming to IDE extensions and browsers. All modules in this domain consume data from other domains (events, evidence, probes, synthesis) and present it -- they do not own or mutate fleet state.

## Requirements

### R1: IDE Panel Server

**Description:** A server that exposes 6 structured JSON panel endpoints, each returning a standardized envelope (`{panel_id, title, updated_at, data, summary}`). Panels: fleet-status, evidence-timeline, synthesis-decisions, probe-findings, wisdom-feedback, synaptic.

**Acceptance Criteria:**
- [ ] `IDEPanelServer.fleet_status_panel()` returns envelope with `panel_id == "fleet-status"` and `data` containing node dicts with keys: `id`, `status`, `nats_connected`, `role`
- [ ] `IDEPanelServer.evidence_timeline_panel(hours=N)` returns envelope where every item in `data` has keys: `seq`, `entry_id`, `timestamp`, `event_type`, `entry_hash`, `verified`
- [ ] `IDEPanelServer.synthesis_decisions_panel(limit=N)` returns envelope with `data` list length <= N and summary containing `avg confidence`
- [ ] `IDEPanelServer.probe_findings_panel()` returns envelope with summary containing `probes` count and `findings` count
- [ ] `IDEPanelServer.wisdom_feedback_panel()` returns envelope where each item has `type` in (`wisdom`, `gotcha`, `wisdom_entry`) and `source` field
- [ ] `IDEPanelServer.synaptic_panel()` returns envelope with `panel_id == "synaptic"` and summary containing `events processed` count
- [ ] `IDEPanelServer.all_panels()` returns dict with key `panels` containing exactly 6 panel envelopes and a `generated_at` timestamp
- [ ] All panel methods return valid envelope (never raise) when underlying dependencies are unavailable -- `data` is empty list and `summary` indicates unavailability

**Dependencies:** Evidence ledger, probe engine, synthesis engine, wisdom consumer, professor bridge, synaptic bridge (all lazy-loaded)

### R2: HTTP API Routes

**Description:** A web server exposing HTTP routes that serve dashboard HTML, JSON API endpoints, SSE event streaming, IDE extension data, and Prometheus metrics. Binds to configurable port (default 8856).

**Acceptance Criteria:**
- [ ] `GET /` and `GET /dashboard` return HTTP 200 with `Content-Type: text/html`
- [ ] `GET /api/status` returns JSON with fleet health data or `{"status": "offline"}` when daemon is unreachable
- [ ] `GET /api/probes` returns JSON with key `probes` (list) and `count` (int)
- [ ] `GET /api/decisions` returns JSON with key `decisions` (list) and `count` (int)
- [ ] `GET /api/branches` returns JSON with branch convergence data
- [ ] `GET /api/panels/fleet-status` returns IDEPanelServer fleet_status_panel() output
- [ ] `GET /api/panels/evidence-timeline?hours=24` passes `hours` param to evidence_timeline_panel()
- [ ] `GET /api/panels/synthesis-decisions?limit=10` passes `limit` param to synthesis_decisions_panel()
- [ ] `GET /api/panels/probe-findings` returns probe_findings_panel() output
- [ ] `GET /api/panels/wisdom-feedback` returns wisdom_feedback_panel() output
- [ ] `GET /api/panels/all` returns all_panels() output
- [ ] `GET /api/panels/poll` returns PanelBusBridge cached panels with timestamps
- [ ] `GET /api/panels/timestamps` returns only panel timestamps for cheap freshness checks
- [ ] `GET /extension/status-bar` returns JSON with keys: `node_count`, `health_dot`, `text`, `stale`
- [ ] `GET /extension/activity` returns JSON with key `sections` (list of tree-view sections)
- [ ] `GET /extension/notifications` returns list of pending notification dicts
- [ ] `GET /metrics` returns HTTP 200 with `Content-Type` containing `text/plain`
- [ ] `GET /health` returns JSON with `status`, `type`, `port`, `uptime_s`
- [ ] `GET /api/professor/panel` returns professor bridge panel data
- [ ] `GET /api/surgeon-status` returns surgeon adapter availability and counters
- [ ] All JSON endpoints include `Access-Control-Allow-Origin: *` header
- [ ] `start_fleet_server()` returns dict with `status` in (`started`, `already_running`, `error`)
- [ ] `stop_fleet_server()` returns dict with `status` in (`stopped`, `not_running`)
- [ ] Server runs in a background daemon thread (does not block caller)

**Dependencies:** R1 (IDEPanelServer), R5 (PanelBusBridge), status_aggregator, prometheus_export

### R3: Human Arbiter View

**Description:** A decision interface for when machines disagree. Presents the human (Aaron) with winner recommendation, alternatives, unresolved dissent, evidence chain, and action buttons. Accessible via `/arbiter` route or standalone server on port 8857.

**Acceptance Criteria:**
- [ ] `register_pending_decision(decision)` stores decision in pending state with `decision_id`, `status == "awaiting_human"`, and `created_at` timestamp
- [ ] `GET /arbiter` returns self-contained HTML page (no external JS/CSS dependencies) with dark theme (#1a1a2e base)
- [ ] `GET /arbiter/pending` returns JSON with `decisions` list and `count`
- [ ] `POST /arbiter/decide` with `{decision_id, action, override_branch}` records human decision and removes from pending
- [ ] Pending decisions include: `recommendation` (with branch, confidence, reasoning), `alternatives` list, `dissent` list with surgeon attribution
- [ ] Decision actions supported: `accept`, `override`, `request_more_info`
- [ ] HTML renders recommendation card, alternative cards, dissent entries, and evidence chain with hash verification display
- [ ] [GAP] No WebSocket push to notify when new decisions arrive -- relies on polling `/arbiter/pending`

**Dependencies:** Synthesis engine (decisions.jsonl), evidence ledger (hash chain)

### R4: Theatrical Dashboard Components

**Description:** Four embeddable HTML components with a dark theme (Superset-inspired, #1a1a2e / #00d4ff accent) that make AI intelligence visible: VitalSignsBar, SurgeonFeed, QuorumPulse, EvidenceTimeline. Each has `render(data) -> HTML` and `get_sample_data() -> dict`. Shared CSS injected once per page.

**Acceptance Criteria:**
- [ ] `VitalSignsBar.render(data)` returns HTML string containing node health indicators with heartbeat, latency, stress values
- [ ] `SurgeonFeed.render(data)` returns HTML string with surgeon activity entries color-coded per surgeon: atlas (#00d4ff), cardiologist (#f97316), neurologist (#a855f7)
- [ ] `QuorumPulse.render(data)` returns HTML string visualizing quorum signal flow
- [ ] `EvidenceTimeline.render(data)` returns HTML string with evidence entries showing hash chain verification status
- [ ] Each component's `get_sample_data()` returns a dict that `render()` accepts without error
- [ ] All components share a common CSS class prefix `tc-component` with dark theme
- [ ] Components produce valid, self-contained HTML (no external resource references)
- [ ] Rich terminal UI (`theater_dashboard.py`) renders 5-zone layout: NODE RAIL, ACTIVE THEATER, CHIEF SYNTHESIS, EVIDENCE LEDGER, TRUTH LADDER
- [ ] Terminal theater dashboard polls daemon every 2s and degrades gracefully when daemon is unreachable
- [ ] `theatrical.py` FleetTheater provides dramatic terminal output for race results, gains-gate, evidence verification using Rich library (degrades to plain text when Rich unavailable)

**Dependencies:** dashboard_html.py (theme constants), Rich library (optional for terminal)

### R5: Panel-Bus Bridge

**Description:** A singleton bridge that subscribes to IDEEventBus namespaces (fleet.*, probe.*, surgeon.*, evidence.*, quorum.*) and maintains per-panel caches. Emits `panel.updated` events when caches change. Dashboard components and poll endpoints query the bridge instead of recomputing.

**Acceptance Criteria:**
- [ ] `PanelBusBridge` subscribes to 5 event namespaces on construction: fleet.*, probe.*, surgeon.*, evidence.*, quorum.*
- [ ] Incoming `fleet.*` events populate `fleet-status` panel cache
- [ ] Incoming `probe.*` events populate `probe-findings` panel cache
- [ ] Incoming `surgeon.*` events populate `surgeon-feed` panel cache
- [ ] Incoming `evidence.*` events populate `evidence-timeline` panel cache
- [ ] Incoming `quorum.*` events populate `quorum-pulse` panel cache
- [ ] `get_panel(panel_id)` returns dict with `panel_id`, `updated_at` (float), `event_count` (int), `events` (list, newest first)
- [ ] `get_all_panels()` returns all 5 panel caches with `generated_at` timestamp
- [ ] `get_timestamps()` returns only `updated_at` and `event_count` per panel (no event payloads)
- [ ] `get_panel_for_component(name)` maps component names (`vitals`, `surgeon-feed`, `quorum-pulse`, `evidence-timeline`) to panel data
- [ ] Each panel cache is bounded to configurable `cache_size` (default 100); oldest events evicted when full
- [ ] `PanelBusBridge.get()` returns singleton; `PanelBusBridge.reset()` clears singleton for testing
- [ ] `emit_panel_update()` publishes `panel.updated` event to the bus with panel_id and updated_at
- [ ] `health()` returns dict with `status`, `active_panels` list, and `total_events` count

**Dependencies:** IDEEventBus (event_bus.py)

### R6: Real-Time Streaming

**Description:** SSE (Server-Sent Events) endpoint for pushing fleet events to browser clients, plus IDE bridge event queue for VS Code extension consumption.

**Acceptance Criteria:**
- [ ] `GET /events` returns HTTP 200 with `Content-Type: text/event-stream` and `Cache-Control: no-cache`
- [ ] SSE endpoint sends keepalive comments (`: keepalive\n\n`) every 15 seconds when no events are pending
- [ ] SSE endpoint pushes events as `data: {json}\n\n` format when events arrive via `_push_sse()`
- [ ] Multiple concurrent SSE clients each receive all events independently
- [ ] Disconnected SSE clients are cleaned up (removed from queue list) without affecting other clients
- [ ] `IDEBridge.connect()` retries with exponential backoff (1s, 2s, 4s) up to `max_retries` (default 3)
- [ ] `IDEBridge.enqueue_event()` adds to queue; when queue exceeds `max_events` (default 1000), oldest events are dropped and `_dropped_count` incremented
- [ ] `IDEBridge.drain_events()` returns all queued events and clears the queue atomically (thread-safe)
- [ ] `IDEBridge.is_stale()` returns True when last snapshot is older than `stale_threshold` (default 30s)
- [ ] `IDEBridge.shutdown()` flushes all pending notifications and events, returns flushed items, sets connected=False
- [ ] `IDEBridge.translate_event()` maps fleet event types to IDE commands via `_EVENT_MAP` (11 event types mapped)
- [ ] `IDEBridge.format_for_extension()` returns ContextDNA Event v1 schema: `{type, payload, source, timestamp}`
- [ ] `DashboardEventConsumer` subscribes to `fleet.peer.*`, `fleet.message.*`, `fleet.health.*`, `fleet.lease.*`, `fleet.rebuttal.*`, `fleet.race.*`
- [ ] `DashboardEventConsumer.state` returns serializable dict snapshot (deques converted to lists)
- [ ] `FleetDashboardData.snapshot()` returns cached aggregated state from daemon HTTP endpoints with configurable TTL (default 5s)

**Dependencies:** IDEEventBus, FleetStatusAggregator

### R7: Auxiliary Dashboards

**Description:** Specialized dashboard views for beta user analytics, regression/build health tracking, and the core Rich terminal TUI.

**Acceptance Criteria:**
- [ ] `BetaDashboard` stores beta users, feedback, and issues in SQLite with schema: `beta_users`, `beta_feedback`, `beta_issues` tables
- [ ] `RegressionDashboard.collect_metrics()` returns dict with: `test_count`, `module_count`, `skill_count`, `mcp_tool_count`, `probe_count`, `revenue_path`, `recent_commits`
- [ ] `RegressionDashboard.render_html()` returns self-contained HTML string
- [ ] `GET /regression` returns regression dashboard HTML
- [ ] `GET /api/regression` returns regression metrics as JSON
- [ ] Terminal `dashboard.py` renders Rich Live layout with sections: header, peers table, messages stats, surgeons status, sessions table
- [ ] Terminal dashboard polls daemon `/health` every 2s and shows "Daemon unreachable" panel when offline
- [ ] Terminal dashboard accepts keyboard input: `q` to quit, `r` to refresh, `d` to trigger doctor

**Dependencies:** SQLite (beta_dashboard), Rich library (terminal dashboards)

## Out of Scope

- Fleet state mutation (this domain is read-only presentation)
- Authentication/authorization on HTTP endpoints (currently open, CORS *)
- WebSocket push for arbiter decisions (currently poll-based, marked as GAP)
- Mobile-responsive layouts (desktop/terminal focus)
- Custom theme configuration (hardcoded dark theme)
- Electron/VS Code extension TypeScript code (this kit covers the Python bridge side only)

## Cross-References

- See also: cavekit-events.md (IDEEventBus that feeds panel-bus-bridge and dashboard-events)
- See also: cavekit-evidence.md (evidence ledger consumed by evidence-timeline panel)
- See also: cavekit-probes.md (probe engine consumed by probe-findings panel)
- See also: cavekit-synthesis.md (ChiefSynthesis consumed by synthesis-decisions panel and arbiter view)
- See also: cavekit-config.md (web_server port configuration, billing tier feature gating)
