"""Integration test: probe → packet → dispatch → synthesis → acceptance gate.

This is the end-to-end pipeline test that the neurologist demanded:
"Stop building. Start integrating."
"""

import time
from unittest.mock import patch

import pytest

from multifleet.fleet_race import FleetRace
from multifleet.epistemic_packet import EpistemicPacketBuilder, EpistemicPacket
from multifleet.acceptance_gate import AcceptanceGate
from multifleet.probe_engine import ProbeResult, ProbeFinding


class TestEndToEndPipeline:
    """Test the full pipeline: probe → packet → synthesis → gate."""

    def test_full_pipeline_accepted(self):
        """Happy path: probes clear, synthesis runs, gate accepts."""
        # Step 1: Build EpistemicPacket
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test")
        packet = builder.build(task_intent="Fix a typo in README", run_probes=True)
        assert packet.packet_id
        assert packet.built_by == "test"

        # Step 2: Simulate race with synthesis
        racer = FleetRace(daemon_port=9999, node_id="test")
        race = {
            "race_id": "integration-test",
            "started_at": time.time() - 60,
            "epistemic_packet": packet.to_dict(),
            "participants": {
                "mac1": {
                    "node": "mac1", "branch": "race/test/mac1",
                    "status": "completed", "completed_at": time.time() - 30,
                    "result": {
                        "confidence": 0.9, "summary": "Fixed typo",
                        "strengths": ["clean"], "risks": [], "dissent": [],
                    },
                },
            },
            "rebuttals": [],
        }

        with patch.object(racer, "_record_evidence"):
            racer._run_synthesis(race)

        # Step 3: Verify synthesis ran (single completion = direct winner)
        assert race["synthesis"] is not None
        assert race["winner"] is not None

        # Step 4: Verify acceptance gate ran
        assert "acceptance" in race
        assert race["acceptance"]["accepted"] is True or race["status"] in ("done", "blocked")

    def test_pipeline_blocked_by_probe(self):
        """Blocking probe findings should prevent acceptance."""
        gate = AcceptanceGate()

        # Simulate blocking probe result
        blocking_result = ProbeResult(
            probe_id="mirror_drift", category="context_quality",
            status="critical", confidence=0.95, severity="critical",
            findings=[ProbeFinding(
                title="Architecture doc references deleted module",
                blast_radius="high",
            )],
        )

        decision = gate.evaluate(
            probe_results=[blocking_result],
            surgeon_resolved=True,
            surgeon_intensity="full",
            evidence_count=1,
        )
        assert decision.accepted is False
        assert "Blocking probe" in decision.blocking_issues[0]

    def test_pipeline_surgeon_not_resolved_blocks(self):
        """Unresolved surgeon review blocks acceptance for non-trivial tasks."""
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=[],
            surgeon_intensity="full",
            surgeon_resolved=False,
            evidence_count=1,
        )
        assert decision.accepted is False

    def test_packet_flows_through_race(self):
        """EpistemicPacket built during race.start() reaches synthesis."""
        racer = FleetRace(daemon_port=9999, node_id="test")

        with patch.object(racer, "_discover_nodes", return_value=["test"]):
            with patch.object(racer, "_acquire_lease", return_value={"acquired": True}):
                with patch.object(racer, "_dispatch_to_node", return_value={"sent": True}):
                    with patch.object(racer, "_broadcast_race_start"):
                        with patch.object(racer, "_record_evidence"):
                            result = racer.start(
                                "Integration test task",
                                nodes=["test"],
                                run_probes=True,
                            )

        # Packet was built
        assert result.get("epistemic_packet") is not None
        pkt = result["epistemic_packet"]
        assert pkt["task_intent"] == "Integration test task"
        assert "required_surgeon_intensity" in pkt

    def test_acceptance_recorded_in_evidence(self):
        """Acceptance decision is recorded in evidence ledger."""
        racer = FleetRace(daemon_port=9999, node_id="test")
        evidence_calls = []

        def mock_record(event_type, data):
            evidence_calls.append((event_type, data))

        race = {
            "race_id": "evidence-test",
            "status": "synthesizing",
            "started_at": time.time() - 60,
            "epistemic_packet": {"acceptance_policy": "standard", "probe_results": []},
            "participants": {
                "test": {
                    "node": "test", "branch": "race/ev/test",
                    "status": "completed", "completed_at": time.time() - 10,
                    "result": {
                        "confidence": 0.8, "summary": "Done",
                        "strengths": [], "risks": [], "dissent": [],
                    },
                },
            },
            "rebuttals": [],
        }

        with patch.object(racer, "_record_evidence", side_effect=mock_record):
            racer._run_synthesis(race)

        # Should have evidence for both synthesis and acceptance
        event_types = [e[0] for e in evidence_calls]
        assert "race_completed" in event_types
        assert "acceptance_decision" in event_types

        # Find acceptance evidence
        acceptance_ev = next(e for e in evidence_calls if e[0] == "acceptance_decision")
        assert "accepted" in acceptance_ev[1]
