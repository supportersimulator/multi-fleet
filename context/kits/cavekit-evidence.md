---
domain: evidence-and-wisdom
version: 1.0.0
status: brownfield
modules: 17
source_files:
  - multifleet/evidence_ledger.py
  - multifleet/evidence_sync.py
  - multifleet/wisdom_consumer.py
  - multifleet/memory_bridge.py
  - multifleet/memory_influence.py
  - multifleet/ghost_layer.py
  - multifleet/ghost_cross_node.py
  - multifleet/ghost_corroborate.py
  - multifleet/ghost_contradictions.py
  - multifleet/ghost_objections.py
  - multifleet/ghost_checkpoint.py
  - multifleet/ghost_pulse.py
  - multifleet/ghost_anticipate.py
  - multifleet/ghost_digest.py
  - multifleet/ghost_mirror.py
  - multifleet/contextdna_analyzer.py
  - multifleet/contextdna_evidence_bridge.py
---

# Cavekit: Evidence and Wisdom

## Scope

This domain covers the evidence storage layer, cross-node evidence synchronization, wisdom accumulation and consumption, memory influence resolution, the ghost layer ambient cognition system (7 sub-modules), and bidirectional Context DNA integration. It is the persistent intelligence substrate that makes fleet decisions compound over time.

Boundary: This domain owns evidence storage and retrieval. It does not own synthesis decisions or merge execution (see cavekit-synthesis). It provides evidence and wisdom to synthesis consumers via well-defined interfaces (EvidenceLedger, WisdomConsumer, MemoryInfluenceResolver).

## Requirements

### R1: Hash-Chained Evidence Ledger

**Description:** Append-only SQLite ledger where each entry's SHA-256 hash includes the previous entry's hash, forming a tamper-evident chain. Any modification to a historical entry breaks the chain from that point forward.

**Acceptance Criteria:**
- [ ] `EvidenceLedger.record(event_type, node_id, subject, payload)` appends a new entry and returns dict with entry_id, seq, timestamp, entry_hash, prev_hash
- [ ] Hash computation uses `SHA256("{entry_id}:{timestamp}:{event_type}:{node_id}:{payload_json}:{prev_hash}")`
- [ ] First entry's prev_hash is "genesis"
- [ ] Writes use `BEGIN IMMEDIATE` to serialize concurrent writes and prevent chain corruption
- [ ] `verify_chain(limit=None)` walks all entries in seq order; returns {valid: bool, checked: int, broken_at: int|None, detail: str}
- [ ] Chain verification detects both hash mismatches and prev_hash discontinuities
- [ ] `query(event_type, node_id, since, limit)` returns filtered entries with parsed payload
- [ ] `latest(count)` returns the N most recent entries
- [ ] `stats` property returns: total, by_type counts, last_entry_at, last_hash_prefix, chain_valid (quick check on last 10)
- [ ] SQLite uses WAL mode with busy_timeout=5000ms; indexes on event_type, node_id, timestamp
- [ ] Optional `data_layer` integration: when set, uses managed connection instead of creating new per call
- [ ] Supported event_types include: merge_decision, lease_granted, rebuttal_resolved, gains_gate_result, channel_repair, consensus_reached, probe_finding, truth_entry, truth_promotion, ghost.corroboration, ghost.mirror.drift_detected, memory_learning, scan_finding

**Dependencies:** None (foundational)

### R2: Bidirectional Evidence Sync

**Description:** Evidence flows both ways between fleet nodes (via EvidenceSync) and between fleet and ContextDNA (via ContextDNAEvidenceBridge).

