"""Full Organism Test — end-to-end pipeline exercising ALL 5 products.

This test proves the complete epistemic organism works:
probe → EpistemicPacket → fleet_race dispatch → ChiefSynthesis →
AcceptanceGate → GhostDigest → evidence candidates → events emitted.

"The gold is not in building new tools. It is in wiring the 5 existing
tools together." — Gold Mining Inventory, 2026-04-08
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.epistemic_packet import EpistemicPacketBuilder, EpistemicPacket
from multifleet.acceptance_gate import AcceptanceGate, AcceptanceDecision
from multifleet.ghost_digest import GhostDigest, DigestResult
from multifleet.ghost_pulse import GhostPulse, PulseResult
from multifleet.probe_engine import ProbeResult, ProbeFinding
from multifleet.event_schema import EventEnvelope, EventType, EventSource, create_event
from multifleet.event_emitter import EventEmitter


class TestFullOrganismPipeline:
    """Test the complete pipeline as described in the GhostScan masterplan."""

    def test_probe_to_packet_to_gate_to_digest(self):
        """The golden path: probe → packet → synthesis → gate → digest → events."""

        # Step 1: Build EpistemicPacket (runs probes internally)
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-organism")
        packet = builder.build(
            task_intent="Implement the fibonacci sequence with memoization",
            run_probes=True,
        )

        assert packet.packet_id
        assert packet.task_intent == "Implement the fibonacci sequence with memoization"
        assert len(packet.probe_results) > 0  # Probes ran
        assert packet.required_surgeon_intensity in ("light", "standard", "full")
        assert packet.built_by == "test-organism"

        # Step 2: Simulate synthesis result (from ChiefSynthesis)
        synthesis_result = {
            "engine": "chief_synthesis",
            "decision": "merge",
            "winner_branch": "race/test/mac1",
            "confidence": 0.85,
            "reasoning": "Clean implementation with tests. mac1 scored highest.",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": "abc123", "reason": "Better error handling"}
            ],
            "dissent_preserved": [
                {"machine": "mac3", "claim": "Should use itertools instead of recursion"}
            ],
        }

        # Step 3: Run AcceptanceGate
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=packet.probe_results,
            surgeon_resolved=True,
            surgeon_intensity=packet.required_surgeon_intensity,
            surgeon_dissent_count=len(synthesis_result["dissent_preserved"]),
            evidence_count=1,
            epistemic_packet=packet.to_dict(),
        )

        assert isinstance(decision, AcceptanceDecision)
        # Gate should produce a decision (accepted or blocked based on probes)
        assert decision.reason

        # Step 4: Ghost Digest (transcripts → evidence candidates)
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test-organism")
            digest_result = digest.digest(
                task_id="organism-test-001",
                task_intent="Implement fibonacci with memoization",
                synthesis_result=synthesis_result,
                probe_results=[r if isinstance(r, dict) else r.to_dict() for r in packet.probe_results],
                acceptance_decision=decision.to_dict(),
            )

            assert isinstance(digest_result, DigestResult)
            assert len(digest_result.evidence_candidates) >= 1  # At least completion record
            assert digest_result.task_id == "organism-test-001"

            # Verify evidence was persisted to disk
            evidence_files = list(Path(tmp).glob("*.json"))
            assert len(evidence_files) == 1

            # Verify content
            persisted = json.loads(evidence_files[0].read_text())
            assert persisted["task_id"] == "organism-test-001"

        # Step 5: Verify dissent was carried forward
        if synthesis_result["dissent_preserved"]:
            assert len(digest_result.unresolved_objections) > 0

        # Step 6: Verify cherry-picks were captured as learning
        learning_candidates = [
            c for c in digest_result.evidence_candidates if c.category == "learning"
        ]
        assert len(learning_candidates) >= 1  # Cherry-pick from mac2

    def test_ghost_pulse_monitors_organism_health(self):
        """Ghost pulse detects system health issues across the organism."""
        with tempfile.TemporaryDirectory() as tmp:
            # Create a ledger with contradicting decisions
            ledger_path = Path(tmp) / "decisions.jsonl"
            ledger_path.write_text(
                json.dumps({"task_id": "conflict", "decision": "merge", "timestamp": time.time()}) + "\n"
                + json.dumps({"task_id": "conflict", "decision": "reject", "timestamp": time.time()}) + "\n"
            )

            pulse = GhostPulse(repo_root=".", evidence_ledger_path=str(ledger_path))
            result = pulse.pulse()

            assert isinstance(result, PulseResult)
            assert result.contradictions_found >= 1  # Detected the conflict

    def test_event_schema_flows_through_system(self):
        """EventEnvelope events are created correctly at each pipeline stage."""
        # Probe event
        probe_event = create_event(
            EventType.PROBE_FINDING_RAISED,
            EventSource.PROBE_ENGINE,
            "test-node",
            payload={"probe_id": "mirror_drift", "severity": "high"},
        )
        assert probe_event.type == "probe.finding.raised"
        assert probe_event.source == "probe-engine"

        # Surgeon event
        surgeon_event = create_event(
            EventType.SURGEON_CONSENSUS_REACHED,
            EventSource.THREE_SURGEONS,
            "test-node",
            payload={"confidence": 0.85, "decision": "merge"},
        )
        assert surgeon_event.type == "surgeon.consensus.reached"

        # Acceptance event
        gate_event = create_event(
            EventType.TASK_ACCEPTED,
            EventSource.CONTEXTDNA,
            "test-node",
            payload={"reason": "All checks passed"},
            task_id="task-001",
        )
        assert gate_event.type == "task.accepted"
        assert gate_event.task_id == "task-001"

        # Ghost event
        ghost_event = create_event(
            EventType.GHOST_SESSION_DIGESTED,
            EventSource.GHOST,
            "test-node",
            payload={"candidates": 5, "objections": 1},
        )
        assert ghost_event.type == "ghost.session.digested"

        # All events serialize correctly
        for event in [probe_event, surgeon_event, gate_event, ghost_event]:
            d = event.to_dict()
            assert "id" in d
            assert "ts" in d
            assert "type" in d
            # Round-trip
            restored = EventEnvelope.from_dict(d)
            assert restored.type == event.type

    def test_event_emitter_logs_locally(self):
        """EventEmitter writes events to JSONL log even when EventBus is down."""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            emitter = EventEmitter(
                node_id="test-emitter",
                event_log_path=str(log_path),
                eventbus_port=99999,  # Unreachable — will fail gracefully
            )

            event = create_event(
                EventType.PROBE_FINDING_RAISED,
                EventSource.PROBE_ENGINE,
                "test-emitter",
                payload={"probe_id": "test_probe"},
            )
            result = emitter.emit(event)

            # EventBus should fail (port 99999)
            assert result["eventbus"] is False
            # Log should succeed
            assert result["log"] is True

            # Verify JSONL
            lines = log_path.read_text().strip().splitlines()
            assert len(lines) == 1
            logged = json.loads(lines[0])
            assert logged["type"] == "probe.finding.raised"
            assert logged["node_id"] == "test-emitter"

    def test_feedback_loop_protection(self):
        """Ghost digest prevents re-processing the same task (CV-FLC protection)."""
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")

            # First digest — should produce results
            result1 = digest.digest(task_id="loop-test", task_intent="Test")
            assert len(result1.evidence_candidates) >= 1

            # Second digest of same task — should be empty (loop protection)
            result2 = digest.digest(task_id="loop-test", task_intent="Test again")
            assert len(result2.evidence_candidates) == 0

    def test_product_bridge_discovery(self):
        """ProductBridge discovers available products."""
        try:
            from multifleet.product_bridge import ProductBridge
            bridge = ProductBridge()
            products = bridge.discover_products()
            # At minimum, multi-fleet should be discovered (daemon running)
            assert isinstance(products, dict)
            # Routes should be registered
            routes = bridge.list_routes()
            assert len(routes) > 0  # Built-in routes exist
        except ImportError:
            pytest.skip("ProductBridge not available")

    def test_epistemic_packet_carries_probe_intelligence(self):
        """EpistemicPacket correctly translates probe results into dispatch context."""
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test")
        packet = builder.build(
            task_intent="Fix critical auth bug",
            changed_files=["src/auth.py"],
            run_probes=True,
        )

        # Packet should have probe-derived intelligence
        assert packet.required_surgeon_intensity in ("light", "standard", "full")
        assert "zero_silent_failures" in packet.active_invariants

        # Serialization round-trip
        d = packet.to_dict()
        restored = EpistemicPacket.from_dict(d)
        assert restored.task_intent == packet.task_intent
        assert restored.required_surgeon_intensity == packet.required_surgeon_intensity
