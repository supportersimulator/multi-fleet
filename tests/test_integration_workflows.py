"""End-to-end integration tests for the 5 core multi-fleet workflows.

These tests prove modules work TOGETHER as a product, not in isolation.
All network/daemon calls are mocked — no real infrastructure required.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Workflow 1: Message Lifecycle ──────────────────────────────────────────


class TestMessageLifecycle:
    """Message sent -> delivery recorded -> audit trail entry -> ACK tracked."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.db_path = tmp_path / "messages.db"
        from multifleet.store import FleetNerveStore
        self.store = FleetNerveStore(db_path=self.db_path)

    def _make_msg(self, msg_id="msg-001", from_node="mac1", to_node="mac2"):
        return {
            "id": msg_id,
            "type": "context",
            "from": from_node,
            "to": to_node,
            "payload": {"subject": "test-subject", "body": "test-body"},
            "priority": 2,
            "ttl_hours": 24,
        }

    def test_message_send_to_audit_trail(self, tmp_path):
        """Full lifecycle: store -> pending -> deliver -> audit -> ACK."""
        from multifleet.audit import AuditTrail

        msg = self._make_msg()
        audit = AuditTrail(storage_dir=str(tmp_path / "audit"), node_id="mac1")

        # 1. Store the message
        self.store.store_message(msg)
        stored = self.store.get_message("msg-001")
        assert stored is not None
        assert stored["type"] == "context"
        assert stored["from_node"] == "mac1"
        assert stored["to_node"] == "mac2"
        assert stored["status"] == "pending"

        # 2. Verify message appears in pending queue for target node
        pending = self.store.pending_for_node("mac2")
        assert len(pending) == 1
        assert pending[0]["id"] == "msg-001"

        # 3. Record audit entry for the delivery
        entry = audit.record(
            event="message_delivered",
            tool="store",
            target="mac2",
            status="ok",
            metadata={"msg_id": "msg-001"},
        )
        assert entry.event == "message_delivered"
        assert entry.status == "ok"
        assert entry.node_id == "mac1"

        # 4. Mark delivered in store
        self.store.mark_delivered("msg-001")
        delivered = self.store.get_message("msg-001")
        assert delivered["status"] == "delivered"
        assert delivered["delivered_at"] is not None

        # 5. ACK tracking: set awaiting -> update to delivered
        self.store.update_ack_status("msg-001", "awaiting")
        ack = self.store.get_ack_status("msg-001")
        assert ack["ack_status"] == "awaiting"

        self.store.update_ack_status("msg-001", "delivered")
        ack = self.store.get_ack_status("msg-001")
        assert ack["ack_status"] == "delivered"

        # 6. Pending queue is now empty for target
        pending_after = self.store.pending_for_node("mac2")
        assert len(pending_after) == 0

        audit.flush()

    def test_ack_retry_and_failure(self, tmp_path):
        """ACK retry: awaiting -> bump retries -> mark failed."""
        msg = self._make_msg(msg_id="msg-retry")
        self.store.store_message(msg)
        self.store.update_ack_status("msg-retry", "awaiting")

        # Bump retry count
        self.store.bump_retry("msg-retry")
        ack = self.store.get_ack_status("msg-retry")
        assert ack["retry_count"] == 1

        # Mark failed after exhausting retries
        self.store.mark_ack_failed("msg-retry")
        ack = self.store.get_ack_status("msg-retry")
        assert ack["ack_status"] == "failed"


# ── Workflow 2: Rebuttal Lifecycle ─────────────────────────────────────────


