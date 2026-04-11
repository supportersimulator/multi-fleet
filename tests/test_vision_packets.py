"""Tests for the vision packet types — both JSON schema validation and Python dataclass round-trips.

Covers: BrainstormPacket, SurgeonDecisionPacket, VisionContract,
        ExecutionLedger, ChiefEscalationPacket, ChiefDecisionPacket.
Also validates that the 6 original packet schemas (task_brief, local_verdict,
branch_status, critique, merge_candidate, chief_decision) remain registered.
"""

import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.packet_registry import PacketRegistry
from multifleet.vision_packets import (
    BrainstormPacket,
    BrainstormOption,
    SurgeonDecisionPacket,
    SurgeonOpinion,
    VisionContract,
    ExecutionLedger,
    ExecutionStep,
    ChiefEscalationPacket,
    ChiefDecisionPacket,
    packet_from_dict,
)

# Skip schema validation tests if jsonschema not installed
jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture
def registry():
    return PacketRegistry()


# ── Helpers ───────────────────────────────────────────────────────────────


def assert_valid(registry, type_name, payload):
    errors = registry.validate(type_name, payload)
    assert errors == [], f"Unexpected validation errors for {type_name}: {errors}"


def assert_invalid(registry, type_name, payload):
    errors = registry.validate(type_name, payload)
    assert len(errors) > 0, f"Expected validation errors for {type_name} but got none"


# ── Schema Registration ──────────────────────────────────────────────────


class TestRegistration:
    def test_original_six_types_registered(self, registry):
        for t in ["task_brief", "local_verdict", "branch_status",
                   "critique", "merge_candidate", "chief_decision"]:
            assert registry.is_registered(t), f"{t} not registered"

    def test_vision_packet_types_registered(self, registry):
        for t in ["brainstorm", "surgeon_decision", "vision_contract",
                   "execution_ledger", "chief_escalation", "chief_decision_v2"]:
            assert registry.is_registered(t), f"{t} not registered"


# ── BrainstormPacket ─────────────────────────────────────────────────────


class TestBrainstormPacket:
    def test_create_and_serialize(self):
        bp = BrainstormPacket(task_id="t-1", node_id="mac1")
        bp.add_option("Option A", "Do it this way", pros=["fast"], cons=["risky"])
        bp.add_option("Option B", "Do it that way", confidence=0.8)
        bp.recommended = bp.options[1].id

        d = bp.to_dict()
        assert d["task_id"] == "t-1"
        assert len(d["options"]) == 2
        assert d["options"][0]["pros"] == ["fast"]
        assert d["recommended"] == bp.options[1].id

    def test_round_trip(self):
        bp = BrainstormPacket(task_id="t-2", node_id="mac2")
        bp.add_option("X", "desc-x")
        d = bp.to_dict()
        bp2 = BrainstormPacket.from_dict(d)
        assert bp2.task_id == "t-2"
        assert len(bp2.options) == 1
        assert bp2.options[0].title == "X"

    def test_get_option(self):
        bp = BrainstormPacket()
        opt = bp.add_option("A", "desc")
        assert bp.get_option(opt.id) is opt
        assert bp.get_option("nonexistent") is None

    def test_schema_valid(self, registry):
        bp = BrainstormPacket(task_id="t-1", node_id="mac1")
        bp.add_option("A", "desc")
        assert_valid(registry, "brainstorm", bp.to_dict())

    def test_schema_missing_task_id(self, registry):
        assert_invalid(registry, "brainstorm", {
            "id": "x", "node_id": "m", "options": [],
        })


# ── SurgeonDecisionPacket ────────────────────────────────────────────────


class TestSurgeonDecisionPacket:
    def test_consensus_unanimous(self):
        sdp = SurgeonDecisionPacket(brainstorm_id="b-1", task_id="t-1")
        sdp.add_opinion("atlas", "opt-a", 0.9, "solid approach")
        sdp.add_opinion("cardiologist", "opt-a", 0.85, "agree")
        sdp.add_opinion("neurologist", "opt-a", 0.7, "pattern match")
        assert sdp.check_consensus() is True
        assert sdp.selected_option == "opt-a"

    def test_no_consensus_split(self):
        sdp = SurgeonDecisionPacket(brainstorm_id="b-1", task_id="t-1")
        sdp.add_opinion("atlas", "opt-a", 0.9, "A is better")
        sdp.add_opinion("cardiologist", "opt-b", 0.8, "B is safer")
        assert sdp.check_consensus() is False

    def test_abstain_excluded(self):
        sdp = SurgeonDecisionPacket()
        sdp.add_opinion("atlas", "opt-a", 0.9, "yes")
        sdp.add_opinion("neurologist", "abstain", 0.3, "not sure")
        assert sdp.check_consensus() is True  # only non-abstain votes count
        assert sdp.selected_option == "opt-a"

    def test_round_trip(self):
        sdp = SurgeonDecisionPacket(task_id="t-1")
        sdp.add_opinion("atlas", "opt-a", 0.9, "good", objections=["minor risk"])
        d = sdp.to_dict()
        sdp2 = SurgeonDecisionPacket.from_dict(d)
        assert sdp2.opinions[0].objections == ["minor risk"]

    def test_schema_valid(self, registry):
        sdp = SurgeonDecisionPacket(brainstorm_id="b-1", task_id="t-1")
        sdp.add_opinion("atlas", "opt-a", 0.9, "solid")
        assert_valid(registry, "surgeon_decision", sdp.to_dict())


# ── VisionContract ───────────────────────────────────────────────────────