**Acceptance Criteria:**
- [ ] `EvidenceSync.export_recent(since_hours, limit)` returns portable packet with entries stripped of internal fields
- [ ] `EvidenceSync.import_packet(packet)` records remote entries with event_type prefixed "remote_"; skips duplicates by entry_id
- [ ] `EvidenceSync.send_to_node(target, since_hours)` tries P2 (HTTP POST to daemon /message) first, falls back to P7 (write JSON to .fleet-messages/{target}/)
- [ ] `EvidenceSync.broadcast(since_hours, targets)` sends to all fleet nodes except self; returns {node: success_bool}
- [ ] Stats track: exported, imported, duplicates_skipped, send_successes, send_failures
- [ ] `ContextDNAEvidenceBridge.push_to_contextdna()` pushes unpromoted evidence entries via MemoryBridge.promote_entry() (local) or ProductBridge.fleet_to_memory() (HTTP fallback)
- [ ] `ContextDNAEvidenceBridge.pull_from_contextdna(keywords)` queries memory via ProductBridge.memory_to_fleet(); records as event_type="memory_learning" in evidence ledger; deduplicates by SHA-256 content hash
- [ ] `sync_cycle()` runs full bidirectional push+pull and returns {pushed, pulled, elapsed_s}
- [ ] Sync watermarks (last_push_ts, last_pull_ts, pushed_hashes, pulled_hashes) persist to `~/.fleet-nerve/sync_state.json`; hash lists bounded to last 500
- [ ] Event hooks: `on_race_completed(task_id, result)` immediately pushes race evidence; `on_probe_finding(finding)` records and pushes probe findings
- [ ] Push limits to 10 entries per cycle to avoid blocking synthesis

**Dependencies:** R1 (EvidenceLedger), ProductBridge (external)

### R3: Wisdom Consumer

**Description:** Reads promoted wisdom (evidence entries with wisdom/promoted status + JSON files in `~/.context-dna/fleet-wisdom/`) and feeds proven patterns back into synthesis decisions.

**Acceptance Criteria:**
- [ ] `WisdomConsumer` loads from two sources: evidence ledger (entries matching wisdom/promoted/consensus_reached event types) and wisdom JSON files (written by MemoryBridge)
- [ ] Sources are deduplicated by entry_id; sorted by timestamp descending
- [ ] Loading is cached with 60-second TTL; `invalidate_cache()` forces reload
- [ ] `get_wisdom_for_task(task_keywords)` returns matching entries (keyword overlap >= 1), capped at 20 results
- [ ] `feed_synthesis(synthesis_result)` checks decision against proven wisdom for contradictions
- [ ] Contradiction detection uses keyword signal sets per decision type: e.g., "reject" contradicts entries containing "merge"/"approved"/"clean"
- [ ] Returns {warnings: [{type, message, wisdom_entry}], supporting: [entry_dicts], wisdom_checked: int}
- [ ] Keyword extraction filters noise words and caps at 20 keywords

**Dependencies:** R1 (EvidenceLedger)

### R4: Memory Influence Resolver

**Description:** During synthesis, queries past decisions from evidence ledger and JSONL decision log to show which memories influenced the current verdict. Two-tier: local evidence (fast) and chief node escalation (when confidence < 0.7 or evidence sparse).

**Acceptance Criteria:**
- [ ] `MemoryInfluenceResolver.resolve(task_id, verdicts, current_confidence)` returns MemoryInfluence with evidence_refs, pattern_summary, confidence_adjustment, authority ("local" or "chief")
- [ ] Tier 1 (local): queries evidence ledger for merge_decision entries + decisions.jsonl; matches by keyword overlap (>= 2 keywords)
- [ ] Tier 2 (chief escalation): triggers when confidence < 0.7 or fewer than 2 relevant local decisions; queries via ProductBridge.fleet_to_memory()
- [ ] Evidence refs capped at 10; each contains entry_id, event_type, node_id, timestamp, entry_hash, reasoning_snippet
- [ ] Pattern summary built from past decision type distribution (e.g., "merge: 3/5, reject: 2/5") plus common dissent themes
- [ ] Confidence adjustment: positive when approvals > rejections (+0.05 per net approval), negative when rejections > approvals (-0.1 per net rejection)
- [ ] Keywords extracted from task_id, verdict summaries, and branch names; noise words filtered
- [ ] `MemoryInfluence.summary_line()` returns one-line dashboard-ready string

**Dependencies:** R1 (EvidenceLedger), ProductBridge (external)

### R5: Ghost Layer Ambient Cognition

**Description:** Seven sub-modules operating as the system's subconscious: pulse (heartbeat), digest (session ingestion), mirror (docs drift), anticipate (predictive pre-staging), corroborate (cross-reference findings), checkpoint (session continuity), objections (unresolved concerns). Orchestrated by GhostLayer.

**Acceptance Criteria:**

