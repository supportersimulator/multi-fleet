---
domain: synthesis-and-decision-making
version: 1.0.0
status: brownfield
modules: 13
source_files:
  - multifleet/synthesis.py
  - multifleet/arbiter.py
  - multifleet/review_flow.py
  - multifleet/merge_adjudicator.py
  - multifleet/cross_rebuttal.py
  - multifleet/rebuttal.py  # deprecated, superseded by cross_rebuttal.py
  - multifleet/acceptance_gate.py
  - multifleet/gains_gate.py
  - multifleet/invariance_gates.py
  - multifleet/invariance_rules.py
  - multifleet/truth_ladder.py
  - multifleet/constitution.py
  - multifleet/branch_convergence.py
---

# Cavekit: Synthesis and Decision-Making

## Scope

This domain covers the complete decision pipeline from verdict collection through merge execution. It includes: chief synthesis aggregation, cross-machine review orchestration, merge adjudication, acceptance gating, health checks, invariance enforcement, constitutional validation, truth promotion, and branch convergence analysis.

Boundary: This domain consumes evidence and wisdom from `cavekit-evidence` but does not own evidence storage or ghost-layer cognition. It produces decisions that flow back into evidence via recording hooks.

## Requirements

### R1: Chief Synthesis Aggregation

**Description:** Given verdicts from N machines, produce a single merge decision (merge, cherry_pick, reconciliation, reject, needs_rebuttal) with confidence score, cherry-pick suggestions from non-winners, and preserved dissent.

**Acceptance Criteria:**
- [ ] `ChiefSynthesis.synthesize(task_id, verdicts)` returns a `SynthesisResult` with fields: decision, winner_branch, confidence (0.0-1.0), reasoning, cherry_pick_from, dissent_preserved
- [ ] When `verdicts` is empty, decision is "reject" with confidence 0.0
- [ ] When a single verdict is provided, decision is "merge"
- [ ] When multiple verdicts exist, scoring uses `confidence - (risk_count * 0.05) - (dissent_count * 0.1)` as baseline
- [ ] Non-winner strengths produce cherry_pick_from entries with {machine, commit, reason}
- [ ] Registered pluggable analyzers are called; highest-confidence result wins
- [ ] Every synthesis result is logged to JSONL ledger at `~/.fleet-nerve/decisions.jsonl`
- [ ] Every synthesis result is recorded to the evidence ledger via `_record_evidence()`
- [ ] Every synthesis result is pushed to ContextDNA via `_push_to_contextdna()`
- [ ] Memory influence is applied post-scoring via `_apply_memory_influence()` -- confidence adjusted by past evidence
- [ ] Wisdom feedback is applied post-scoring via `_apply_wisdom_feedback()` -- contradictions reduce confidence (max penalty 0.15)
- [ ] Quorum feedback is applied post-scoring via `_apply_quorum_feedback()` when quorum registry is available
- [ ] Pre-synthesis probe scan runs via ProbeEngine; findings are corroborated via GhostCorroborate and attached to metadata

**Dependencies:** cavekit-evidence (EvidenceLedger, WisdomConsumer, MemoryInfluenceResolver, GhostCorroborate)

### R2: Cross-Machine Review Flow

**Description:** A 4-phase orchestrated review lifecycle: dispatch task to machines, collect local verdicts, collect cross-critiques, run chief synthesis.

**Acceptance Criteria:**
- [ ] `ReviewFlowOrchestrator.start_review()` creates a `RebuttalSession` in ASSIGNED phase, then advances to LOCAL_SURGERY
- [ ] `submit_verdict()` records verdicts; when all expected machines submit, session auto-advances to VERDICTS_COLLECTED then UNDER_REVIEW
- [ ] `submit_critique()` records critiques; when all expected machines submit, session auto-advances to CRITIQUES_COLLECTED and sets `needs_synthesis=True`
- [ ] `run_synthesis()` pulls memory context via ChiefSyncAdapter, runs ChiefSynthesis, records chief_decision in session, evaluates AcceptanceGate, and sets phase to "resolved"
- [ ] `ReviewFlowResult` contains: synthesis dict, acceptance dict, machines_participated, duration_s, events_emitted
- [ ] Events are emitted for review.started, review.verdict_received, review.completed via typed EventBus
- [ ] `list_active()` returns non-resolved sessions; `cleanup_expired()` removes expired sessions
- [ ] RebuttalSession state machine enforces valid transitions: ASSIGNED -> LOCAL_SURGERY -> VERDICTS_COLLECTED -> UNDER_REVIEW -> CRITIQUES_COLLECTED -> SYNTHESIS -> RESOLVED
- [ ] Invalid transitions are rejected and logged
- [ ] Expired sessions with verdicts are escalated to HumanArbiter

