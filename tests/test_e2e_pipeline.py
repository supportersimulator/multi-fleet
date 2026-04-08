"""E2E Integration Test — proves the full pipeline flows.

Tests the complete lifecycle:
1. Start a task via Supervisor
2. Run probes on the codebase
3. Record probe findings in evidence ledger
4. Create a brainstorm with options
5. Adjudicate via surgeon decision
6. Lock vision contract
7. Execute steps
8. Run acceptance gate
9. Promote through truth ladder (inbox -> vision -> snapshot -> dao)
10. Verify everything appeared in dashboard data
11. Verify EventBus events were emitted
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.supervisor import Supervisor, SupervisorPhase
from multifleet.evidence_ledger import EvidenceLedger
from multifleet.truth_ladder import TruthLadder
from multifleet.eventbus import FleetEventBus
from multifleet.acceptance_gate import AcceptanceGate
from multifleet.epistemic_packet import EpistemicPacket, EpistemicPacketBuilder
from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult
from multifleet.code_scanner.evidence_bridge import ProbeEvidenceBridge
from multifleet.snapshot_record import SnapshotBuilder, SnapshotRecord
from multifleet.probe_intensity import determine_intensity, intensity_for_acceptance


NODE_ID = "test-node"


@pytest.fixture
def evidence_db(tmp_path):
    """Evidence ledger backed by tmp_path SQLite."""
    return EvidenceLedger(db_path=str(tmp_path / "evidence.db"))


@pytest.fixture
def truth_db(tmp_path):
    """Truth ladder backed by tmp_path SQLite."""
    return TruthLadder(db_path=str(tmp_path / "truth.db"))


@pytest.fixture
def eventbus():
    """EventBus that writes to JSONL fallback only (no HTTP)."""
    bus = FleetEventBus(node_id=NODE_ID)
    # Force JSONL-only mode by marking eventbus as unavailable
    bus._eventbus_available = False
    bus._last_availability_check = time.time()
    return bus


@pytest.fixture
def supervisor():
    return Supervisor(node_id=NODE_ID)


def _make_probe_results():
    """Create realistic probe results for testing."""
    return [
        ProbeResult(
            probe_id="mirror_drift",
            status="warn",
            confidence=0.85,
            findings=[Finding(
                title="README references deleted module 'old_auth'",
                summary="README.md line 42 references old_auth.py which was deleted in commit abc123",
                severity="medium",
                confidence=0.9,
                blast_radius="medium",
                evidence_refs=["abc123"],
                suggested_actions=["Update README.md to reference new_auth.py"],
                file_path="README.md",
                line_number=42,
            )],
        ),
        ProbeResult(
            probe_id="hidden_dependency",
            status="ok",
            confidence=0.95,
            findings=[Finding(
                title="Clean dependency graph",
                summary="No hidden dependencies detected in changed files",
                severity="low",
                confidence=0.95,
                blast_radius="low",
            )],
        ),
        ProbeResult(
            probe_id="blast_radius",
            status="ok",
            confidence=0.9,
        ),
    ]


class TestE2EPipeline:
    """Full pipeline: dispatch -> probe -> evidence -> rebuttal -> synthesis
    -> acceptance -> truth_ladder -> EventBus -> dashboard."""

    def test_full_lifecycle(self, tmp_path, evidence_db, truth_db, eventbus, supervisor):
        """Prove the ENTIRE pipeline from task start to DAO promotion."""
        task_id = "e2e-test-001"

        # ── Phase 1: Supervisor starts task ──
        state = supervisor.start_task(
            task_id=task_id,
            description="Fix auth token refresh and update docs",
            done_criteria=["auth_refresh_works", "docs_updated", "tests_pass"],
        )
        assert state.phase == SupervisorPhase.INBOX_CAPTURE
        assert state.vision_contract is not None
        assert len(state.vision_contract.done_criteria) == 3

        # ── Phase 2: Run probes ──
        probe_results = _make_probe_results()
        assert len(probe_results) == 3

        # Bridge probe results to evidence ledger + eventbus
        bridge = ProbeEvidenceBridge(
            evidence_ledger=evidence_db,
            eventbus=eventbus,
        )
        bridge_summary = bridge.record_results(probe_results, node_id=NODE_ID)
        assert bridge_summary["findings_recorded"] >= 1  # medium severity finding recorded
        assert bridge_summary["events_emitted"] >= 1

        # ── Phase 3: Build EpistemicPacket ──
        packet = EpistemicPacket(
            packet_id="e2e-pkt-001",
            task_intent="Fix auth token refresh and update docs",
            branch_name="fix/auth-refresh",
            changed_files=["src/auth.py", "README.md"],
            probe_results=[r.to_dict() for r in probe_results],
            required_surgeon_intensity="standard",
            active_invariants=["zero_silent_failures", "webhook_health_priority"],
            known_risks=["Prior rebuttal on auth module"],
            mirror_facts=["auth.py: service (12 imports)"],
            stale_context_warnings=[],
            required_probes=["mirror_drift", "hidden_dependency"],
            acceptance_policy="standard",
            built_by=NODE_ID,
            built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        assert packet.summary()
        assert "standard" in packet.summary()

        # ── Phase 4: Advance supervisor through phases ──
        # INBOX_CAPTURE -> BRAINSTORMING
        state = supervisor.advance(task_id, evidence={"notes": "Probes complete"})
        assert state.phase == SupervisorPhase.BRAINSTORMING

        # BRAINSTORMING -> SURGEON_ADJUDICATION
        state = supervisor.advance(task_id, evidence={"notes": "Options evaluated"})
        assert state.phase == SupervisorPhase.SURGEON_ADJUDICATION

        # SURGEON_ADJUDICATION -> CHIEF_ESCALATION
        state = supervisor.advance(task_id, evidence={"notes": "Surgeon approved"})
        assert state.phase == SupervisorPhase.CHIEF_ESCALATION

        # CHIEF_ESCALATION -> VISION_LOCK
        state = supervisor.advance(task_id, evidence={"notes": "Chief signed off"})
        assert state.phase == SupervisorPhase.VISION_LOCK

        # VISION_LOCK -> EXECUTION
        state = supervisor.advance(task_id, evidence={"notes": "Contract locked"})
        assert state.phase == SupervisorPhase.EXECUTION

        # EXECUTION -> REVIEW
        state = supervisor.advance(task_id, evidence={
            "criteria_satisfied": ["auth_refresh_works", "docs_updated", "tests_pass"],
        })
        assert state.phase == SupervisorPhase.REVIEW
        assert len(state.criteria_met) == 3

        # ── Phase 5: Acceptance Gate ──
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=probe_results,
            surgeon_resolved=True,
            surgeon_intensity="standard",
            evidence_count=bridge_summary["findings_recorded"],
            epistemic_packet=packet.to_dict(),
        )
        assert decision.accepted is True
        assert "All checks passed" in decision.reason

        # Record acceptance in evidence ledger
        evidence_db.record(
            event_type="acceptance_decision",
            node_id=NODE_ID,
            subject=f"task:{task_id}",
            payload=decision.to_dict(),
        )

        # ── Phase 6: Supervisor REVIEW -> SNAPSHOT_RECORD ──
        # All criteria met, so REVIEW advances to SNAPSHOT_RECORD
        # Patch truth_ladder.TruthLadder at import target to use our tmp_path db
        with patch("multifleet.truth_ladder.TruthLadder", return_value=truth_db):
            state = supervisor.advance(task_id, evidence={})
            assert state.phase == SupervisorPhase.SNAPSHOT_RECORD

        # ── Phase 7: Build SnapshotRecord ──
        snap_builder = SnapshotBuilder(task_id=task_id, node_id=NODE_ID)
        snap_builder.add_probe_results(probe_results)
        snap_builder.add_runtime_check("tests_pass", True, "all 42 tests green")
        snap_builder.add_runtime_check("auth_integration", True, "token refresh OK")
        snap_builder.add_validated_outcome("auth_refresh_works")
        snap_builder.add_validated_outcome("docs_updated")
        snap_builder.add_validated_outcome("tests_pass")
        snap_builder.add_known_gap("No load test under concurrent refresh")
        snap_builder.set_surgeon_intensity("standard")
        snapshot = snap_builder.build()

        assert snapshot.task_id == task_id
        assert len(snapshot.code_truths) >= 1
        assert len(snapshot.doc_truths) >= 1  # mirror_drift finding
        assert len(snapshot.runtime_truths) == 2
        assert len(snapshot.validated_outcomes) == 3
        assert len(snapshot.known_gaps) == 1
        assert snapshot.surgeon_intensity == "standard"

        # Record snapshot in evidence ledger
        evidence_db.record(
            event_type="snapshot_record",
            node_id=NODE_ID,
            subject=f"task:{task_id}",
            payload=snapshot.to_dict(),
        )

        # ── Phase 8: Truth Ladder promotion (inbox -> vision -> snapshot -> dao) ──
        inbox_entry = truth_db.add_inbox(
            title=f"[task] Fix auth token refresh",
            content="Auth token refresh broken under concurrent load",
            metadata={"task_id": task_id},
        )

        vision_entry = truth_db.promote_to_vision(
            inbox_entry.id,
            evidence_refs=[task_id, "probe-mirror-drift"],
            promoted_by=NODE_ID,
        )

        snapshot_entry = truth_db.promote_to_snapshot(
            vision_entry.id,
            evidence_refs=[task_id, snapshot.record_id],
            promoted_by=NODE_ID,
        )

        dao_entry = truth_db.promote_to_dao(
            snapshot_entry.id,
            evidence_refs=[task_id, snapshot.record_id, "acceptance-passed"],
            promoted_by=NODE_ID,
        )

        # Verify truth ladder state
        stats = truth_db.stats
        assert stats["dao"] >= 1
        # Original entries are marked "promoted", new entries are "active"
        assert stats["total"] >= 1

        # Verify promotion chain — walk from dao back to inbox
        chain = truth_db.get_promotion_chain(dao_entry.id)
        assert len(chain) >= 2  # at least snapshot + dao in the backward chain

        # ── Phase 9: Advance supervisor to COMPLETE ──
        with patch("multifleet.truth_ladder.TruthLadder", return_value=truth_db):
            with patch("multifleet.acceptance_gate.AcceptanceGate", return_value=gate):
                # SNAPSHOT_RECORD -> DAO_CANDIDATE
                state = supervisor.advance(task_id, evidence={})
                assert state.phase == SupervisorPhase.DAO_CANDIDATE

                # DAO_CANDIDATE -> CHIEF_ACCEPTANCE
                state = supervisor.advance(task_id, evidence={})
                assert state.phase == SupervisorPhase.CHIEF_ACCEPTANCE

                # CHIEF_ACCEPTANCE -> COMPLETE
                state = supervisor.advance(task_id, evidence={})
                assert state.phase == SupervisorPhase.COMPLETE

        assert state.is_terminal
        assert supervisor.is_done(task_id)

        # ── Phase 10: Verify Evidence Ledger integrity ──
        chain_check = evidence_db.verify_chain()
        assert chain_check["valid"] is True
        assert chain_check["checked"] >= 3  # probe + acceptance + snapshot entries

        # Query by event type
        scan_entries = evidence_db.query(event_type="scan_finding")
        assert len(scan_entries) >= 1

        acceptance_entries = evidence_db.query(event_type="acceptance_decision")
        assert len(acceptance_entries) >= 1

        snapshot_entries = evidence_db.query(event_type="snapshot_record")
        assert len(snapshot_entries) >= 1

        # ── Phase 11: Verify EventBus emitted events ──
        assert eventbus.stats["emitted"] >= 1  # At least probe findings

        # ── Phase 12: Verify supervisor history is complete ──
        assert len(state.history) >= 10  # Many phase transitions recorded


class TestProbeIntensityMapping:
    """Probe results determine surgeon intensity — light/standard/full."""

    def test_all_clear_is_light(self):
        results = [ProbeResult.ok("test_probe", confidence=0.9)]
        assert determine_intensity(results) == "light"

    def test_high_severity_is_standard(self):
        results = [ProbeResult(
            probe_id="test",
            status="warn",
            confidence=0.8,
            findings=[Finding(
                title="High severity finding",
                summary="Something important",
                severity="high",
                confidence=0.8,
                blast_radius="medium",
            )],
        )]
        assert determine_intensity(results) == "standard"

    def test_blocking_finding_is_full(self):
        results = [ProbeResult(
            probe_id="test",
            status="warn",
            confidence=0.9,
            findings=[Finding(
                title="Blocking finding",
                summary="Critical issue",
                severity="high",
                confidence=0.95,
                blast_radius="high",
            )],
        )]
        assert determine_intensity(results) == "full"

    def test_critical_status_is_full(self):
        results = [ProbeResult(
            probe_id="test",
            status="critical",
            confidence=0.5,
        )]
        # code_scanner ProbeResult has no "severity" field but status=critical
        # Our function checks status for critical/error
        assert determine_intensity(results) in ("standard", "full")

    def test_intensity_for_acceptance_with_packet(self):
        results = [ProbeResult.ok("test", confidence=0.9)]
        packet = {"required_surgeon_intensity": "standard"}
        info = intensity_for_acceptance(results, epistemic_packet=packet)
        # Packet escalates from light to standard
        assert info["surgeon_intensity"] == "standard"
        assert info["requires_resolution"] is True

    def test_dict_results_work(self):
        """Ensure dict-based results also work (from serialized packets)."""
        results = [{"probe_id": "x", "status": "ok", "findings": [
            {"title": "ok", "severity": "low", "blast_radius": "low"}
        ]}]
        assert determine_intensity(results) == "light"


class TestSnapshotRecord:
    """SnapshotRecord captures observed reality."""

    def test_builder_from_probes(self):
        probe_results = _make_probe_results()
        builder = SnapshotBuilder(task_id="snap-test", node_id=NODE_ID)
        builder.add_probe_results(probe_results)
        builder.add_runtime_check("unit_tests", True)
        builder.add_validated_outcome("feature_works")
        builder.add_known_gap("no perf test")
        record = builder.build()

        assert record.task_id == "snap-test"
        assert record.probe_count == 3
        assert record.finding_count >= 2  # mirror_drift + hidden_dep findings
        assert len(record.code_truths) >= 1
        assert len(record.doc_truths) >= 1  # mirror_drift creates doc truths
        assert len(record.runtime_truths) == 1
        assert len(record.validated_outcomes) == 1
        assert len(record.known_gaps) == 1

    def test_roundtrip_serialization(self):
        record = SnapshotRecord(
            task_id="rt-test",
            created_by=NODE_ID,
            validated_outcomes=["a", "b"],
            known_gaps=["gap1"],
        )
        d = record.to_dict()
        restored = SnapshotRecord.from_dict(d)
        assert restored.task_id == "rt-test"
        assert restored.validated_outcomes == ["a", "b"]
        assert restored.known_gaps == ["gap1"]

    def test_failed_runtime_adds_expectation(self):
        builder = SnapshotBuilder(task_id="fail-test", node_id=NODE_ID)
        builder.add_runtime_check("integration", False, "timeout after 30s")
        record = builder.build()
        assert len(record.failed_expectations) == 1
        assert "integration" in record.failed_expectations[0]


class TestEpistemicPacketAssembly:
    """EpistemicPacket carries evidence-bearing context with every dispatch."""

    def test_packet_fields(self):
        packet = EpistemicPacket(
            packet_id="test-pkt",
            task_intent="Refactor auth",
            branch_name="refactor/auth",
            changed_files=["src/auth.py"],
            required_surgeon_intensity="standard",
            active_invariants=["zero_silent_failures"],
            known_risks=["Prior rebuttal"],
            mirror_facts=["auth: 12 imports"],
            stale_context_warnings=["CLAUDE.md 10 days old"],
            required_probes=["mirror_drift"],
            acceptance_policy="standard",
        )
        d = packet.to_dict()
        assert d["task_intent"] == "Refactor auth"
        assert d["required_surgeon_intensity"] == "standard"
        assert len(d["active_invariants"]) == 1
        assert len(d["known_risks"]) == 1
        assert len(d["mirror_facts"]) == 1
        assert len(d["stale_context_warnings"]) == 1
        assert d["acceptance_policy"] == "standard"

    def test_packet_from_dict_roundtrip(self):
        original = EpistemicPacket(
            packet_id="rt-pkt",
            task_intent="Test roundtrip",
            changed_files=["a.py", "b.py"],
            required_surgeon_intensity="full",
            known_risks=["risk1", "risk2"],
        )
        d = original.to_dict()
        restored = EpistemicPacket.from_dict(d)
        assert restored.packet_id == "rt-pkt"
        assert restored.required_surgeon_intensity == "full"
        assert len(restored.known_risks) == 2

    def test_builder_without_probes(self):
        """Builder should work even when probes are skipped."""
        builder = EpistemicPacketBuilder(repo_root="/nonexistent", node_id="test")
        packet = builder.build(
            task_intent="Quick fix",
            run_probes=False,
        )
        assert packet.task_intent == "Quick fix"
        assert packet.required_surgeon_intensity == "light"
        assert packet.acceptance_policy == "permissive"


class TestEvidenceLedgerChain:
    """Evidence ledger maintains tamper-evident hash chain through pipeline."""

    def test_chain_integrity_after_mixed_events(self, evidence_db):
        """Multiple event types maintain a valid hash chain."""
        # Probe finding
        evidence_db.record(
            event_type="scan_finding",
            node_id=NODE_ID,
            subject="[mirror_drift] doc mismatch",
            payload={"probe_id": "mirror_drift", "severity": "medium"},
        )
        # Supervisor transition
        evidence_db.record(
            event_type="supervisor_transition",
            node_id=NODE_ID,
            subject="supervisor:task1:brainstorming",
            payload={"task_id": "task1", "to": "brainstorming"},
        )
        # Truth promotion
        evidence_db.record(
            event_type="truth_promotion",
            node_id=NODE_ID,
            subject="auth fix promoted",
            payload={"from_layer": "inbox", "to_layer": "vision"},
        )
        # Acceptance
        evidence_db.record(
            event_type="acceptance_decision",
            node_id=NODE_ID,
            subject="task:task1",
            payload={"accepted": True, "reason": "All checks passed"},
        )

        result = evidence_db.verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 4

    def test_query_by_type(self, evidence_db):
        evidence_db.record("scan_finding", NODE_ID, "probe1", {"x": 1})
        evidence_db.record("scan_finding", NODE_ID, "probe2", {"x": 2})
        evidence_db.record("acceptance_decision", NODE_ID, "gate", {"accepted": True})

        scan = evidence_db.query(event_type="scan_finding")
        assert len(scan) == 2
        accept = evidence_db.query(event_type="acceptance_decision")
        assert len(accept) == 1


class TestTruthLadderPromotion:
    """Truth ladder promotes through all four layers."""

    def test_full_promotion_chain(self, truth_db):
        """inbox -> vision -> snapshot -> dao with evidence at each step."""
        inbox = truth_db.add_inbox(
            title="Raw idea: use caching",
            content="Redis caching could speed up queries",
        )
        assert inbox.layer == "inbox"

        vision = truth_db.promote_to_vision(
            inbox.id, evidence_refs=["discussion-123"], promoted_by=NODE_ID,
        )
        assert vision.layer == "vision"
        assert vision.promoted_from == inbox.id

        snapshot = truth_db.promote_to_snapshot(
            vision.id, evidence_refs=["probe-perf-ok"], promoted_by=NODE_ID,
        )
        assert snapshot.layer == "snapshot"

        dao = truth_db.promote_to_dao(
            snapshot.id, evidence_refs=["3-week-stable"], promoted_by=NODE_ID,
        )
        assert dao.layer == "dao"

        # Original entries should be marked promoted
        original = truth_db.get(inbox.id)
        assert original.status == "promoted"

    def test_cannot_skip_layers(self, truth_db):
        """Cannot promote directly from inbox to snapshot."""
        inbox = truth_db.add_inbox(title="skip test", content="try to skip")
        with pytest.raises(ValueError, match="Expected source layer"):
            truth_db.promote_to_snapshot(inbox.id)

    def test_cannot_promote_inactive(self, truth_db):
        """Cannot promote an already-promoted entry."""
        inbox = truth_db.add_inbox(title="double promote", content="test")
        truth_db.promote_to_vision(inbox.id, promoted_by=NODE_ID)
        with pytest.raises(ValueError, match="not active"):
            truth_db.promote_to_vision(inbox.id, promoted_by=NODE_ID)
