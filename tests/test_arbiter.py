"""Tests for the Human Arbiter — decision queueing, resolution, and execution."""

import time
from unittest.mock import patch

import pytest

from multifleet.arbiter import HumanArbiter, PendingDecision


def _make_arbiter() -> HumanArbiter:
    """Create arbiter with daemon calls disabled."""
    return HumanArbiter(daemon_port=19999)


def _queue_sample(arbiter: HumanArbiter, **overrides) -> PendingDecision:
    """Queue a sample decision with sensible defaults."""
    defaults = dict(
        decision_type="race",
        task="Fix auth flow",
        recommendation={"approach": "rewrite", "confidence": 0.65, "reasoning": "cleaner"},
        alternatives=[
            {"approach": "patch", "node": "mac1", "summary": "quick fix", "strengths": ["fast"]},
            {"approach": "refactor", "node": "mac3", "summary": "full redo", "strengths": ["clean"]},
        ],
        dissent=[{"surgeon": "neurologist", "concern": "perf regression", "severity": "medium"}],
        evidence=[{"type": "probe", "result": "passed"}],
        source_id="race-abc123",
    )
    defaults.update(overrides)
    return arbiter.queue_decision(**defaults)


class TestQueueCreates:
    def test_queue_creates_pending(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        assert decision.status == "pending"
        assert decision.decision_type == "race"
        assert decision.task_description == "Fix auth flow"
        assert len(decision.id) == 8
        assert decision.source_id == "race-abc123"

    def test_queue_appears_in_pending_list(self):
        arbiter = _make_arbiter()
        _queue_sample(arbiter)

        pending = arbiter.get_pending()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"


class TestDecide:
    def test_decide_moves_to_decided(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "accept_recommendation")

        assert result["decided"] is True
        assert result["decision_id"] == decision.id
        assert arbiter.get_pending() == []
        assert len(arbiter.get_decided()) == 1

    def test_decide_records_evidence(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        # The evidence recording uses urllib which will fail on bad port — that is fine,
        # we just verify the decide path completes without error
        result = arbiter.decide(decision.id, "accept_recommendation")
        assert result["decided"] is True

        decided = arbiter.get_decided()
        assert decided[0]["decision"]["action"] == "accept_recommendation"
        assert decided[0]["decision"]["decided_by"] == "human"


class TestGetPendingList:
    def test_get_pending_list(self):
        arbiter = _make_arbiter()
        d1 = _queue_sample(arbiter, task="Task A")
        d2 = _queue_sample(arbiter, task="Task B")

        pending = arbiter.get_pending()
        assert len(pending) == 2
        tasks = {p["task_description"] for p in pending}
        assert tasks == {"Task A", "Task B"}

    def test_get_decision_by_id(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        fetched = arbiter.get_decision(decision.id)
        assert fetched is not None
        assert fetched["id"] == decision.id

    def test_get_decision_unknown_returns_none(self):
        arbiter = _make_arbiter()
        assert arbiter.get_decision("nonexistent") is None


class TestAcceptRecommendation:
    def test_accept_recommendation(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "accept_recommendation")
        assert result["result"]["executed"] == "recommendation accepted"
        assert result["result"]["approach"] == "rewrite"


class TestAcceptAlternative:
    def test_accept_alternative_1(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "accept_alternative_1")
        assert result["result"]["executed"] == "alternative accepted"
        assert result["result"]["approach"] == "patch"

    def test_accept_alternative_2(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "accept_alternative_2")
        assert result["result"]["executed"] == "alternative accepted"
        assert result["result"]["approach"] == "refactor"

    def test_accept_alternative_out_of_range(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "accept_alternative_99")
        assert result["result"]["executed"] == "alternative index out of range"


class TestOverrideCustom:
    def test_override_custom(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "override_custom", custom_reason="Do it my way")
        assert result["result"]["executed"] == "custom override"
        assert result["result"]["reason"] == "Do it my way"


class TestUnknownDecision:
    def test_unknown_decision_returns_error(self):
        arbiter = _make_arbiter()

        result = arbiter.decide("no-such-id", "accept_recommendation")
        assert "error" in result
        assert "Unknown decision" in result["error"]


class TestRequestInfo:
    def test_request_info_action(self):
        arbiter = _make_arbiter()
        decision = _queue_sample(arbiter)

        result = arbiter.decide(decision.id, "request_info")
        assert result["result"]["executed"] == "info requested"


class TestExpireStale:
    def test_expire_stale_removes_old_decisions(self):
        arbiter = HumanArbiter(daemon_port=19999, max_age_s=1)
        decision = _queue_sample(arbiter)
        # Backdate the decision
        decision.created_at = time.time() - 10

        expired_count = arbiter.expire_stale()
        assert expired_count == 1
        assert arbiter.get_pending() == []
        assert len(arbiter.get_decided()) == 1
        assert arbiter.get_decided()[0]["status"] == "expired"
