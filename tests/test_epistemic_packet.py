"""Tests for EpistemicPacket builder — evidence-bearing fleet dispatch."""

import json
from unittest.mock import patch, MagicMock

import pytest

from multifleet.epistemic_packet import EpistemicPacket, EpistemicPacketBuilder


class TestEpistemicPacket:
    """Test the packet dataclass."""

    def test_to_dict(self):
        pkt = EpistemicPacket(
            packet_id="test-123",
            task_intent="Fix auth",
            branch_name="fix/auth",
            changed_files=["src/auth.py"],
            required_surgeon_intensity="full",
            known_risks=["Token refresh broke 3 times"],
        )
        d = pkt.to_dict()
        assert d["packet_id"] == "test-123"
        assert d["task_intent"] == "Fix auth"
        assert d["required_surgeon_intensity"] == "full"
        assert "Token refresh" in d["known_risks"][0]

    def test_summary(self):
        pkt = EpistemicPacket(
            packet_id="abc12345",
            task_intent="Test",
            probe_results=[{"probe_id": "test"}],
            blocking_findings=[{"title": "blocker"}],
            known_risks=["risk1"],
        )
        s = pkt.summary()
        assert "abc12345" in s
        assert "1 probes" in s
        assert "1 blocking" in s

    def test_from_dict(self):
        d = {
            "packet_id": "xyz",
            "task_intent": "Build feature",
            "required_surgeon_intensity": "standard",
        }
        pkt = EpistemicPacket.from_dict(d)
        assert pkt.packet_id == "xyz"
        assert pkt.required_surgeon_intensity == "standard"

    def test_to_json(self):
        pkt = EpistemicPacket(packet_id="j", task_intent="test")
        j = pkt.to_json()
        parsed = json.loads(j)
        assert parsed["packet_id"] == "j"


class TestEpistemicPacketBuilder:
    """Test the packet builder."""

    def test_build_without_probes(self):
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-node")
        pkt = builder.build(task_intent="Quick test", run_probes=False)
        assert pkt.task_intent == "Quick test"
        assert pkt.built_by == "test-node"
        assert pkt.packet_id  # UUID generated
        assert pkt.required_surgeon_intensity == "light"

    def test_build_with_probes(self):
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-node")
        pkt = builder.build(task_intent="Fix auth token refresh")
        assert pkt.task_intent == "Fix auth token refresh"
        assert pkt.built_at  # timestamp set
        assert len(pkt.probe_results) > 0  # probes ran
        assert pkt.required_surgeon_intensity in ("light", "standard", "full")

    def test_build_sets_acceptance_policy(self):
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-node")
        pkt = builder.build(task_intent="Minor fix", run_probes=False)
        assert pkt.acceptance_policy == "permissive"  # no probes, no blocking

    def test_active_invariants_populated(self):
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-node")
        pkt = builder.build(task_intent="Test", run_probes=False)
        assert "zero_silent_failures" in pkt.active_invariants

    def test_round_trip(self):
        builder = EpistemicPacketBuilder(repo_root=".", node_id="test-node")
        pkt = builder.build(task_intent="Round trip test", run_probes=False)
        d = pkt.to_dict()
        pkt2 = EpistemicPacket.from_dict(d)
        assert pkt2.task_intent == pkt.task_intent
        assert pkt2.packet_id == pkt.packet_id


class TestFleetRaceIntegration:
    """Test that fleet_race uses EpistemicPacket."""

    def test_race_start_builds_packet(self):
        from multifleet.fleet_race import FleetRace

        racer = FleetRace(daemon_port=9999, node_id="test")
        with patch.object(racer, "_discover_nodes", return_value=["test"]):
            with patch.object(racer, "_acquire_lease", return_value={"acquired": True}):
                with patch.object(racer, "_dispatch_to_node", return_value={"sent": True}):
                    with patch.object(racer, "_broadcast_race_start"):
                        with patch.object(racer, "_record_evidence"):
                            result = racer.start("Test task", nodes=["test"], run_probes=True)

        assert "epistemic_packet" in result
        if result["epistemic_packet"]:
            assert result["epistemic_packet"]["task_intent"] == "Test task"
            assert "required_surgeon_intensity" in result["epistemic_packet"]

    def test_race_start_without_probes(self):
        from multifleet.fleet_race import FleetRace

        racer = FleetRace(daemon_port=9999, node_id="test")
        with patch.object(racer, "_discover_nodes", return_value=["test"]):
            with patch.object(racer, "_acquire_lease", return_value={"acquired": True}):
                with patch.object(racer, "_dispatch_to_node", return_value={"sent": True}):
                    with patch.object(racer, "_broadcast_race_start"):
                        with patch.object(racer, "_record_evidence"):
                            result = racer.start("Quick task", nodes=["test"], run_probes=False)

        assert result["epistemic_packet"] is None