#### R5a: Ghost Pulse
- [ ] `GhostPulse.pulse()` returns PulseResult with: stale_beliefs_expired, contradictions_found, repeated_failures_detected, changes_since_last, warnings
- [ ] Stale beliefs: counts entries older than max_staleness_hours (default 48h) in decisions.jsonl
- [ ] Contradictions: detects same task_id with conflicting decisions
- [ ] Repeated failures: tasks with >= 2 rejections/needs_rebuttal
- [ ] Changes since last: runs `git log --since` to detect new commits
- [ ] Triggers idle probe scan via probe_engine (non-blocking)
- [ ] Triggers ghost_mirror scan; drift adds to warnings
- [ ] Checks for stale provisional leases (>10 min unreconciled)
- [ ] Checks probe staleness; warns if probes have never run
- [ ] Warns on: evidence ledger > 10MB, fleet messages > 7 days old, CLAUDE.md > 14 days old
- [ ] Emits GHOST_PULSE_COMPLETED event

#### R5b: Ghost Digest
- [ ] `GhostDigest.digest(task_id, ...)` produces DigestResult with: evidence_candidates, unresolved_objections, future_warnings, packet_hints
- [ ] Extracts evidence from synthesis result (decision, cherry-picks as learning candidates)
- [ ] Extracts evidence from probe findings (high/critical severity only)
- [ ] Reads recent probe evidence from evidence ledger (ghost_digest <-> probe wiring)
- [ ] Extracts warnings from acceptance decisions (blocking issues, required followups)
- [ ] Always records a "Task completed" evidence candidate
- [ ] Feedback loop protection (CV-FLC): already-digested task_ids are skipped; seen-set capped at 1000
- [ ] Persists DigestResult to `~/.fleet-nerve/evidence-candidates/{task_id}.json`
- [ ] Emits GHOST_SESSION_DIGESTED event

#### R5c: Ghost Mirror
- [ ] `GhostMirror.scan()` returns MirrorResult with: docs_checked, drift_items, fix_suggestions
- [ ] Runs mirror_drift probe internally; checks CLAUDE.md, README.md, ARCHITECTURE.md
- [ ] Each drift item has: doc, missing_ref, file_paths
- [ ] Fix suggestions are actionable: "Update {doc}: remove or fix reference to '{ref}'"
- [ ] Drift is recorded to evidence ledger as ghost.mirror.drift_detected
- [ ] Emits GHOST_MIRROR_SCANNED event

#### R5d: Ghost Anticipate
- [ ] `GhostAnticipate.anticipate()` returns AnticipateResult with predictions and backlog_items_added
- [ ] Prediction kinds: file_edit (from recent commits + staged files), memory_query (from branch name), probe_run (from file patterns)
- [ ] Predictions ranked by confidence (0.0-1.0); fed to productive_wait backlog when confidence >= 0.6
- [ ] File edit predictions: recently modified files get 0.7 confidence; staged files get 0.8
- [ ] Doc file changes predict mirror_drift probe (0.8); dependency file changes predict dependency_drift (0.85)
- [ ] Emits GHOST_ANTICIPATE_COMPLETED event

#### R5e: Ghost Orchestrator
- [ ] `GhostLayer.status()` returns GhostStatus with per-module availability and aggregate health
- [ ] Health requires at least 3 of 4 core modules (pulse, digest, checkpoint, objections) available
- [ ] `GhostLayer.cycle(reason)` runs: pulse -> contradiction scan -> checkpoint; returns summary
- [ ] `session_end()` carries forward open objections + captures final checkpoint
- [ ] `session_start()` restores latest checkpoint + reports open objections and unresolved contradictions
- [ ] All sub-module failures are caught and reported; ghost layer never crashes the host

**Dependencies:** R1 (EvidenceLedger), ProbeEngine (external), ProductiveWaiter (external)

### R6: Cross-Node Evidence Corroboration and Synthesis

**Description:** Three modules handle cross-node evidence: GhostCorroborate (cross-references findings with history), GhostContradictions (detects conflicting truths), CrossNodeSynthesizer (reweights evidence from multiple nodes).

**Acceptance Criteria:**

