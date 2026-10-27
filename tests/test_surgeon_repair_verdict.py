"""Destructive repairs under critical stress require an explicit, structured
surgeon APPROVE verdict. Free-text substring matching ("approve" in output)
treated "do not approve" as approval — these tests pin that down."""

from unittest.mock import patch

import pytest

from multifleet.invariance_gates import FleetRepairGate


def _run(output, status="ok"):
    FleetRepairGate._repair_history = {}
    FleetRepairGate._repair_in_progress = {}
    review = {"status": status, "output": output}
    with patch("multifleet.invariance_gates._record_audit"), \
         patch("multifleet.surgeon_adapter.SurgeonAdapter.request_review",
               return_value=review):
        result = FleetRepairGate().check("mac2", "full_restart", stress_score=0.9)
    return next(c for c in result.checks if c.name == "stress_repair_policy")


@pytest.mark.parametrize("output", [
    "I do not approve this restart.",
    "Do NOT proceed. Disallow.",
    "We cannot allow a full restart under this stress.",
    "REJECT — unsafe to proceed",
    '{"verdict": "REJECT", "reason": "do not approve"}',
    "",
    "garbled ???",
    '{"verdict": "maybe"}',
    {"verdict": "REJECT"},
])
def test_non_approval_blocks_destructive_repair(output):
    assert not _run(output).passed


@pytest.mark.parametrize("output", [
    '{"verdict": "APPROVE", "reason": "node is wedged"}',
    'Analysis...\n```json\n{"verdict": "approve"}\n```',
    {"verdict": "APPROVE"},
])
def test_structured_approve_allows_destructive_repair(output):
    assert _run(output).passed


def test_unparseable_verdict_is_loud():
    FleetRepairGate._repair_history = {}
    FleetRepairGate._repair_in_progress = {}
    with patch("multifleet.invariance_gates._record_audit"), \
         patch("multifleet.surgeon_adapter.SurgeonAdapter.request_review",
               return_value={"status": "ok", "output": "sure, allow it"}):
        result = FleetRepairGate().check("mac2", "full_restart", stress_score=0.9)
    review = next(c for c in result.checks if c.name == "surgeon_repair_review")
    assert not review.passed
    assert "unparseable" in review.message.lower()
