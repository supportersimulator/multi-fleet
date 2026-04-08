"""Full stack integration tests.

End-to-end flows through multiple modules: probe -> evidence -> synthesis -> ledger,
fleet message -> audit -> liaison -> IDE, lease -> rebuttal -> synthesis, and
cross-cutting invariants (logger namespaces, JSON serializability).
"""

import json
import logging
import time
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.event_bus_server import EventBus
from multifleet.event_types import create_event, validate_namespace, NAMESPACES
from multifleet.eventbus import FleetEventBus
from multifleet.ide_bridge import IDEBridge
from multifleet.evidence_ledger import EvidenceLedger
from multifleet.probe_engine import ProbeCategory, ProbeSeverity
from multifleet.synthesis import SynthesisResult


# ═══════════════════════════════════════════════════════════════════
# Probe -> Evidence -> Synthesis -> Ledger -> Dashboard
# ═══════════════════════════════════════════════════════════════════


class TestProbeToLedgerPipeline:
    """Probe scan -> evidence -> synthesis -> decision -> ledger -> dashboard."""

    def test_probe_finding_flows_to_eventbus(self):
        """Probe finding emitted to EventBus reaches IDE bridge subscriber."""
        bus = EventBus(ring_size=100)
        bridge = IDEBridge()
        received_events = []

        def on_event(event):
            translated = bridge.translate_event(event["type"], event.get("payload", {}))
            received_events.append(translated)

        bus.on("fleet.probe.*", on_event)

        # Simulate probe engine emitting a finding
        bus.emit(
            "fleet.probe.finding",
            {
                "probe_id": "stale-docs-001",
                "title": "CLAUDE.md out of date",
                "severity": "warning",
                "category": "context_quality",
            },
            source="probe-engine",
        )

        assert len(received_events) == 1
        assert received_events[0]["command"] == "notification"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1

    def test_evidence_ledger_records_decision(self, tmp_path):
        """Synthesis result written to evidence ledger with hash chain."""
        ledger = EvidenceLedger(db_path=str(tmp_path / "evidence.db"))

        # Create a synthesis result
        result = SynthesisResult(
            task_id="task-001",
            decision="merge",
            winner_branch="mac2/feature-x",
            confidence=0.92,
            reasoning="All surgeons agree on approach",
            dissent_preserved=[],
        )

        # Record as ledger entry
        entry = ledger.record(
            event_type="merge_decision",
            node_id="mac1",
            subject=f"task:{result.task_id}",
            payload=result.to_chief_decision(),
        )

        assert entry is not None
        assert entry["entry_id"]

        # Verify it can be retrieved
        entries = ledger.query(event_type="merge_decision")
        assert len(entries) >= 1
        latest = entries[0]  # query returns newest first
        assert latest["subject"] == "task:task-001"
        payload = latest.get("payload", latest.get("data"))
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["decision"] == "merge"
        assert payload["confidence"] == 0.92

    def test_ledger_hash_chain_integrity(self, tmp_path):
        """Multiple ledger entries form a valid hash chain."""
        ledger = EvidenceLedger(db_path=str(tmp_path / "evidence.db"))

        for i in range(5):
            ledger.record(
                event_type="merge_decision",
                node_id="mac1",
                subject=f"task-{i}",
                payload={"decision": "merge", "seq": i},
            )

        result = ledger.verify_chain()
        assert result["valid"] is True, f"Hash chain broken at entry {result['broken_at']}"


# ═══════════════════════════════════════════════════════════════════
# Fleet Message -> Audit -> Liaison -> Injection -> IDE Event
# ═══════════════════════════════════════════════════════════════════


