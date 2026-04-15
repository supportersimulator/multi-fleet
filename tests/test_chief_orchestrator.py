"""Tests for chief_orchestrator, workspace_roles, and surgeon_lan_relay."""

import time
import pytest

from multifleet.chief_orchestrator import (
    ChiefOrchestrator,
    SurgicalPhase,
    LocalVerdict,
    CrossCritique,
    ChiefDecision,
    assign_approaches,
    build_critique_pairs,
)
from multifleet.workspace_roles import (
    WorkspaceRegistry,
    WorkspaceRole,
    ApproachStrategy,
    detect_workspace_ide,
)
from multifleet.surgeon_lan_relay import (
    SurgeonLANRelay,
    SurgeonPacket,
    build_task_brief,
    build_local_verdict,
    build_critique,
    build_chief_decision,
    build_branch_status,
    build_review_request,
    VALID_PACKET_TYPES,
    PROTOCOL_VERSION,
)


# ── ChiefOrchestrator tests ────────────────────────────────────────────────


class TestAssignApproaches:
    def test_default_three_machines(self):
        result = assign_approaches(["mac1", "mac2", "mac3"])
        assert result == {
            "mac1": "straightforward",
            "mac2": "alternative",
            "mac3": "hardening",
        }

    def test_with_hints(self):
        result = assign_approaches(
            ["mac1", "mac2"],
            hints={"mac1": "minimal", "mac2": "exploratory"},
        )
        assert result == {"mac1": "minimal", "mac2": "exploratory"}

    def test_partial_hints(self):
        result = assign_approaches(
            ["mac1", "mac2", "mac3"],
            hints={"mac2": "custom-approach"},
        )
        assert result["mac1"] == "straightforward"
        assert result["mac2"] == "custom-approach"
        assert result["mac3"] == "hardening"


class TestCritiquePairs:
    def test_three_machines(self):
        pairs = build_critique_pairs(["mac1", "mac2", "mac3"])
        assert pairs == [("mac1", "mac2"), ("mac2", "mac3"), ("mac3", "mac1")]

    def test_two_machines(self):
        pairs = build_critique_pairs(["mac1", "mac2"])
        assert pairs == [("mac1", "mac2"), ("mac2", "mac1")]

    def test_one_machine(self):
        pairs = build_critique_pairs(["mac1"])
        assert pairs == []

    def test_empty(self):
        pairs = build_critique_pairs([])
        assert pairs == []


class TestChiefOrchestrator:
    def test_phase_assign(self):
        chief = ChiefOrchestrator(node_id="mac1")
        result = chief.phase_assign("t1", "Fix auth", ["mac1", "mac2", "mac3"])
        assert result.phase == SurgicalPhase.ASSIGN
        assert len(result.assignments) == 3
        assert result.task_id == "t1"

    def test_receive_verdicts(self):
        chief = ChiefOrchestrator(node_id="mac1")
        chief.phase_assign("t1", "Fix auth", ["mac1", "mac2"])

        assert not chief.receive_verdict("t1", {
            "machine_id": "mac1", "summary": "fix1",
            "branch": "mac1/fix", "confidence": 0.8,
        })
        assert chief.receive_verdict("t1", {
            "machine_id": "mac2", "summary": "fix2",
            "branch": "mac2/fix", "confidence": 0.75,
        })

    def test_phase_review(self):
        chief = ChiefOrchestrator(node_id="mac1")
        chief.phase_assign("t1", "Fix auth", ["mac1", "mac2", "mac3"])
        for m in ["mac1", "mac2", "mac3"]:
            chief.receive_verdict("t1", {
                "machine_id": m, "summary": f"{m} fix",
                "branch": f"{m}/fix", "confidence": 0.8,
            })
        pairs = chief.phase_review("t1")
        assert len(pairs) == 3

    def test_phase_close(self):
        chief = ChiefOrchestrator(node_id="mac1")
        chief.phase_assign("t1", "Fix auth", ["mac1", "mac2"])
        chief.receive_verdict("t1", {
            "machine_id": "mac1", "summary": "direct",
            "branch": "mac1/fix", "confidence": 0.9,
        })
        chief.receive_verdict("t1", {
            "machine_id": "mac2", "summary": "alt",
            "branch": "mac2/fix", "confidence": 0.7,
        })
        decision = chief.phase_close("t1")
        assert decision.winner_branch == "mac1/fix"
        assert decision.confidence >= 0.7

    def test_unknown_task_verdict(self):
        chief = ChiefOrchestrator(node_id="mac1")
        assert not chief.receive_verdict("nonexistent", {"machine_id": "mac1"})

    def test_clear_completed(self):
        chief = ChiefOrchestrator(node_id="mac1")
        chief.phase_assign("t1", "Fix", ["mac1"])
        chief.receive_verdict("t1", {
            "machine_id": "mac1", "summary": "done",
            "branch": "mac1/fix", "confidence": 0.9,
        })
        chief.phase_close("t1")
        assert chief.clear_completed() == 1
        assert chief.get_protocol("t1") is None