class TestVisionContract:
    def test_lock(self):
        vc = VisionContract(task_id="t-1", description="Build X",
                            done_criteria=["tests pass", "docs updated"])
        assert not vc.locked
        vc.lock("mac1")
        assert vc.locked
        assert vc.locked_by == "mac1"

    def test_double_lock_raises(self):
        vc = VisionContract()
        vc.lock("mac1")
        with pytest.raises(ValueError):
            vc.lock("mac2")

    def test_is_satisfied(self):
        vc = VisionContract(done_criteria=["a", "b", "c"])
        assert not vc.is_satisfied(["a", "b"])
        assert vc.is_satisfied(["a", "b", "c"])
        assert vc.is_satisfied(["a", "b", "c", "d"])  # superset ok

    def test_round_trip(self):
        vc = VisionContract(task_id="t-1", done_criteria=["x"])
        vc.lock("mac2")
        d = vc.to_dict()
        vc2 = VisionContract.from_dict(d)
        assert vc2.locked is True
        assert vc2.locked_by == "mac2"

    def test_schema_valid(self, registry):
        vc = VisionContract(task_id="t-1", description="desc",
                            done_criteria=["tests pass"])
        assert_valid(registry, "vision_contract", vc.to_dict())

    def test_schema_empty_criteria_invalid(self, registry):
        assert_invalid(registry, "vision_contract", {
            "id": "x", "task_id": "t", "description": "d", "done_criteria": [],
        })


# ── ExecutionLedger ──────────────────────────────────────────────────────


class TestExecutionLedger:
    def test_progress(self):
        el = ExecutionLedger(task_id="t-1")
        el.add_step("step 1")
        el.add_step("step 2")
        el.add_step("step 3")
        assert el.progress == 0.0

        el.advance_step(el.steps[0].step_id, "done")
        assert abs(el.progress - 1 / 3) < 0.01

        el.advance_step(el.steps[1].step_id, "done")
        el.advance_step(el.steps[2].step_id, "done")
        assert el.progress == 1.0

    def test_blocked(self):
        el = ExecutionLedger()
        el.add_step("s1")
        assert not el.is_blocked
        el.advance_step(el.steps[0].step_id, "blocked")
        assert el.is_blocked

    def test_advance_with_evidence(self):
        el = ExecutionLedger()
        s = el.add_step("do thing")
        el.advance_step(s.step_id, "done", evidence="test-output.log")
        assert "test-output.log" in el.steps[0].evidence_refs

    def test_advance_nonexistent(self):
        el = ExecutionLedger()
        assert el.advance_step("nope", "done") is False

    def test_round_trip(self):
        el = ExecutionLedger(task_id="t-1", node_id="mac1")
        el.add_step("s1")
        el.advance_step(el.steps[0].step_id, "in_progress")
        d = el.to_dict()
        el2 = ExecutionLedger.from_dict(d)
        assert el2.steps[0].status == "in_progress"
        assert el2.steps[0].started_at > 0

    def test_empty_progress(self):
        el = ExecutionLedger()
        assert el.progress == 0.0


# ── ChiefEscalationPacket ───────────────────────────────────────────────


class TestChiefEscalation:
    def test_create(self):
        esc = ChiefEscalationPacket(
            task_id="t-1", from_node="mac2",
            reason="no_consensus", urgency="high",
        )
        assert esc.reason == "no_consensus"
        assert esc.urgency == "high"

    def test_round_trip(self):
        esc = ChiefEscalationPacket(
            task_id="t-1", from_node="mac1",
            reason="high_risk", evidence_refs=["probe-1"],
        )
        d = esc.to_dict()
        esc2 = ChiefEscalationPacket.from_dict(d)
        assert esc2.evidence_refs == ["probe-1"]

    def test_schema_valid(self, registry):
        assert_valid(registry, "chief_escalation", {
            "id": "x", "task_id": "t", "from_node": "mac1",
            "reason": "no_consensus",
        })

    def test_schema_invalid_reason(self, registry):
        assert_invalid(registry, "chief_escalation", {
            "id": "x", "task_id": "t", "from_node": "mac1",
            "reason": "because_yolo",
        })


# ── ChiefDecisionPacket ─────────────────────────────────────────────────


class TestChiefDecisionV2:
    def test_create(self):
        cd = ChiefDecisionPacket(
            escalation_id="e-1", task_id="t-1",
            decision="modify", reasoning="needs adjustments",
            modifications=["use approach B for step 3"],
        )
        assert cd.decision == "modify"

    def test_round_trip(self):
        cd = ChiefDecisionPacket(
            escalation_id="e-1", task_id="t-1",
            decision="approve", reasoning="lgtm",
            memory_refs=["dao:principle-42"],
        )
        d = cd.to_dict()
        cd2 = ChiefDecisionPacket.from_dict(d)
        assert cd2.memory_refs == ["dao:principle-42"]

    def test_schema_valid(self, registry):
        assert_valid(registry, "chief_decision_v2", {
            "id": "x", "escalation_id": "e", "task_id": "t",
            "decision": "approve", "reasoning": "good",
        })

    def test_schema_invalid_decision(self, registry):
        assert_invalid(registry, "chief_decision_v2", {
            "id": "x", "escalation_id": "e", "task_id": "t",
            "decision": "yolo", "reasoning": "because",
        })


# ── packet_from_dict ─────────────────────────────────────────────────────


class TestPacketFromDict:
    def test_known_type(self):
        bp = packet_from_dict("brainstorm", {"task_id": "t-1", "node_id": "m1"})
        assert isinstance(bp, BrainstormPacket)

    def test_unknown_type(self):
        with pytest.raises(ValueError, match="Unknown vision packet"):
            packet_from_dict("imaginary", {})