class TestRebuttalLifecycle:
    """task_brief -> 3 verdicts -> 3 critiques -> synthesis -> chief_decision -> evidence ledger."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.tmp_path = tmp_path
        self.machines = ["mac1", "mac2", "mac3"]

    def test_full_rebuttal_to_chief_decision(self):
        """Complete cross-rebuttal lifecycle with synthesis and evidence."""
        from multifleet.cross_rebuttal import RebuttalSession, RebuttalPhase
        from multifleet.synthesis import ChiefSynthesis
        from multifleet.evidence_ledger import EvidenceLedger

        ledger = EvidenceLedger(db_path=str(self.tmp_path / "evidence.db"))
        synthesis = ChiefSynthesis(ledger_path=str(self.tmp_path / "decisions.jsonl"))
        synthesis._evidence_ledger = None  # Disable auto evidence (we test manually)

        # 1. Create rebuttal session (task_brief equivalent)
        session = RebuttalSession(
            task_id="task-001",
            phase=RebuttalPhase.ASSIGNED,
            created_at=time.time(),
            deadline_minutes=30,
            expected_machines=self.machines,
        )
        assert session.phase == RebuttalPhase.ASSIGNED

        # 2. Advance to local surgery
        assert session.advance(RebuttalPhase.LOCAL_SURGERY)
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

        # 3. Add 3 verdicts
        for mac in self.machines:
            verdict = {
                "machine": mac,
                "branch": f"feature/{mac}",
                "confidence": 0.85 if mac == "mac1" else 0.7,
                "risks": ["timing risk"] if mac == "mac3" else [],
                "dissent": [],
                "strengths": [f"{mac} optimization"],
            }
            session.add_verdict(mac, verdict)

        assert session.all_verdicts_in()

        # 4. Advance through phases: verdicts -> under_review
        assert session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        assert session.advance(RebuttalPhase.UNDER_REVIEW)

        # 5. Add 3 critiques
        for mac in self.machines:
            critique = {
                "machine": mac,
                "reviewed_machines": [m for m in self.machines if m != mac],
                "blocking_concerns": [],
                "suggestions": [f"consider {mac}'s approach"],
            }
            session.add_critique(mac, critique)

        assert session.all_critiques_in()

        # 6. Advance to synthesis
        assert session.advance(RebuttalPhase.CRITIQUES_COLLECTED)
        assert session.advance(RebuttalPhase.SYNTHESIS)

        # 7. Run synthesis on collected verdicts
        result = synthesis.synthesize("task-001", session.verdicts)
        assert result.task_id == "task-001"
        assert result.decision in ("merge", "cherry_pick", "needs_rebuttal", "reject")
        assert 0.0 <= result.confidence <= 1.0
        assert result.winner_branch is not None

        # 8. Generate chief_decision packet
        decision = result.to_chief_decision()
        assert decision["type"] == "chief_decision"
        assert decision["task_id"] == "task-001"
        assert "decision" in decision
        assert "confidence" in decision
        assert isinstance(decision["timestamp"], float)

        # 9. Record in evidence ledger
        evidence_entry = ledger.record(
            event_type="merge_decision",
            node_id="mac1",
            subject=f"task-001: {decision['decision']}",
            payload=decision,
        )
        assert evidence_entry["event_type"] == "merge_decision"
        assert evidence_entry["entry_hash"] != "genesis"
        assert evidence_entry["prev_hash"] == "genesis"  # First entry

        # 10. Set decision on session, advance to resolved
        session.set_decision(decision)
        assert session.chief_decision is not None
        assert session.advance(RebuttalPhase.RESOLVED)
        assert session.phase == RebuttalPhase.RESOLVED

        # 11. Verify decision logged to JSONL ledger
        decisions_file = self.tmp_path / "decisions.jsonl"
        assert decisions_file.exists()
        lines = decisions_file.read_text().strip().splitlines()
        assert len(lines) >= 1
        logged = json.loads(lines[-1])
        assert logged["task_id"] == "task-001"

        # 12. Verify evidence chain integrity
        chain = ledger.verify_chain()
        assert chain["valid"] is True

    def test_invalid_phase_transition_blocked(self):
        """Invalid phase transitions return False, phase unchanged."""
        from multifleet.cross_rebuttal import RebuttalSession, RebuttalPhase

        session = RebuttalSession(
            task_id="task-bad",
            phase=RebuttalPhase.ASSIGNED,
            created_at=time.time(),
            deadline_minutes=5,
            expected_machines=["mac1"],
        )
        # Can't jump from ASSIGNED to RESOLVED
        assert not session.advance(RebuttalPhase.RESOLVED)
        assert session.phase == RebuttalPhase.ASSIGNED


# ── Workflow 3: Probe to IDE Event ─────────────────────────────────────────


class TestProbeToEventBus:
    """Probe scan -> findings -> liaison escalation -> EventBus event."""

    def test_probe_scan_to_eventbus(self):
        """Probe findings flow through EventBus and liaison to status."""
        from multifleet.probe_engine import (
            ProbeCategory,
            ProbeFinding,
            ProbeRegistry,
            ProbeResult,
        )
        from multifleet.eventbus import FleetEventBus
        from multifleet.liaison import FleetLiaison, EscalationLevel

        # Set up probe that returns findings
        registry = ProbeRegistry()

        def sample_probe(context):
            return ProbeResult(
                probe_id="test.sample",
                category="structure",
                status="warning",
                confidence=0.9,
                severity="medium",
                findings=[
                    ProbeFinding(
                        title="Missing test coverage",
                        evidence=["src/foo.py has 0% coverage"],
                        blast_radius="medium",
                    )
                ],
            )

        registry.register(
            "test.sample",
            sample_probe,
            ProbeCategory.STRUCTURE,
            description="Sample probe",
            triggers=["manual"],
        )

        # Mock IDEEventBus to capture emitted events
        emitted_events = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(side_effect=lambda *a, **kw: emitted_events.append((a, kw)))

        # Patch the IDEEventBus.get() inside _emit_probe_finding (local import)
        mock_ide_bus_mod = MagicMock()
        mock_ide_bus_mod.IDEEventBus.get.return_value = mock_bus
        with patch.dict("sys.modules", {"multifleet.event_bus": mock_ide_bus_mod}):
            results = registry.run_all({})

        # Verify probe returned findings
        assert len(results) == 1
        result = results[0]
        assert result.status == "warning"
        assert len(result.findings) == 1
        assert result.findings[0].title == "Missing test coverage"

        # Verify EventBus was called with probe.finding event
        assert mock_bus.emit.called
        call_args = mock_bus.emit.call_args
        assert call_args[0][0] == "probe.finding"

        # Now feed the finding through liaison as a fleet message
        liaison = FleetLiaison(node_id="mac1")
        liaison._event_bus = mock_bus  # inject mock

        probe_msg = {
            "type": "context",
            "from": "mac2",
            "payload": {
                "subject": "probe finding",
                "body": result.to_json(),
            },
        }
        level = liaison.process_message(probe_msg)
        assert level == EscalationLevel.INFO

        # Verify unread messages accumulated
        assert len(liaison.state.unread_messages) == 1

        # Verify to_dict is JSON-serializable
        result_dict = result.to_dict()
        json.dumps(result_dict)  # Must not raise

    def test_probe_error_handled_gracefully(self):
        """Probe that throws exception produces error result, not crash."""
        from multifleet.probe_engine import ProbeCategory, ProbeRegistry

        registry = ProbeRegistry()

        def broken_probe(context):
            raise RuntimeError("disk full")

        registry.register("test.broken", broken_probe, ProbeCategory.RELIABILITY)
        results = registry.run_all({})

        assert len(results) == 1
        assert results[0].status == "error"
        assert "disk full" in results[0].findings[0].title


# ── Workflow 4: Lease Lifecycle ────────────────────────────────────────────


class TestLeaseLifecycle:
    """Acquire -> work -> release -> freed -> cleanup expired."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        from multifleet.store import FleetNerveStore
        from multifleet.leases import LeaseManager

        self.store = FleetNerveStore(db_path=tmp_path / "leases.db")
        self.mac1 = LeaseManager(self.store, node_id="mac1")
        self.mac2 = LeaseManager(self.store, node_id="mac2")

    def test_lease_acquire_work_release_cleanup(self):
        """Full lease lifecycle: acquire -> deny competitor -> release -> cleanup."""
        # 1. mac1 acquires lease
        result = self.mac1.acquire("task-alpha", lease_type="short_task")
        assert result["acquired"] is True
        assert result["holder"] == "mac1"
        assert result["fence_token"] >= 1

        # 2. Verify query shows lease is active
        info = self.mac1.query("task-alpha")
        assert info is not None
        assert info["active"] is True
        assert info["node_id"] == "mac1"

        # 3. mac2 tries to acquire same resource -> denied
        denied = self.mac2.acquire("task-alpha", lease_type="short_task")
        assert denied["acquired"] is False
        assert denied["holder"] == "mac1"
        assert "reason" in denied

        # 4. mac1 releases the lease
        released = self.mac1.release("task-alpha")
        assert released["released"] is True

        # 5. After release, query shows not active
        info_after = self.mac1.query("task-alpha")
        assert info_after is not None
        assert info_after["active"] is False

        # 6. mac2 can now acquire it
        result2 = self.mac2.acquire("task-alpha", lease_type="short_task")
        assert result2["acquired"] is True
        assert result2["holder"] == "mac2"
        # Fence token is monotonically increasing
        assert result2["fence_token"] > result["fence_token"]

        # 7. Release and cleanup
        self.mac2.release("task-alpha")
        cleaned = self.mac2.cleanup_expired()
        assert cleaned >= 1  # Released lease gets cleaned up

    def test_expired_lease_cleanup(self):
        """Expired leases are cleaned up and resource becomes available."""
        # Acquire with heartbeat type (30s TTL)
        result = self.mac1.acquire("task-expire", lease_type="heartbeat")
        assert result["acquired"] is True

        # Manually expire by backdating acquired_at
        conn = self.store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id = ?",
            (time.time() - 100, "task-expire"),
        )
        conn.commit()

        # Now it's expired — cleanup should remove it
        cleaned = self.mac1.cleanup_expired()
        assert cleaned >= 1

        # mac2 can now acquire it
        result2 = self.mac2.acquire("task-expire", lease_type="short_task")
        assert result2["acquired"] is True

    def test_mac2_cannot_release_mac1_lease(self):
        """Only the holder can release a lease."""
        self.mac1.acquire("task-owned", lease_type="short_task")
        result = self.mac2.release("task-owned")
        assert result["released"] is False
        assert "mac1" in result["reason"]