**Dependencies:** R1 (ChiefSynthesis), R4 (AcceptanceGate)

### R3: Merge Adjudicator

**Description:** Executes cherry-pick git operations from synthesis decisions. Handles conflicts by aborting cleanly and records outcomes to the evidence ledger.

**Acceptance Criteria:**
- [ ] `MergeAdjudicator.execute(decision)` only acts when `decision["decision"] == "cherry_pick"`; otherwise returns status "skipped"
- [ ] When cherry_pick_from is empty, returns status "noop"
- [ ] Checks out winner_branch before applying cherry-picks
- [ ] Each cherry-pick runs `git cherry-pick <commit>` with a 30-second timeout
- [ ] On merge conflict, runs `git cherry-pick --abort` and records the conflict
- [ ] Overall status is "ok" (all applied), "partial" (some applied), "conflict" (none applied, all conflicts), or "failed"
- [ ] Outcome is recorded to the evidence ledger with event_type "merge_decision"
- [ ] dry_run mode returns success without running git commands
- [ ] Repo path validation: returns "failed" if path is not a git repository

**Dependencies:** cavekit-evidence (EvidenceLedger)

### R4: Acceptance Gate

**Description:** Evaluates whether a task's output meets acceptance criteria before merge. Integrates probe results, surgeon state, invariants, and evidence.

**Acceptance Criteria:**
- [ ] `AcceptanceGate.evaluate()` supports two call patterns: positional `(task_id, branch, context={})` and keyword `(probe_results=[], surgeon_resolved=True, ...)`
- [ ] Blocking probe findings (blast_radius == "high") produce blocking_issues
- [ ] Surgeon phase "triage" or "operate" blocks acceptance
- [ ] Surgeon disagreement_score > 0.7 blocks acceptance
- [ ] When surgeon_intensity is "standard" or "full", surgeon resolution is verified via SurgeonAdapter.validate_acceptance() -- never trusts caller alone
- [ ] Unresolved invariants block acceptance in strict mode
- [ ] Zero evidence blocks acceptance in strict mode for non-trivial changes
- [ ] Acceptance policy from epistemic_packet ("strict", "standard", "permissive") adjusts which blockers apply
- [ ] `AcceptanceDecision` contains: accepted (bool), reason, blocking_issues, required_followups, promoted_evidence_ids, unresolved_objection_ids
- [ ] Events are emitted for TASK_ACCEPTED and TASK_BLOCKED
- [ ] `quick_check()` returns boolean -- any high blast_radius finding returns False
- [ ] Mirror drift detected but unacknowledged blocks acceptance (contextual evaluation path)

**Dependencies:** SurgeonAdapter, ProbeEngine

### R5: Fleet Gains Gate

**Description:** Structured health checks before major fleet operations (merges, deployments, phase transitions). Returns pass/fail with per-check detail.

**Acceptance Criteria:**
- [ ] `FleetGainsGate.run()` executes 6 core checks (daemon_health, nats_connected, peer_reachability, packet_registry, dedup_health, eventbus_running)
- [ ] Full run (not quick) adds 5 more checks: lease_conflicts, rebuttal_stale, audit_trail, test_suite, invariance_rules
- [ ] Gate passes only when zero critical checks fail
- [ ] Each `CheckResult` contains: name, passed, message, latency_ms, critical flag
- [ ] `GateResult` contains: passed, checks, total, passed_count, failed_count, critical_failures, duration_ms, recommendation
- [ ] Daemon health check fetches `http://127.0.0.1:{port}/health` with 3s timeout
- [ ] NATS check verifies transport == "nats" and nats_url is present
- [ ] Peer reachability requires at least 1 peer with score > 0
- [ ] Test suite check runs pytest with 30s timeout (non-critical)
- [ ] Invariance rules check runs InvarianceEngine.check_all() (non-critical)
- [ ] Results are recorded to the evidence ledger (best-effort, never fails the gate)
- [ ] CLI mode (`__main__`) exits 0 on pass, 1 on fail; supports --json and --quick flags

**Dependencies:** R6 (InvarianceEngine), cavekit-evidence (EvidenceLedger)

### R6: Invariance Gates and Rules

**Description:** Three operational gates (send, repair, merge) enforce constitutional rules before fleet operations. Seven invariance rules (INV-001 through INV-007) plus channel health invariant (INV-008) provide machine-checkable protocol enforcement.

