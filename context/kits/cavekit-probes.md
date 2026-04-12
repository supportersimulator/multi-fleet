---
domain: probes-monitoring
version: "1.0"
status: brownfield-extracted
modules: 29
source_files:
  - probe_engine.py
  - probe_scheduler.py
  - probe_interpreter.py
  - probe_review.py
  - probe_context_factory.py
  - probe_intensity.py
  - health_monitor.py
  - self_heal.py
  - doctor.py
  - probes/__init__.py
  - probes/mirror_drift.py
  - probes/stale_context.py
  - probes/fleet_health.py
  - probes/architectural_hotspots.py
  - probes/branch_intent.py
  - probes/dependency_drift.py
  - probes/agent_readiness.py
  - probes/repeated_failure.py
  - probes/hidden_dependency.py
  - probes/task_diff_mismatch.py
  - probes/worktree_contamination.py
  - probes/invariant_coverage.py
  - probes/blast_radius.py
  - probes/security_surface.py
  - probes/injection_prioritizer.py
  - probes/repeated_failure_cluster.py
  - probes/worktree_divergence.py
  - probes/partition_lease_drift.py
  - probes/distributed.py
---

# Cavekit: Probes and Monitoring

## Scope

Continuous code scanning, fleet health observation, self-healing, and diagnostic systems. Probes produce structured findings (ProbeResult) that feed into the evidence pipeline, drive surgeon intensity decisions, and surface in IDE panels. Health monitoring detects degradation across fleet nodes and triggers repair escalation. Self-healing manages repair lifecycle with backoff and vacation awareness.

## Requirements

### R1: Probe Engine -- Trigger-Based Execution

**Description:** A central engine orchestrates probe execution by trigger type (manual, all, incremental, session_start, commit, session_end, synthesis, diff_ready). Each probe is registered with its trigger list. The engine dispatches only matching probes for a given trigger.

**Acceptance Criteria:**
- [ ] `ProbeEngine.scan("manual", ctx)` runs only probes whose `triggers` list contains `"manual"`
- [ ] `ProbeEngine.scan("all", ctx)` runs every registered probe regardless of trigger list
- [ ] `ProbeEngine.scan("incremental", ctx)` uses git diff to determine changed files and runs only probes mapped via `FILE_PROBE_MAP`
- [ ] `ProbeEngine.scan_category(category, ctx)` runs only probes in the specified `ProbeCategory`
- [ ] `ProbeEngine.scan_one(probe_id, ctx)` runs exactly one probe and appends to results cache
- [ ] Invalid `probe_id` values (not matching `^[a-z][a-z0-9_]{0,63}$`) return an error ProbeResult without raising
- [ ] Each probe execution is isolated in a thread with configurable timeout (default 10s)
- [ ] A probe that exceeds its timeout returns a ProbeResult with status `"error"` and message containing `"timed out"`
- [ ] A probe that raises an exception returns a ProbeResult with status `"error"` and the exception message

**Dependencies:** None

### R2: Probe Registry and Caching

**Description:** Probes register with a central ProbeRegistry that manages identity, caching, and health tracking. Cached results avoid redundant execution within a configurable TTL window.

**Acceptance Criteria:**
- [ ] `ProbeRegistry.register()` stores probe_id, callable, category, description, and triggers list
- [ ] Registering a duplicate probe_id overwrites the previous entry (with warning log)
- [ ] Cached probe results are returned when age < `cache_ttl` (default 60s)
- [ ] `invalidate_cache(probe_id)` clears cache for one probe; `invalidate_cache()` clears all
- [ ] Per-probe health tracking: status transitions from `"ok"` to `"degraded"` (1-2 consecutive failures) to `"broken"` (3+ consecutive failures)
- [ ] A successful probe execution resets `consecutive_failures` to 0 and status to `"ok"`
- [ ] `get_health()` returns health status for every registered probe

**Dependencies:** None

### R3: Probe Scheduling with Independent Intervals

**Description:** Each probe has its own execution interval. The scheduler's `tick()` checks which probes are due, runs them, and caches results with staleness metadata.