class TestLocalVerdict:
    def test_from_dict(self):
        v = LocalVerdict.from_dict({
            "machine_id": "mac1",
            "summary": "Fixed it",
            "branch": "mac1/fix",
            "confidence": 0.85,
            "risks": ["edge case"],
            "dissent": [{"surgeon": "neurologist", "claim": "risky"}],
        })
        assert v.machine_id == "mac1"
        assert v.confidence == 0.85
        assert len(v.dissent) == 1

    def test_to_packet(self):
        v = LocalVerdict(
            machine_id="mac2",
            summary="Alt approach",
            branch="mac2/fix",
            confidence=0.78,
        )
        pkt = v.to_packet()
        assert pkt["type"] == "local_verdict"
        assert pkt["from_node"] == "mac2"


# ── WorkspaceRoles tests ───────────────────────────────────────────────────


class TestWorkspaceRegistry:
    def test_assign(self):
        reg = WorkspaceRegistry()
        role = reg.assign("mac1", "straightforward", task_id="t1")
        assert role.approach == ApproachStrategy.STRAIGHTFORWARD
        assert role.task_id == "t1"

    def test_assign_custom(self):
        reg = WorkspaceRegistry()
        role = reg.assign("mac1", "my-custom-thing", task_id="t1")
        assert role.approach == ApproachStrategy.CUSTOM
        assert role.approach_label == "my-custom-thing"

    def test_assign_fleet(self):
        reg = WorkspaceRegistry()
        roles = reg.assign_fleet("t1", ["mac1", "mac2", "mac3"])
        assert len(roles) == 3
        approaches = {r.approach_label for r in roles.values()}
        assert "straightforward" in approaches
        assert "alternative" in approaches
        assert "hardening" in approaches

    def test_get_fleet_status(self):
        reg = WorkspaceRegistry()
        reg.assign_fleet("t1", ["mac1", "mac2"])
        status = reg.get_fleet_status()
        assert status["active_machines"] == 2
        assert status["approach_diversity"] == 2

    def test_clear_task(self):
        reg = WorkspaceRegistry()
        reg.assign_fleet("t1", ["mac1", "mac2"])
        cleared = reg.clear_task("t1")
        assert cleared == 2
        assert reg.get_role("mac1") is None

    def test_persistence(self, tmp_path):
        path = tmp_path / "roles.json"
        reg1 = WorkspaceRegistry(persist_path=path)
        reg1.assign("mac1", "hardening", task_id="t1")

        reg2 = WorkspaceRegistry(persist_path=path)
        role = reg2.get_role("mac1")
        assert role is not None
        assert role.approach == ApproachStrategy.HARDENING


# ── SurgeonLANRelay tests ──────────────────────────────────────────────────