#### R6a: Ghost Corroborate
- [ ] `GhostCorroborate.corroborate(findings)` returns CorroborateResult with per-finding assessments
- [ ] Recurring findings (>= 3 prior occurrences) are "strengthened": +0.1 per occurrence, capped at +0.25
- [ ] Novel findings (0 prior occurrences) are "novel": -0.05 penalty
- [ ] 1-2 prior occurrences: "unchanged" with minor boost (+0.05 per occurrence)
- [ ] Confidence bounded: floor 0.1, ceiling 0.95
- [ ] Prior occurrence matching: >50% keyword overlap between finding title and history entry titles
- [ ] History loaded from evidence ledger (event_type="probe_finding", limit 100)
- [ ] Context DNA evidence pulled via ProductBridge.memory_to_fleet() and merged into history
- [ ] Corroboration results recorded to evidence ledger as ghost.corroboration
- [ ] Corroboration results pushed to Context DNA via ProductBridge (closes learning loop)
- [ ] Emits GHOST_CORROBORATE_COMPLETED event

#### R6b: Ghost Contradictions
- [ ] `ContradictionDetector.scan()` returns newly found contradictions
- [ ] Scans decisions.jsonl for same task_id with different decisions (task conflicts)
- [ ] Scans evidence-candidates directory for same topic from different nodes (cross-node conflicts)
- [ ] Severity assessment: high (>= 3 nodes or >= 4 claims), medium (>= 2 nodes), low (other)
- [ ] `list_unresolved()` returns contradictions where resolved == False
- [ ] `resolve(contradiction_id, resolution)` marks as resolved with explanation
- [ ] Persisted to `~/.fleet-nerve/ghost/contradictions.json`

#### R6c: Cross-Node Synthesizer
- [ ] `CrossNodeSynthesizer.synthesize(task_id, node_evidence)` returns SynthesisResult with weighted consensus
- [ ] Weight formula per node: confidence + min(0.3, evidence_refs * 0.1) - min(0.3, objections * 0.1) + (0.1 if probes clean else -0.2 if blocking)
- [ ] Consensus by weighted vote: decision with highest total weight wins
- [ ] consensus_confidence = winner_weight / total_weight
- [ ] Synthesis method: "unanimous" (all agree), "majority_weighted" (>= 3 nodes), "weighted_evidence" (default)
- [ ] Dissent preserved: lists nodes that disagreed with their decision and confidence
- [ ] Results persisted to `~/.fleet-nerve/ghost/synthesis/{task_id}.json`
- [ ] [GAP] CrossNodeSynthesizer operates on file-persisted results but has no mechanism to collect NodeEvidence packets from remote nodes -- collection depends on external orchestration

**Dependencies:** R1 (EvidenceLedger), ProductBridge (external)

### R7: Ghost Objections and Checkpoints

**Description:** Objections store tracks unresolved surgeon reservations, probe red flags, and dissent across sessions. Checkpoints capture full system state for session continuity.

**Acceptance Criteria:**

#### R7a: Ghost Objections
- [ ] `ObjectionStore.record(claim, source, source_node, task_id, evidence)` creates an Objection with unique 12-char ID, status "open"
- [ ] Sources: surgeon, probe, pulse, manual, digest
- [ ] `resolve(objection_id, resolution)` sets status to "resolved" with timestamp and explanation
- [ ] `dismiss(objection_id, reason)` sets status to "dismissed"; requires non-empty reason (dismissal without reason is forbidden)
- [ ] `carry_forward()` marks all open objections as "carried" with incremented carry_count; called at session end
- [ ] `list_open()` returns objections with status "open" or "carried"
- [ ] JSONL-backed: latest entry per objection_id wins (append-only, last-write-wins)
- [ ] `stats()` returns total, by_status counts, oldest_open timestamp

#### R7b: Ghost Checkpoint
- [ ] `GhostCheckpoint.capture(reason)` saves full system state: git (branch, dirty_files, last_commit), session (session_id, model), fleet (daemon_healthy, peer_count, active_peers), ghost (pulse_summary, objections, warnings, evidence_candidates), workflow (phase, last_synthesis_task)
- [ ] Partial checkpoint is always better than no checkpoint -- all data gathering uses try/except
- [ ] `restore(checkpoint_id="latest")` loads and returns CheckpointData; supports fuzzy match on checkpoint_id
- [ ] `list_checkpoints()` returns available checkpoints, newest first
- [ ] Keeps last 10 checkpoints; oldest pruned on each capture
- [ ] Stored as JSON files in `~/.fleet-nerve/ghost/checkpoints/`