**Acceptance Criteria:**
- [ ] `ProbeScheduler` accepts a `ProbeEngine` and a dict of `probe_id -> interval_seconds`
- [ ] `load_defaults()` populates schedules from `DEFAULT_SCHEDULES` (8 probes with intervals from 60s to 600s)
- [ ] `tick()` runs only probes whose time since last run exceeds their interval
- [ ] `tick()` does not run a probe whose interval has not elapsed
- [ ] `get_cached_results()` returns `CachedResult` objects with `age_s` and `ttl_s` metadata
- [ ] Each `CachedResult.age_s` reflects seconds since caching (monotonic clock)

**Dependencies:** R1, R2

### R4: Self-Healing with Exponential Backoff, Vacation Mode, and Message Cap

**Description:** The SelfHealManager detects offline peers, executes repair escalation (L1-L4), tracks idle nodes with exponential backoff, respects vacation markers, and caps unread messages per node.

**Acceptance Criteria:**
- [ ] Repair locks prevent concurrent repairs on the same target (TTL 300s)
- [ ] `_acquire_repair_lock(target)` returns False if lock is already held and not expired
- [ ] Exponential backoff: initial 300s, multiplier 2.0, max 3600s per failed repair cycle
- [ ] After 3 consecutive failed repairs (`EXTENDED_IDLE_THRESHOLD`), node is considered "extended idle"
- [ ] `_should_skip_repair(peer_id)` returns True when backoff timer has not elapsed or peer is on vacation
- [ ] `mark_vacation(peer_id, True)` persists a `.vacation` marker file under `.fleet-messages/<peer_id>/`
- [ ] `mark_vacation(peer_id, False)` removes marker file and resets backoff state
- [ ] `is_on_vacation(peer_id)` returns True only for nodes with active vacation markers
- [ ] Vacation markers are loaded from disk on `SelfHealManager` initialization
- [ ] `MAX_UNREAD_MESSAGES_PER_NODE` is 50; messages beyond this cap are not sent [GAP: enforcement mechanism not visible in read code -- verify `_should_skip_repair` or send path checks this cap]
- [ ] L4 SSH repairs are restricted to chief node only when `FLEET_REPAIR_L4_CHIEF_ONLY` env var is truthy (default: true)
- [ ] Non-chief nodes escalate L4 repairs to chief via fleet message instead of executing directly
- [ ] Successful repair resets backoff state via `_record_successful_repair()`

**Dependencies:** cavekit-coordination (fleet messaging for escalation)

### R5: Health Monitor -- Continuous Background Observability

**Description:** FleetHealthMonitor polls the daemon `/health` endpoint at configurable intervals, tracks per-node health history, detects anomalies (score drops, error spikes, peer disappearances), and triggers repair actions.

**Acceptance Criteria:**
- [ ] `FleetHealthMonitor` polls daemon at `check_interval` (default 30s) in a background thread
- [ ] Per-node health history is stored as a deque with max `HISTORY_MAX` (60) snapshots
- [ ] Score drop of >= `SCORE_DROP_THRESHOLD` (2) within `SCORE_DROP_WINDOW_S` (300s) triggers an anomaly alert
- [ ] Error spike of >= `ERROR_SPIKE_THRESHOLD` (10) within window triggers an anomaly alert
- [ ] `wire_eventbus(eventbus)` injects EventBus for event emission on state changes
- [ ] `wire_ledger(ledger)` injects EvidenceLedger for tamper-evident recording of repair actions
- [ ] `ChannelHealthInvariant` is instantiated on init for continuous channel health enforcement
- [ ] Channel state transitions (healthy->broken, broken->healthy) are detected per peer
- [ ] `check_fleet()` returns `{"status": "unreachable", ...}` when daemon is not reachable

**Dependencies:** R4 (self-heal triggers), cavekit-coordination (daemon health endpoint)

### R6: 18 Typed Probes Covering Security, Dependencies, Drift, Performance

**Description:** 18 probe modules (registered via `probes/__init__.py`) cover 6 categories: structure, change, reliability, context_quality, security, and agent_readiness. Each returns a `ProbeResult` with findings, confidence, severity, and blast_radius.

