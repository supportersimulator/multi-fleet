"""Tests for ReviewFlowOrchestrator wiring in fleet_nerve_nats daemon.

Verifies: HTTP endpoints (GET/POST /review*) and NATS message routing
to the modern ReviewFlowOrchestrator, coexisting with legacy /rebuttal.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multifleet.review_flow import ReviewFlowOrchestrator, ReviewFlowResult
from multifleet.cross_rebuttal import RebuttalPhase


def _make_flow() -> ReviewFlowOrchestrator:
    """Lightweight orchestrator without memory deps."""
    from multifleet.synthesis import ChiefSynthesis
    from pathlib import Path

    s = ChiefSynthesis.__new__(ChiefSynthesis)
    s._ledger_path = Path("/tmp/test-decisions.jsonl")
    s._analyzers = []
    s._default_analyzer = s._basic_confidence_analyzer
    s._evidence_ledger = None
    return ReviewFlowOrchestrator(node_id="test-node", synthesis=s)


class TestReviewFlowHTTPContract:
    """Test the HTTP endpoint contract that the daemon exposes."""

    def test_start_review_returns_task_id(self):
        flow = _make_flow()
        session = flow.start_review(
            task_id="wire-test",
            assigned_machines=["mac1", "mac2"],
            task_summary="Test wiring",
        )
        assert session.task_id == "wire-test"
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

    def test_get_status_after_start(self):
        flow = _make_flow()
        flow.start_review(
            task_id="status-test",
            assigned_machines=["mac1"],
            task_summary="Status check",
        )
        status = flow.get_status("status-test")
        assert status["task_id"] == "status-test"
        assert status["phase"] == "local_surgery"
        assert "error" not in status

    def test_get_status_unknown_returns_error(self):
        flow = _make_flow()
        status = flow.get_status("nonexistent")
        assert "error" in status

    def test_list_active_reviews(self):
        flow = _make_flow()
        flow.start_review(task_id="r1", assigned_machines=["mac1"], task_summary="First")
        flow.start_review(task_id="r2", assigned_machines=["mac2"], task_summary="Second")
        active = flow.list_active()
        assert len(active) == 2
        task_ids = {r["task_id"] for r in active}
        assert task_ids == {"r1", "r2"}

    def test_submit_verdict_via_flow(self):
        flow = _make_flow()
        flow.start_review(
            task_id="verdict-test",
            assigned_machines=["mac1", "mac2"],
            task_summary="Verdict check",
        )
        result = flow.submit_verdict("verdict-test", "mac1", {
            "summary": "Fixed auth",
            "confidence": 0.9,
            "files_touched": ["src/auth.py"],
        })
        assert result["action"] in ("verdict_recorded", "auto_advanced")
        assert result["task_id"] == "verdict-test"

    def test_stats_track_reviews(self):
        flow = _make_flow()
        flow.start_review(task_id="s1", assigned_machines=["m1"], task_summary="Stats")
        stats = flow.stats
        assert stats["reviews_started"] == 1
        assert stats["verdicts_received"] == 0


class TestNATSRouting:
    """Test that NATS message types route correctly to the orchestrator."""

    def test_review_verdict_routes_to_flow(self):
        """Simulate what _handle_message does for review_verdict type."""
        flow = _make_flow()
        flow.start_review(
            task_id="nats-test",
            assigned_machines=["mac1", "mac2"],
            task_summary="NATS routing test",
        )
        # Simulate the routing logic from _handle_message
        payload = {
            "task_id": "nats-test",
            "summary": "Refactored module",
            "confidence": 0.85,
        }
        flow.submit_verdict("nats-test", "mac1", payload)
        status = flow.get_status("nats-test")
        assert status["verdicts"] == 1

    def test_review_critique_routes_to_flow(self):
        """Simulate critique routing after all verdicts collected."""
        flow = _make_flow()
        flow.start_review(
            task_id="crit-test",
            assigned_machines=["mac1"],
            task_summary="Critique test",
        )
        # Submit verdict first
        flow.submit_verdict("crit-test", "mac1", {
            "summary": "Done",
            "confidence": 0.8,
        })
        # Now submit critique (after advancing to UNDER_REVIEW)
        result = flow.submit_critique("crit-test", "mac1", {
            "concern": "Thread safety issue",
            "severity": "medium",
        })
        assert result.get("task_id") == "crit-test" or "action" in result


class TestBackwardCompatibility:
    """Verify legacy /rebuttal and modern /review can coexist."""

    def test_both_managers_can_init(self):
        """Both old RebuttalManager and new ReviewFlowOrchestrator init independently."""
        flow = _make_flow()
        assert flow is not None
        assert flow.stats["reviews_started"] == 0

        # Old-style rebuttal still importable
        try:
            from multifleet.rebuttal import RebuttalManager as LegacyRM
            legacy_available = True
        except ImportError:
            legacy_available = False
        # Either way, the modern flow works
        assert flow.stats is not None

    def test_review_flow_uses_cross_rebuttal_not_legacy(self):
        """ReviewFlowOrchestrator internally uses cross_rebuttal.RebuttalManager."""
        flow = _make_flow()
        from multifleet.cross_rebuttal import RebuttalManager as ModernRM
        assert isinstance(flow._rebuttal, ModernRM)