**Dependencies:** R1 (EvidenceLedger, indirect via pulse/objections)

### R8: Memory Bridge (Evidence to ContextDNA Promotion)

**Description:** Promotes fleet evidence entries into ContextDNA's persistent memory system. This is the moat -- intelligence that compounds over time.

**Acceptance Criteria:**
- [ ] `MemoryBridge.promote_recent(hours, min_severity)` filters by time window, severity rank (low=0, medium=1, high=2, critical=3), and already-promoted status
- [ ] `promote_entry(entry)` executes 5-step pipeline: write wisdom JSON file, call brain.py, call auto_capture (for successes), emit evidence.promoted EventBus event, mark as promoted in tracker
- [ ] Wisdom files written to `~/.context-dna/fleet-wisdom/` as JSON with fields: source, entry_id, event_type, node_id, subject, action, area, evidence_hash, promoted_at, original_timestamp, content
- [ ] Already-promoted entry IDs tracked in `~/.context-dna/fleet-wisdom/.promoted.json`
- [ ] Event type mapping: merge_decision -> (success, architecture), race_completed -> (success, fleet-race), scan_finding -> (fix, scan-finding), etc.
- [ ] brain.py called via subprocess with 5s timeout; failure is non-blocking
- [ ] auto_capture called via subprocess for success actions; failure is non-blocking
- [ ] `run_promotion_cycle()` runs promote_recent(24h, medium) and returns {promoted_count, promoted_ids, elapsed_s, total_promoted}
- [ ] `get_wisdom_stats()` returns: total_promoted, wisdom_files, by_event_type, last_promotion, session_promoted, memory_path

**Dependencies:** R1 (EvidenceLedger), brain.py / auto_capture (external, optional)

### R9: ContextDNA Analyzer

**Description:** Pluggable analyzer registered in ChiefSynthesis. Compares fleet verdicts against ContextDNA evidence store, prior learnings, and architectural decisions to produce evidence-informed merge recommendations.

**Acceptance Criteria:**
- [ ] `create_contextdna_analyzer()` returns a callable or None if no memory source is available
- [ ] Analyzer discovery chain: memory.query.query_learnings -> memory.brain.Brain.context -> evidence ledger keyword search (local fallback)
- [ ] Professor consultation: memory.professor.consult called for task wisdom (once per synthesis, optional)
- [ ] Scoring adjustments: PRIOR_SUCCESS_BOOST=+0.10, PRIOR_FAILURE_PENALTY=-0.15, ARCH_DECISION_BOOST=+0.05
- [ ] File-specific failure detection: queries memory with file paths + failure keywords; file failures override general success signal
- [ ] Architectural decision detection: queries memory for design/pattern/rationale keywords
- [ ] Probe evidence cross-reference: critical probe findings on touched files apply -0.15 penalty; high findings apply -0.075
- [ ] ContextDNA evidence pulled via ProductBridge.memory_to_fleet() with 3s SIGALRM timeout
- [ ] Strategic planner consulted for blind spots (optional, non-blocking)
- [ ] If all adjustments are zero and no professor wisdom, returns None (lets default analyzer handle)
- [ ] Analysis results pushed back to ContextDNA via ProductBridge (closes learning loop)
- [ ] metadata["memory_reasons"] contains per-machine breakdown: memory_matches, delta, source, prior_failures, arch_decisions

**Dependencies:** R1 (EvidenceLedger), ProductBridge (external), memory.query/brain/professor (external, optional)

## Out of Scope

- Synthesis decision logic and merge execution (see cavekit-synthesis)
- Transport layer implementation (NATS, HTTP, git channels)
- Probe engine implementation (consumed as dependency)
- Surgeon adapter / 3-surgeon protocol (consumed as dependency)
- ProductBridge HTTP client implementation (consumed as dependency)
- Quorum sensing system (separate domain)
- Race execution and worktree management (separate domain)
- EventBus WebSocket server (separate domain)
- brain.py, auto_capture, professor.py, strategic_planner.py (ContextDNA internals, consumed as optional external dependencies)

## Cross-References

- See also: cavekit-synthesis.md (consumes EvidenceLedger, WisdomConsumer, MemoryInfluenceResolver, GhostCorroborate, ContextDNA analyzer)
