---
domain: fleet-coordination
version: "1.0"
status: brownfield-extracted
modules: 13
source_files:
  - task_orchestrator.py
  - auto_coordinator.py
  - race_orchestrator.py
  - fleet_race.py
  - supervisor.py
  - supervisor_store.py
  - supervisor_watchdog.py
  - coordinator.py
  - captain.py
  - liaison.py
  - hierarchy.py
  - node_overlay.py
  - fleet_metrics.py
---

# Cavekit: Fleet Coordination

## Scope

Fleet-wide task orchestration, competitive branch racing, session lifecycle supervision, message relay to coding agents, hierarchical role management, and fleet status aggregation. These modules coordinate work across multiple fleet nodes (mac1, mac2, mac3+) through the daemon HTTP API and NATS messaging.

## Requirements

### R1: Competitive Branch Racing -- Dispatch, Cross-Review, Synthesize

**Description:** FleetRace dispatches the same task to multiple nodes for parallel execution in isolated worktrees. Nodes race to produce solutions, which undergo cross-review (rebuttal) and chief synthesis to select or merge the best outcome.

**Acceptance Criteria:**
- [ ] `FleetRace.start(task, nodes, branch_prefix, timeout_s)` creates a race with unique `race_id` and dispatches to all specified nodes
- [ ] Default timeout is 600s (10 minutes)
- [ ] Race state tracks lifecycle phases: `dispatched -> racing -> first_complete -> cross_review -> synthesis -> done`
- [ ] All race state is held in-memory (`_races` dict keyed by `race_id`)
- [ ] `FleetRace.status(race_id)` returns current race state including participants, phase, and timestamps
- [ ] When superpowers workflow phase blocks dispatch, race returns `status: "blocked_by_workflow"` and queues the race for later execution
- [ ] `run_probes=True` (default) triggers probe scan before race dispatch [GAP: verify probe integration path in start() -- only the flag is visible, execution path not confirmed in read portion]

**Dependencies:** cavekit-probes (probe scan before dispatch)

### R2: Race Orchestrator -- End-to-End Pipeline

**Description:** RaceOrchestrator wires FleetRace dispatch, verdict collection, RebuttalManager cross-examination, and ChiefSynthesis into a single blocking pipeline call.

**Acceptance Criteria:**
- [ ] `run_race(prompt, nodes, timeout_s)` executes the full pipeline: dispatch -> poll verdicts -> rebuttal -> synthesis
- [ ] Default nodes are `["mac1", "mac2", "mac3"]`
- [ ] Verdicts are polled at `poll_interval_s` (default 10s) cadence
- [ ] Rebuttal sessions have configurable `deadline_minutes` (default 30)
- [ ] Return dict includes: `status`, `decision`, `winner`, `verdicts`, `critiques`, `synthesis`, `elapsed_s`, `rebuttal_session`
- [ ] When `auto_execute_cherry_pick=True` and synthesis decides cherry-pick, MergeAdjudicator applies the cherries via git
- [ ] `auto_execute_cherry_pick` defaults to False (safe default for non-git contexts)
- [ ] Daemon communication uses HTTP GET/POST to `http://127.0.0.1:8855` with 5s/10s timeouts

**Dependencies:** R1

### R3: Auto-Coordinator -- Inbox Monitoring and Least-Loaded Dispatch

**Description:** AutoCoordinator watches `.fleet-messages/<node>/` for incoming task requests, dispatches to available nodes based on session count (least-loaded), tracks task progress, and escalates stalled tasks.

**Acceptance Criteria:**
- [ ] `AutoCoordinator(node_id, stall_timeout_s, inbox_dir)` initializes with configurable stall timeout (default 300s)
- [ ] `scan_inbox()` reads `.fleet-messages/<node_id>/` for new YAML-frontmatter task messages
- [ ] Messages require at minimum a `subject` key in frontmatter
- [ ] Already-processed files (tracked by path in `_processed_files` set) are not re-dispatched
- [ ] Dispatch selects the node with the fewest `active_sessions` from `_node_session_counts`
- [ ] `TrackedTask` tracks: `task_id`, `description`, `assigned_to`, `source_file`, `dispatched_at`, `status` (dispatched/in_progress/completed/stalled/escalated)
- [ ] `tick()` performs a full cycle: scan inbox + dispatch new tasks + check for stalled tasks
- [ ] Tasks exceeding `stall_timeout_s` without progress are marked `"stalled"` and counted in `stats["stalls_detected"]`
- [ ] `status()` returns all tracked tasks with age and assignment information
- [ ] Stats track: `scans`, `tasks_dispatched`, `tasks_completed`, `tasks_escalated`, `stalls_detected`
- [ ] Available nodes default to `["mac1", "mac2", "mac3"]`