**Acceptance Criteria:**
- [ ] `FleetSendGate.check(packet)` validates: envelope fields, schema (if registry provided), rate limit, HMAC key availability, target node known, strategic corroboration (Las-like quorum), and stress policy (IQS-like hierarchy shift)
- [ ] Strategic messages (signal_class == "strategic") without corroboration are blocked unless surgeon cross-exam approves or quorum registry confirms quorum
- [ ] Under critical stress (score > threshold), irreversible tactical actions are blocked
- [ ] Stress signals always pass (they ARE the environmental override)
- [ ] `FleetRepairGate.check(target, action)` validates: target unhealthy (not stale diagnostic > 5min), no concurrent repair, action in allowlist (6 valid actions), L4 SSH repairs only from chief, 5-minute cooldown per target, stress policy for destructive repairs
- [ ] L4 repairs require surgeon corrigibility check; blocked if surgeon denies
- [ ] `FleetMergeGate.check(branch)` validates: no lease conflicts, all nodes acked, gains gate passes, no critical dissent, surgeon state OK (not in triage, disagreement <= 0.3), branch mergeable with main (git merge-tree)
- [ ] `InvarianceEngine.check_all(state)` runs 7 rules and returns list of InvarianceResult
- [ ] INV-001: No execution without BrainstormPacket or explicit bypass (critical)
- [ ] INV-002: No VisionContract lock without SurgeonDecisionPacket (critical)
- [ ] INV-003: Missing surgeon consensus at standard/full intensity requires fleet escalation (error)
- [ ] INV-004: COMPLETE requires locked Vision + satisfied criteria + Snapshot + chief acceptance (critical)
- [ ] INV-005: DAO promotion requires Snapshot in truth_ladder_path before DAO (critical)
- [ ] INV-006: Transport fallback to P3+ requires auto-repair request (warning)
- [ ] INV-007: Overlay state must reflect actual surgeon phase, execution phase, chief sync age within 60s tolerance (warning)
- [ ] `ChannelHealthInvariant.check(channel_health)` detects peers with score below half threshold; returns violations with severity and escalation level
- [ ] `ChannelHealthInvariant.auto_repair()` escalates L1 (notify) -> L2 (guide) -> L3 (assist) -> L4 (remote_fix) with 5-minute cooldown between levels
- [ ] L4 remote_fix requires surgeon corrigibility check before execution
- [ ] All gate results are recorded to evidence ledger audit trail via `_record_audit()`

**Dependencies:** SurgeonAdapter, cavekit-evidence (EvidenceLedger)

### R7: Wisdom Feedback Loop

**Description:** Proven wisdom from the evidence ledger and fleet-wisdom files is checked against synthesis decisions. Contradictions reduce confidence; supporting entries are noted.

**Acceptance Criteria:**
- [ ] `WisdomConsumer.feed_synthesis(synthesis_result)` returns {warnings, supporting, wisdom_checked}
- [ ] Contradiction detection uses keyword signal sets: e.g., "reject" decision contradicts entries containing "merge", "approved", "clean"
- [ ] Each contradiction warning includes: type, message, wisdom_entry reference
- [ ] In `ChiefSynthesis._apply_wisdom_feedback()`, each contradiction reduces confidence by 0.05, capped at 0.15 total penalty
- [ ] Reasoning string is prefixed with wisdom warning count and first warning message when contradictions found
- [ ] Wisdom metadata is attached to SynthesisResult: warnings_count, supporting_count, wisdom_checked, confidence_penalty
- [ ] [GAP] Wisdom feedback loop does not currently track whether a contradiction was later resolved or was a false positive -- no learning-from-contradictions path exists

**Dependencies:** cavekit-evidence (WisdomConsumer, EvidenceLedger)

### R8: Constitutional Gate

**Description:** Seven constitutional principles (CP-1 through CP-7) expressed as executable checks. Hard-gate principles block actions; advisory principles log warnings.

**Acceptance Criteria:**
- [ ] `ConstitutionalGate.validate(action, context)` runs all 7 principles and returns GateResult
- [ ] CP-1 Preserve Determinism (hard-gate): fails if output1 and output2 hashes differ
- [ ] CP-2 No Discovery at Injection (hard-gate): fails if operation_log contains forbidden ops (learn, promote, train, discover, write_memory)
- [ ] CP-3 Respect SOP Integrity (advisory): fails if sop_changed=True with evidence_count=0
- [ ] CP-4 Respect acontext Logic (advisory): fails if full_expansion=True with failure_count < 2
- [ ] CP-5 Evidence Over Confidence (hard-gate): fails if confidence > 0.7 with evidence_count=0
- [ ] CP-6 Prefer Reversible Actions (hard-gate): fails if destructive=True without has_checkpoint=True
- [ ] CP-7 Minimalism (advisory): warns if scope="broad" with files_changed > 20, or lines_changed > 1000 with non-minimal scope
- [ ] GateResult.passed is True only when zero hard-gate failures exist
- [ ] `validate_before_action()` module-level convenience uses a default singleton gate
- [ ] `explain_principle(id)` returns human-readable rationale for why the principle exists
- [ ] Check function exceptions are treated as hard failures (zero silent failures)