**Acceptance Criteria:**
- [ ] `register_all(registry)` registers exactly 18 probes [GAP: __init__.py imports 17 modules but registers 18 probe_ids -- `distributed.py` is imported separately by engine, not registered in `register_all`. Actual registered count is 17 per current code. Verify whether this is intentional or a missing registration]
- [ ] Every registered probe callable accepts a `context: dict` and returns a `ProbeResult`
- [ ] `ProbeResult` contains: `probe_id`, `category` (string), `status` (ok/warning/critical/error/skipped), `confidence` (0.0-1.0), `severity` (info/low/medium/high/critical), `findings` (list of ProbeFinding), `missing_inputs`, `duration_ms`, `generated_at`
- [ ] `ProbeFinding` contains: `title`, `evidence` (list of strings), `blast_radius` (low/medium/high), `suggested_injections`, `file_paths`
- [ ] Category distribution: STRUCTURE (architectural_hotspots, blast_radius, hidden_dependency), CHANGE (branch_intent, dependency_drift, task_diff_mismatch, worktree_contamination, worktree_divergence), RELIABILITY (fleet_health, repeated_failure, repeated_failure_cluster, invariant_coverage, partition_lease_drift), CONTEXT_QUALITY (mirror_drift, stale_context), SECURITY (security_surface), AGENT_READINESS (agent_readiness, injection_prioritizer)
- [ ] Probes that lack required context inputs return `ProbeResult.skipped()` with `missing_inputs` populated
- [ ] `create_engine(wire_evidence_bridge=True)` returns a fully wired ProbeEngine with all probes registered

**Dependencies:** R1, R2

### R7: Probe Findings Feed Evidence Pipeline

**Description:** HIGH and CRITICAL severity probe findings are automatically recorded to the EvidenceLedger and pushed to ContextDNA via ProductBridge. All findings emit EventBus events. A SignalScorer ranks results by priority.

**Acceptance Criteria:**
- [ ] `_record_high_findings()` writes to EvidenceLedger only for results with severity `"high"` or `"critical"` that have non-empty findings
- [ ] Evidence ledger entries include: event_type `"probe_finding"`, node_id, probe_id, severity, findings_count, confidence, category, finding_titles
- [ ] `_push_high_findings_to_contextdna()` sends HIGH+ findings to ProductBridge with tags `["probe", "high_findings", "learning_loop"]`
- [ ] `_emit_probe_finding(result)` emits `"probe.finding"` event to EventBus (best-effort, never fails probe execution)
- [ ] `SignalScorer.score_results()` produces score = `confidence * severity_weight * recurrence_bonus * freshness_decay`
- [ ] Severity weights: critical=1.0, high=0.8, medium=0.5, low=0.2, info=0.1
- [ ] Recurrence bonus uses `log1p(count) * 0.3` from evidence ledger probe counts
- [ ] Freshness decay uses exponential decay with 6-hour half-life
- [ ] `filter_noise(results, threshold=0.3)` removes results below the score threshold

**Dependencies:** R1, R6, cavekit-coordination (EventBus, EvidenceLedger)

### R8: Probe Interpretation (LLM with Template Fallback)

**Description:** ProbeInterpreter generates human-readable summaries from raw probe results. Attempts LLM-powered interpretation first (P4 BACKGROUND priority), falls back to template-based interpretation when LLM is unavailable.

**Acceptance Criteria:**
- [ ] `interpret(results)` returns dict with keys: `summary`, `risk_level`, `flagged`, `recommendations`, `source`, `generated_at`
- [ ] When LLM is available, `source` is `"llm"`; when unavailable, `source` is `"template"`
- [ ] Empty results list returns a result with `risk_level: "none"` and `source: "template"`
- [ ] LLM timeout is configurable (default 15s)

**Dependencies:** R1, R6

### R9: Probe-to-Surgeon Intensity Mapping

**Description:** Probe results map to 3-Surgeon intensity levels (light/standard/full) that determine the rigor of cross-examination before accepting changes.

**Acceptance Criteria:**
- [ ] `determine_intensity()` returns `"full"` when any result has status `"critical"` or any finding has `blast_radius == "high"`
- [ ] `determine_intensity()` returns `"standard"` when any result has severity `"high"` or `"critical"` (without blocking findings)
- [ ] `determine_intensity()` returns `"light"` when all results are low/medium/info severity
- [ ] `escalate_intensity(current, minimum)` returns the stricter of the two levels
- [ ] `intensity_for_acceptance()` integrates epistemic packet's `required_surgeon_intensity` as a floor
- [ ] Return dict includes `requires_resolution: True` when intensity is `"standard"` or `"full"`