**Dependencies:** None (filesystem-based inbox)

### R4: Supervisor -- Session Lifecycle State Machine

**Description:** Supervisor drives a task through a 12-phase state machine from inbox capture to completion, enforcing that execution continues until the VisionContract is satisfied or the task is truly blocked.

**Acceptance Criteria:**
- [ ] `SupervisorPhase` enum defines 13 phases: INBOX_CAPTURE, BRAINSTORMING, SURGEON_ADJUDICATION, CHIEF_ESCALATION, VISION_LOCK, EXECUTION, REVIEW, FIX_LOOP, SNAPSHOT_RECORD, DAO_CANDIDATE, CHIEF_ACCEPTANCE, COMPLETE, BLOCKED
- [ ] `PHASE_ORDER` defines the sequential ordering of all non-BLOCKED phases (12 phases)
- [ ] `VisionContract` specifies `task_id`, `description`, `done_criteria` (list of strings that must all be satisfied), and `blocked_criteria`
- [ ] `SupervisorState` tracks: `task_id`, `phase`, `vision_contract`, `history` (phase transitions), `blocked_reason`, `iteration_count`, `max_iterations` (default 20), `criteria_met`
- [ ] Phase transitions are recorded in `history` list
- [ ] Safety limit: `max_iterations` (default 20) prevents infinite loops
- [ ] `SupervisorPhase.BLOCKED` is a terminal state with a `blocked_reason`

**Dependencies:** None

### R5: Supervisor Persistence (SQLite Store)

**Description:** SupervisorStore provides SQLite-backed persistence so supervisor task state survives daemon restarts. State is written on every phase transition.

**Acceptance Criteria:**
- [ ] `SupervisorStore` creates a SQLite database at `.contextdna/local/supervisor.db` with WAL journal mode
- [ ] `save(state)` persists all SupervisorState fields including serialized `done_criteria`, `criteria_met`, and `history` as JSON
- [ ] `load_active()` returns all non-COMPLETE, non-BLOCKED tasks as SupervisorState objects
- [ ] `wrap(supervisor)` returns a supervisor instance with auto-save on every phase transition
- [ ] Database table `supervisor_tasks` has `task_id` as primary key

**Dependencies:** R4

### R6: Supervisor Watchdog -- Stall Detection and Auto-Recovery

**Description:** SupervisorWatchdog detects phases stuck beyond configurable timeouts and either auto-advances safe phases or marks blocking phases as blocked.

**Acceptance Criteria:**
- [ ] Per-phase timeouts range from 60s (INBOX_CAPTURE) to 600s (EXECUTION)
- [ ] `AUTO_ADVANCE_PHASES` (INBOX_CAPTURE, VISION_LOCK, SNAPSHOT_RECORD, DAO_CANDIDATE) are auto-advanced on stall
- [ ] `BLOCK_ON_STALL_PHASES` (EXECUTION, FIX_LOOP, SURGEON_ADJUDICATION) are marked blocked on stall, not auto-advanced
- [ ] `check_and_recover()` returns a list of stalled tasks with actions taken
- [ ] Timeout overrides can be passed at construction time

**Dependencies:** R4

### R7: Liaison -- Fleet Message Relay to Coding Sessions

**Description:** FleetLiaison runs as a background agent that monitors all fleet channels, processes incoming messages, and interrupts the main coding agent only when escalation threshold is crossed.