class TestMessageToIDEPipeline:
    """Fleet message -> audit -> liaison -> injection -> IDE event."""

    def test_message_audit_records_event(self, tmp_path):
        """Fleet message processing creates an audit trail entry."""
        from multifleet.audit import AuditTrail

        trail = AuditTrail(storage_dir=str(tmp_path), node_id="mac1")
        trail.record(
            event="message_received",
            tool="fleet_send",
            target="mac2",
            status="ok",
            latency_ms=42.5,
        )
        trail.flush()

        # Verify audit file exists and contains the entry
        audit_files = list(tmp_path.glob("*.jsonl"))
        assert len(audit_files) >= 1
        content = audit_files[0].read_text()
        assert "message_received" in content
        assert "mac2" in content

    def test_liaison_processes_message_and_escalates(self):
        """Liaison receives a message, classifies escalation, and queues for IDE."""
        from multifleet.liaison import LiaisonState, ESCALATION_MAP, EscalationLevel

        state = LiaisonState(node_id="mac1")

        # Simulate incoming task_brief (should be URGENT)
        msg = {
            "type": "task_brief",
            "from_node": "mac1",
            "subject": "Build feature X",
            "body": "Implement the new dashboard widget",
            "timestamp": time.time(),
        }

        escalation = ESCALATION_MAP.get(msg["type"], EscalationLevel.INFO)
        assert escalation == EscalationLevel.URGENT

        # Add to unread
        state.unread_messages.append(msg)
        assert len(state.unread_messages) == 1

    def test_liaison_message_becomes_ide_event(self):
        """Message processed by liaison emits IDE-consumable event via EventBus."""
        bus = EventBus(ring_size=100)
        bridge = IDEBridge()
        received = []

        bus.on("fleet.message.*", lambda e: received.append(e))

        # Simulate liaison emitting event after processing a message
        bus.emit(
            "fleet.message.delivered",
            {"from": "mac2", "type": "context", "subject": "Schema update"},
            source="liaison",
        )

        assert len(received) == 1
        translated = bridge.translate_event(
            received[0]["type"], received[0]["payload"]
        )
        assert translated["command"] == "notification"


# ═══════════════════════════════════════════════════════════════════
# Lease -> Rebuttal -> Synthesis -> Merge Recommendation
# ═══════════════════════════════════════════════════════════════════


class TestLeaseRebuttalSynthesis:
    """Lease -> rebuttal -> synthesis -> merge recommendation."""

    def test_rebuttal_lifecycle_to_synthesis(self):
        """Rebuttal session advances through phases to synthesis."""
        from multifleet.cross_rebuttal import RebuttalSession, RebuttalPhase

        session = RebuttalSession(
            task_id="task-rebuttal-001",
            phase=RebuttalPhase.ASSIGNED,
            created_at=time.time(),
            deadline_minutes=10,
            expected_machines=["mac1", "mac2", "mac3"],
        )

        # Walk through the full lifecycle
        assert session.advance(RebuttalPhase.LOCAL_SURGERY) is True
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

        assert session.advance(RebuttalPhase.VERDICTS_COLLECTED) is True
        session.verdicts["mac1"] = {"decision": "merge", "branch": "feature-x"}
        session.verdicts["mac2"] = {"decision": "cherry_pick", "branch": "feature-y"}

        assert session.advance(RebuttalPhase.UNDER_REVIEW) is True
        assert session.advance(RebuttalPhase.CRITIQUES_COLLECTED) is True
        session.critiques["mac1"] = {"agrees_with": "mac2", "notes": "good approach"}

        assert session.advance(RebuttalPhase.SYNTHESIS) is True

        # Create synthesis result
        result = SynthesisResult(
            task_id="task-rebuttal-001",
            decision="cherry_pick",
            winner_branch="mac2/feature-y",
            confidence=0.85,
            reasoning="mac2 approach preserves backward compat",
            dissent_preserved=[
                {"surgeon": "Atlas", "machine": "mac1", "claim": "Full merge preferred"}
            ],
        )

        decision = result.to_chief_decision()
        assert decision["decision"] == "cherry_pick"
        assert decision["dissent_count"] == 1
        assert len(decision["dissent_preserved"]) == 1

        # Final phase
        assert session.advance(RebuttalPhase.RESOLVED) is True

    def test_invalid_phase_transition_rejected(self):
        """Invalid rebuttal phase transition returns False."""
        from multifleet.cross_rebuttal import RebuttalSession, RebuttalPhase

        session = RebuttalSession(
            task_id="bad-transition",
            phase=RebuttalPhase.ASSIGNED,
            created_at=time.time(),
            deadline_minutes=5,
        )

        # Can't jump from ASSIGNED to SYNTHESIS
        assert session.advance(RebuttalPhase.SYNTHESIS) is False
        assert session.phase == RebuttalPhase.ASSIGNED  # unchanged