**Dependencies:** R1, R6

### R10: Probe Context Auto-Assembly

**Description:** ProbeContextFactory builds a fully-populated context from live git state, eliminating manual context construction by callers.

**Acceptance Criteria:**
- [ ] `build_probe_context()` returns a ProbeContext with `repo_path`, `branch`, `changed_files`, `task_description`, `node_id`
- [ ] `branch` is populated from `git rev-parse --abbrev-ref HEAD`
- [ ] `changed_files` includes both staged and unstaged changes (union of `git diff --name-only HEAD` and `git diff --name-only --cached`)
- [ ] `node_id` defaults to `MULTIFLEET_NODE_ID` env var when not provided
- [ ] Git command failures return empty strings (never raise)
- [ ] Git commands have a 10s timeout

**Dependencies:** None

### R11: Distributed Probe Scanning

**Description:** The DistributedProbeRunner dispatches probe scans to fleet peers via the daemon HTTP API and aggregates results into a fleet-wide view. The ProbeEngine merges remote results with local results transparently.

**Acceptance Criteria:**
- [ ] `_dispatch_to_peers()` returns empty list when daemon is unreachable (2s timeout check)
- [ ] Remote probe results are deserialized into local `ProbeResult` objects with `probe_id` prefixed by `"{peer}:"`
- [ ] Peers with `node_id` starting with `"test"` are excluded from distributed scans
- [ ] `DistributedProbeRunner.run_fleet_scan()` returns per-node results and fleet-wide summary
- [ ] Distributed dispatch is non-blocking: failures return empty list, never raise

**Dependencies:** R1, R6, cavekit-coordination (daemon HTTP API)

### R12: Fleet Doctor Diagnostics

**Description:** FleetDoctor runs 14 structured diagnostic checks and reports machine-readable health status. Results use diagnostic codes (FD-001, FD-002, etc.) for programmatic consumption.

**Acceptance Criteria:**
- [ ] `FleetDoctor.run()` returns a `DoctorReport` containing a list of `Check` objects
- [ ] Each `Check` has: `code` (string), `name`, `status` (pass/fail/warn/skip), `message`, `latency_ms`, `detail` (dict)
- [ ] `DoctorReport` exposes `passed`, `failed`, `warned` counts as properties
- [ ] Report includes `duration_ms` for total diagnostic time
- [ ] Doctor is usable as CLI: `python3 -m multifleet.doctor` (text) and `python3 -m multifleet.doctor --json` (JSON)

**Dependencies:** cavekit-coordination (daemon health endpoint)

### R13: Probe Review Integration

**Description:** `run_probe_review()` runs the full organism pipeline: probes detect, builds epistemic packet, 3-surgeons review, acceptance gate decides.

**Acceptance Criteria:**
- [ ] `run_probe_review(repo_root, task_intent)` runs all probes and includes findings count, surgeon intensity, and blocking count in result
- [ ] When `consult_surgeons=True`, 3-surgeon cross-examination is invoked
- [ ] Result includes `probe_results`, surgeon verdict, and acceptance decision
- [ ] `engine.determine_surgeon_intensity()` is called to set review rigor

**Dependencies:** R1, R6, R9

## Out of Scope

- IDE panel rendering (covered by IDE/dashboard kits)
- EventBus implementation details (separate module)
- EvidenceLedger storage format (separate module)
- ProductBridge ContextDNA sync protocol (separate module)
- 3-Surgeon cross-examination logic (separate module)
- Acceptance gate evaluation logic (separate module)
- NATS transport layer (covered by cavekit-coordination or transport kit)

## Cross-References

- See also: cavekit-coordination.md (daemon health endpoint, fleet messaging for distributed probes and self-heal escalation)
- Evidence pipeline modules: evidence_ledger.py, product_bridge.py
- Surgeon integration: acceptance_gate.py, probe_intensity.py -> invariance_gates.py
- EventBus: eventbus.py, event_bus.py