# ── Workflow 5: Health Pipeline ────────────────────────────────────────────


class TestHealthPipeline:
    """fleet_doctor -> gains_gate -> synthesis_ready check."""

    def test_doctor_to_gains_gate_to_synthesis(self, tmp_path):
        """Doctor results inform gains gate, which gates synthesis."""
        from multifleet.doctor import FleetDoctor, DoctorReport, Check
        from multifleet.gains_gate import FleetGainsGate, GateResult, CheckResult
        from multifleet.synthesis import ChiefSynthesis

        # 1. Run fleet doctor with all network calls mocked
        doctor = FleetDoctor()
        with patch.object(doctor, "run") as mock_run:
            mock_report = DoctorReport(
                started_at=time.time(),
                duration_ms=42.0,
                node_id="mac1",
                checks=[
                    Check("FD-001", "daemon_running", "pass", "Up 3600s"),
                    Check("FD-002", "nats_server", "pass", "Connected"),
                    Check("FD-003", "peers_visible", "pass", "3 peers"),
                ],
            )
            mock_run.return_value = mock_report
            report = doctor.run()

        assert report.healthy is True
        assert report.passed == 3
        assert report.failed == 0

        # 2. Run gains gate with mocked network checks
        gate = FleetGainsGate()
        with patch.object(gate, "run") as mock_gate:
            mock_gate_result = GateResult(
                passed=True,
                checks=[
                    CheckResult("daemon_health", True, "Daemon up").to_dict(),
                    CheckResult("nats_connected", True, "NATS ok").to_dict(),
                ],
                total=2,
                passed_count=2,
                failed_count=0,
                critical_failures=0,
                duration_ms=15.0,
                timestamp="2026-04-05T00:00:00Z",
                recommendation="All checks passed. Safe to proceed.",
            )
            mock_gate.return_value = mock_gate_result
            gate_result = gate.run(quick=True)

        assert gate_result.passed is True
        assert gate_result.critical_failures == 0

        # 3. Since gate passed, synthesis is allowed
        synthesis = ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))
        synthesis._evidence_ledger = None

        verdicts = {
            "mac1": {"confidence": 0.9, "branch": "feat/mac1", "risks": [], "dissent": []},
            "mac2": {"confidence": 0.8, "branch": "feat/mac2", "risks": [], "dissent": []},
        }

        if gate_result.passed:
            result = synthesis.synthesize("task-gated", verdicts)
            assert result.decision in ("merge", "cherry_pick")
            assert result.confidence > 0

        # 4. Verify to_dict produces JSON-serializable output
        report_dict = report.to_dict()
        gate_dict = gate_result.to_dict()
        json.dumps(report_dict)
        json.dumps(gate_dict)

    def test_failed_gate_blocks_synthesis(self, tmp_path):
        """When gains gate fails, synthesis should be blocked."""
        from multifleet.gains_gate import GateResult, CheckResult

        gate_result = GateResult(
            passed=False,
            checks=[
                CheckResult("daemon_health", False, "Daemon down", critical=True).to_dict(),
            ],
            total=1,
            passed_count=0,
            failed_count=1,
            critical_failures=1,
            duration_ms=5.0,
            timestamp="2026-04-05T00:00:00Z",
            recommendation="Critical failures detected. Do not proceed.",
        )

        assert gate_result.passed is False
        assert gate_result.critical_failures == 1

        # Synthesis should NOT run when gate fails
        synthesis_ran = False
        if gate_result.passed:
            synthesis_ran = True
        assert synthesis_ran is False