**Dependencies:** None (self-contained)

### R9: Truth Ladder

**Description:** Four-layer promotion pipeline: inbox -> vision -> snapshot -> dao. Each promotion requires evidence. SQLite-backed with promotion chain tracking.

**Acceptance Criteria:**
- [ ] `TruthLadder.add_inbox(title, content)` creates an entry in layer "inbox" with no evidence requirement
- [ ] `promote_to_vision(inbox_id)` moves inbox -> vision; source must be in "inbox" layer and "active" status
- [ ] `promote_to_snapshot(vision_id)` moves vision -> snapshot
- [ ] `promote_to_dao(snapshot_id)` moves snapshot -> dao; direct vision -> dao is rejected with ValueError
- [ ] Promotion creates a new entry in the target layer; source entry status changes to "promoted"
- [ ] `get_promotion_chain(entry_id)` traces full history: inbox -> vision -> snapshot -> dao
- [ ] `query(layer, status, limit)` filters by layer and status, ordered by created_at DESC
- [ ] `stats` property returns count per layer plus total
- [ ] Every add_inbox and promote operation records to the evidence ledger (best-effort)
- [ ] SQLite uses WAL mode; table has indexes on (layer, status)

**Dependencies:** cavekit-evidence (EvidenceLedger)

### R10: Branch Convergence

**Description:** Visualizes how multi-machine branches relate to main: divergence, file conflicts, merge recommendations.

**Acceptance Criteria:**
- [ ] `BranchConvergence.scan_branches(base="main")` returns list of BranchInfo for fleet-prefixed remote branches (fleet/, mac1/, mac2/, mac3/, race/, worktree/)
- [ ] Each BranchInfo contains: branch name, machine_id (guessed from prefix or committer), commits_ahead, commits_behind, files_changed, last_commit_ts
- [ ] Branches with zero divergence (ahead=0, behind=0) are excluded
- [ ] `conflict_matrix(branches)` returns {(branch_a, branch_b): [overlapping_files]} for all pairs
- [ ] `merge_recommendations(branches)` ranks by: oldest first, fewest conflicts, smallest diff; recommends strategy (fast-forward, rebase, merge --no-ff, manual review)
- [ ] `render_ascii(branches)` produces human-readable branch visualization
- [ ] `render_html(branches)` produces HTML table with conflict highlighting
- [ ] `to_dict()` returns complete convergence report including branches, conflicts, recommendations, and ascii art

**Dependencies:** None (reads git state only)

### R11: Human Arbiter

**Description:** Queues fleet disagreements (low-confidence synthesis, unresolved rebuttals, contested leases) for human decision. Presents recommendation + alternatives + dissent + evidence.

**Acceptance Criteria:**
- [ ] `HumanArbiter.queue_decision()` creates a PendingDecision with unique 8-char ID; emits alert to fleet via daemon /message endpoint
- [ ] `decide(decision_id, action)` supports actions: accept_recommendation, accept_alternative_N, request_info, override_custom
- [ ] Decided outcomes are recorded to the evidence ledger via daemon /evidence endpoint
- [ ] `expire_stale()` removes decisions older than max_age_s (default 3600s); moves to decided list
- [ ] `get_pending()` returns all pending decisions as dicts
- [ ] `get_decided()` returns history of decided items
- [ ] [GAP] PendingDecision uses in-memory dict -- no persistence across daemon restarts

**Dependencies:** cavekit-evidence (EvidenceLedger via daemon)

## Out of Scope

- Evidence storage and hash-chain integrity (see cavekit-evidence)
- Ghost layer ambient cognition (see cavekit-evidence)
- Transport layer (NATS, HTTP, P7 git) -- separate domain
- Probe engine implementation -- consumed as dependency
- Surgeon adapter / 3-surgeon protocol -- consumed as dependency
- Quorum sensing implementation (quorum_analyzer, quorum_registry) -- separate domain
- Race execution and worktree management -- separate domain
- Lease management -- separate domain
- EventBus WebSocket server -- separate domain

## Cross-References

- See also: cavekit-evidence.md (evidence ledger, wisdom consumer, memory influence, ghost layer)