**Acceptance Criteria:**
- [ ] `FleetLiaison(node_id, daemon_url)` initializes with node identity and daemon endpoint
- [ ] `EscalationLevel` enum: SILENT, INFO, URGENT, CRITICAL
- [ ] `ESCALATION_MAP` maps message types to default escalation levels: heartbeat/ack/sync -> SILENT, context/repair/merge_candidate/local_verdict/branch_status -> INFO, task_brief/critique/chief_decision -> URGENT, alert -> CRITICAL
- [ ] `LiaisonState` tracks: `pending_reviews`, `active_rebuttals`, `unread_messages`, `last_heartbeat`, `coding_agent_session`
- [ ] `_MAX_UNREAD` is 200; oldest messages are dropped when cap is exceeded
- [ ] `LiaisonState.summary()` returns a one-line status string (e.g., "3 review(s) pending | 2 unread") suitable for systemMessage injection
- [ ] Liaison is schema-bound and low-token: it relays, summarizes, compares, and escalates but never edits code

**Dependencies:** cavekit-probes R5 (health state for escalation context)

### R8: Captain Role -- Cluster Worker Management

**Description:** CaptainRole manages a cluster of workers within the fleet hierarchy, routing worker messages to chief and aggregating worker heartbeats.

**Acceptance Criteria:**
- [ ] `CaptainRole(node_id, cluster_id, chief_id)` initializes with identity and hierarchy context
- [ ] `activate()` returns True on first activation, False if already active
- [ ] `deactivate()` returns True if was active, False if already inactive
- [ ] `add_worker(worker_id)` returns False for duplicates, True for new workers
- [ ] `remove_worker(worker_id)` returns False if not found; removes heartbeat entry when worker is removed
- [ ] `WorkerHeartbeat` stores: `worker_id`, `timestamp`, `status`, `payload`
- [ ] `workers` property returns a copy of the worker list (not a reference)
- [ ] `_relay_log` tracks all messages relayed to chief [GAP: relay method not visible in read portion -- verify relay_to_chief() or equivalent exists]

**Dependencies:** R9

### R9: Fleet Hierarchy -- Chief/Captain/Worker Role Management

**Description:** FleetHierarchy manages node roles and routing hierarchy, enabling scaling from 3 nodes to 100+ via hierarchical message routing.

**Acceptance Criteria:**
- [ ] `NodeRole` enum: CHIEF, CAPTAIN, WORKER
- [ ] `NodeInfo` stores: `node_id`, `role`, `captain_id` (workers only), `cluster_id`, `workers` (captains only), `capabilities`
- [ ] `FleetHierarchy.register_node()` registers a node with its role and returns `NodeInfo`
- [ ] Registering a CHIEF sets `_chief_id`; only one chief is expected at a time [GAP: verify whether registering a second CHIEF raises/warns or silently replaces]
- [ ] `NodeInfo.to_dict()` and `NodeInfo.from_dict()` provide round-trip serialization
- [ ] Hierarchy can be initialized from a config dict at construction time
- [ ] Workers reference their captain via `captain_id`; captains list their workers in `workers` field

**Dependencies:** None

### R10: Node Overlay -- Fleet-Wide Status Aggregation

**Description:** NodeOverlay maintains 5 status strips per node that capture epistemic state, surgeon state, invariant state, evidence state, and probe findings. This provides a fleet-wide view of what each node thinks, how strongly, and based on what.

**Acceptance Criteria:**
- [ ] `EpistemicStrip` tracks: `certainty_band` (0.0-1.0), `contradiction_count`, `stale_context_risk` (0.0-1.0), `last_packet_ts`
- [ ] `SurgeonStrip` tracks: `phase` (triage/operate/review/close/idle), `disagreement_score` (0.0-1.0), `correction_loop_count`, `abstention_status`
- [ ] `InvariantStrip` tracks: `active_count`, `violated_count`, `proof_receipts` (list of strings)
- [ ] `EvidenceStrip` tracks: `active_evidence_ids`, `candidate_count`, `promoted_count`, `unresolved_objection_ids`
- [ ] Each strip has a `to_dict()` method returning JSON-serializable data
- [ ] Numeric values are rounded to 3 decimal places in `to_dict()` output

**Dependencies:** cavekit-probes (probe findings strip)

### R11: Fleet Metrics Aggregation