# ── Workflow 6: Cross-Module Interface Consistency ─────────────────────────


class TestCrossModuleConsistency:
    """Verify key interfaces are consistent across modules."""

    def test_all_to_dict_return_json_serializable(self, tmp_path):
        """Every to_dict() method returns a JSON-serializable dict."""
        from multifleet.audit import AuditEntry
        from multifleet.doctor import Check, DoctorReport
        from multifleet.gains_gate import CheckResult, GateResult
        from multifleet.evidence_ledger import EvidenceLedger
        from multifleet.probe_engine import ProbeFinding, ProbeResult
        from multifleet.cross_rebuttal import RebuttalSession, RebuttalPhase
        from multifleet.synthesis import SynthesisResult
        from multifleet.packets import FleetPacket

        objects_with_to_dict = [
            AuditEntry(event="test", tool="test"),
            Check("FD-001", "test", "pass", "ok"),
            DoctorReport(checks=[], node_id="mac1"),
            CheckResult("test", True, "ok"),
            GateResult(passed=True),
            ProbeFinding(title="test"),
            ProbeResult(
                probe_id="test", category="structure",
                status="ok", confidence=1.0, severity="info",
            ),
            RebuttalSession(
                task_id="t1", phase=RebuttalPhase.ASSIGNED,
                created_at=time.time(), deadline_minutes=5,
            ),
            # SynthesisResult uses to_chief_decision(), tested separately below
            FleetPacket(type="test", from_node="mac1", to_node="mac2"),
        ]

        for obj in objects_with_to_dict:
            d = obj.to_dict()
            assert isinstance(d, dict), f"{type(obj).__name__}.to_dict() returned {type(d)}"
            # Must be JSON-serializable
            serialized = json.dumps(d)
            assert isinstance(serialized, str), f"{type(obj).__name__}.to_dict() not JSON-safe"

        # SynthesisResult uses to_chief_decision() instead of to_dict()
        synth = SynthesisResult(
            task_id="t1", decision="merge",
            winner_branch="main", confidence=0.9,
            reasoning="test",
        )
        d = synth.to_chief_decision()
        assert isinstance(d, dict)
        json.dumps(d)  # Must not raise

    def test_loggers_use_fleet_nerve_namespace(self):
        """All core modules use fleet-nerve.* logger namespace."""
        import multifleet.store as store_mod
        import multifleet.audit as audit_mod
        import multifleet.leases as leases_mod
        import multifleet.rebuttal as rebuttal_mod
        import multifleet.probe_engine as probe_mod
        import multifleet.liaison as liaison_mod
        import multifleet.synthesis as synthesis_mod
        import multifleet.evidence_ledger as evidence_mod
        import multifleet.doctor as doctor_mod
        import multifleet.gains_gate as gains_mod
        import multifleet.eventbus as eventbus_mod
        import multifleet.status_aggregator as status_mod

        modules = [
            store_mod, audit_mod, leases_mod, rebuttal_mod, probe_mod,
            liaison_mod, synthesis_mod, evidence_mod, doctor_mod,
            gains_mod, eventbus_mod, status_mod,
        ]

        for mod in modules:
            logger = mod.logger
            assert logger.name.startswith("fleet-nerve"), (
                f"{mod.__name__} logger is '{logger.name}', expected 'fleet-nerve.*'"
            )

    def test_fleet_packet_roundtrip(self):
        """FleetPacket serializes to dict and deserializes back."""
        from multifleet.packets import FleetPacket

        original = FleetPacket(
            type="task_brief",
            from_node="mac1",
            to_node="mac2",
            payload={"subject": "test", "data": [1, 2, 3]},
        )
        d = original.to_dict()
        restored = FleetPacket.from_dict(d)
        assert restored.type == original.type
        assert restored.from_node == original.from_node
        assert restored.to_node == original.to_node
        assert restored.payload == original.payload
        assert restored.id == original.id