class TestSurgeonPacket:
    def test_roundtrip(self):
        pkt = build_local_verdict("mac1", "t1", "summary", "mac1/fix", 0.85)
        data = pkt.to_dict()
        pkt2 = SurgeonPacket.from_dict(data)
        assert pkt2.type == "local_verdict"
        assert pkt2.from_node == "mac1"

    def test_validate_valid(self):
        pkt = build_branch_status("mac1", "main", "summary", ["file.py"])
        assert pkt.validate() == []

    def test_validate_missing_type(self):
        pkt = SurgeonPacket(from_node="mac1")
        errors = pkt.validate()
        assert any("missing type" in e for e in errors)

    def test_validate_unknown_type(self):
        pkt = SurgeonPacket(type="unknown_weird_type", from_node="mac1")
        errors = pkt.validate()
        assert any("unknown type" in e for e in errors)

    def test_stale(self):
        pkt = SurgeonPacket(type="heartbeat", from_node="mac1",
                           timestamp=time.time() - 7200)
        assert pkt.is_stale

    def test_not_stale(self):
        pkt = SurgeonPacket(type="heartbeat", from_node="mac1")
        assert not pkt.is_stale

    def test_all_builders(self):
        """All packet builders produce valid packets."""
        packets = [
            build_task_brief("chief", "mac1", "t1", "task", "straight", ["mac1"]),
            build_branch_status("mac1", "main", "summary", ["f.py"]),
            build_local_verdict("mac1", "t1", "summary", "b", 0.8),
            build_review_request("chief", "mac2", "t1", "mac1", "mac1/fix"),
            build_critique("mac2", "mac1", "t1", "looks good", 0.7),
            build_chief_decision("chief", "t1", "merge", "mac1/fix", 0.9, "best"),
        ]
        for pkt in packets:
            assert pkt.validate() == [], f"Invalid: {pkt.type} -> {pkt.validate()}"


class TestSurgeonLANRelay:
    def test_receive_and_dedup(self):
        relay = SurgeonLANRelay(node_id="mac2")
        pkt = build_local_verdict("mac1", "t1", "summary", "b", 0.8)
        result = relay.receive(pkt.to_dict())
        assert result is not None
        result2 = relay.receive(pkt.to_dict())
        assert result2 is None
        assert relay.get_stats()["deduped"] == 1

    def test_stale_rejection(self):
        relay = SurgeonLANRelay(node_id="mac2")
        pkt = build_local_verdict("mac1", "t1", "old", "b", 0.8)
        pkt.timestamp = time.time() - 7200  # 2 hours old
        result = relay.receive(pkt.to_dict())
        assert result is None
        assert relay.get_stats()["stale_dropped"] == 1

    def test_handler_registration(self):
        relay = SurgeonLANRelay(node_id="mac2")
        received = []
        relay.on("local_verdict", lambda p: received.append(p))

        pkt = build_local_verdict("mac1", "t1", "summary", "b", 0.8)
        relay.receive(pkt.to_dict())
        assert len(received) == 1
        assert received[0].type == "local_verdict"

    def test_p7_fallback(self, tmp_path):
        relay = SurgeonLANRelay(
            node_id="mac2",
            fleet_messages_root=tmp_path,
        )
        pkt = build_branch_status("mac2", "main", "status", ["f.py"])
        result = relay._try_p7(pkt)
        assert result
        files = list(tmp_path.glob("all/*.json"))
        assert len(files) == 1

    def test_stats(self):
        relay = SurgeonLANRelay(node_id="mac2")
        stats = relay.get_stats()
        assert stats["sent"] == 0
        assert stats["received"] == 0


class TestProtocolVersion:
    def test_version(self):
        assert PROTOCOL_VERSION == "3s-lan/v1"

    def test_valid_types(self):
        assert "task_brief" in VALID_PACKET_TYPES
        assert "chief_decision" in VALID_PACKET_TYPES
        assert len(VALID_PACKET_TYPES) >= 8