# ═══════════════════════════════════════════════════════════════════
# Cross-cutting invariants
# ═══════════════════════════════════════════════════════════════════


class TestCrossCuttingInvariants:
    """All modules use consistent patterns: logger namespaces, JSON serializability."""

    def test_all_modules_use_consistent_logger_namespace(self):
        """Key modules use fleet-nerve.* or multifleet.* logger namespace."""
        import multifleet.eventbus as eb
        import multifleet.ide_bridge as ib
        import multifleet.evidence_ledger as el
        import multifleet.liaison as li
        import multifleet.audit as au
        import multifleet.protocol as pr
        import multifleet.event_stream as es

        loggers = {
            "eventbus": eb.logger.name,
            "ide_bridge": ib.logger.name,
            "evidence_ledger": el.logger.name,
            "liaison": li.logger.name,
            "audit": au.logger.name,
            "event_stream": es.logger.name,
        }

        # All should use a dotted namespace (fleet-nerve.X or multifleet.X)
        for module_name, logger_name in loggers.items():
            assert "." in logger_name or logger_name.startswith("fleet"), (
                f"{module_name} logger name '{logger_name}' does not follow "
                f"fleet-nerve.* or multifleet.* convention"
            )

    def test_create_event_returns_json_serializable(self):
        """create_event output is fully JSON-serializable."""
        event = create_event(
            event_type="fleet.test.event",
            payload={"key": "value", "nested": {"a": 1}},
            source="test",
            correlation_id="corr-123",
            session_id="sess-456",
        )
        serialized = json.dumps(event)
        deserialized = json.loads(serialized)
        assert deserialized["type"] == "fleet.test.event"
        assert deserialized["payload"]["nested"]["a"] == 1

    def test_synthesis_result_json_serializable(self):
        """SynthesisResult.to_chief_decision() returns JSON-serializable dict."""
        result = SynthesisResult(
            task_id="task-json-test",
            decision="merge",
            winner_branch="main",
            confidence=0.95,
            reasoning="all agree",
        )
        decision = result.to_chief_decision()
        serialized = json.dumps(decision)
        assert '"merge"' in serialized

    def test_ide_bridge_translate_json_serializable(self):
        """IDEBridge.translate_event returns JSON-serializable dict."""
        bridge = IDEBridge()
        for event_type in [
            "fleet.peer.online",
            "fleet.probe.finding",
            "fleet.health.degraded",
            "fleet.rebuttal.proposed",
        ]:
            result = bridge.translate_event(event_type, {"test": True})
            serialized = json.dumps(result)
            assert isinstance(json.loads(serialized), dict)

    def test_evidence_ledger_entry_json_serializable(self, tmp_path):
        """EvidenceLedger entries are JSON-serializable when queried."""
        ledger = EvidenceLedger(db_path=str(tmp_path / "evidence.db"))
        ledger.record(
            event_type="test_event",
            node_id="mac1",
            subject="json-test",
            payload={"nested": {"list": [1, 2, 3]}},
        )

        entries = ledger.query(event_type="test_event")
        for entry in entries:
            # Each entry should be dict-like and its payload JSON-parseable
            payload = entry.get("payload", entry.get("data"))
            if isinstance(payload, str):
                parsed = json.loads(payload)
            else:
                parsed = payload
            json.dumps(parsed)  # should not raise

    def test_fleet_namespaces_all_registered(self):
        """The 'fleet' namespace is registered in event_types.NAMESPACES."""
        assert "fleet" in NAMESPACES
        assert validate_namespace("fleet.peer.online") is True
        assert validate_namespace("fleet.health.degraded") is True

    def test_validate_namespace_rejects_unknown(self):
        """Unknown namespaces are rejected by validate_namespace."""
        assert validate_namespace("bogus.event") is False
        assert validate_namespace("") is False
        assert validate_namespace("nodots") is False