**Description:** FleetMetricsAggregator collects metrics from all reachable fleet nodes via the daemon `/health` endpoint and computes fleet-wide aggregates and per-node summaries.

**Acceptance Criteria:**
- [ ] `FleetMetricsAggregator(daemon_port)` defaults to port 8855
- [ ] `NodeMetrics` captures: `node_id`, `uptime_s`, `messages_sent`, `messages_received`, `errors`, `dedup_dropped`, `registered_types`, `active_sessions`, `channel_score`, `evidence_entries`, `active_leases`, `active_rebuttals`, `probe_findings`, `last_updated`
- [ ] `NodeMetrics.to_dict()` returns all fields as a JSON-serializable dict
- [ ] HTTP fetch failures return None (3s timeout), never raise
- [ ] Aggregator pulls from all reachable nodes and builds fleet-wide summaries

**Dependencies:** cavekit-probes (probe_findings metric)

### R12: Task Orchestrator -- Unified Lifecycle from Idea to Wisdom

**Description:** TaskOrchestrator ties together Supervisor phases, FleetRace parallel execution, TruthLadder promotion, AcceptanceGate verification, EvidenceLedger audit, and Probes into one workflow.

**Acceptance Criteria:**
- [ ] `TaskOrchestrator.run_task(description, done_criteria, parallel, nodes, timeout_s)` drives a complete lifecycle
- [ ] Lifecycle phases: inbox capture -> baseline probes -> execute (parallel or local) -> review (probes + acceptance gate) -> promote (truth ladder) -> record (evidence ledger)
- [ ] Each phase records its outcome in the task dict for full traceability
- [ ] `parallel=True` uses FleetRace across specified nodes; `parallel=False` runs locally
- [ ] Task dict includes: `id`, `description`, `done_criteria`, `status`, `node_id`, `parallel`, `phases` (dict of phase results), `started_at`, `errors` (list)
- [ ] All daemon communication goes through HTTP API (port configurable, default 8855)

**Dependencies:** R1, R2, R4, cavekit-probes (R1 for baseline/review probes)

### R13: Coordinator -- Node Registration and Packet Queue

**Description:** MultiFleetCoordinator handles node registration, heartbeat, packet send/receive to chief, and local packet queue for deferred delivery when chief is offline.

**Acceptance Criteria:**
- [ ] Node ID is determined from `MULTIFLEET_NODE_ID` env var, hostname mapping, or local config
- [ ] `register` command registers the node with the chief
- [ ] `heartbeat` command sends a heartbeat to the chief
- [ ] `send <packet_type> <json_payload>` sends a packet to chief; queues locally if chief is unreachable
- [ ] `flush` drains the local packet queue to chief
- [ ] `status` reports current node state
- [ ] Config stored at `.multifleet/config.json`; local state at `.contextdna/local/node_state.json`; packet queue at `.contextdna/packets/`
- [ ] CLI usage: `python3 -m multifleet.coordinator <command> [args]`

**Dependencies:** None

## Out of Scope

- NATS transport layer details (nats_client.py, nats_cluster.py)
- Cross-rebuttal debate logic (cross_rebuttal.py -- used by R2 but internal logic is a separate domain)
- Chief synthesis decision logic (synthesis.py -- called by R2 but separate domain)
- Merge adjudication git operations (merge_adjudicator.py)
- EventBus implementation (eventbus.py, event_bus.py)
- EvidenceLedger storage (evidence_ledger.py)
- TruthLadder promotion logic (truth_ladder.py)
- AcceptanceGate evaluation (acceptance_gate.py)
- Superpowers workflow phase gating (superpowers_adapter.py -- referenced but separate)
- Theatrical/visual dashboard rendering
- Web dashboard and web server

## Cross-References

- See also: cavekit-probes.md (probes feed into task orchestration baseline/review; health monitor drives self-heal which escalates through fleet messaging)
- Rebuttal/synthesis modules: cross_rebuttal.py, synthesis.py (consumed by R2)
- Evidence pipeline: evidence_ledger.py, truth_ladder.py (consumed by R12)
- Superpowers integration: superpowers_adapter.py (gates R1 race dispatch)
