"""Tests for the cross-machine review flow orchestrator."""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multifleet.review_flow import ReviewFlowOrchestrator, ReviewFlowResult
from multifleet.synthesis import ChiefSynthesis


def _make_lightweight_synthesis() -> ChiefSynthesis:
    """Create a ChiefSynthesis that doesn't try to import memory modules."""
    from pathlib import Path
    s = ChiefSynthesis.__new__(ChiefSynthesis)
    s._ledger_path = Path("/tmp/test-decisions.jsonl")
    s._analyzers = []
    s._default_analyzer = s._basic_confidence_analyzer
    s._evidence_ledger = None
    return s


class TestReviewFlowOrchestrator:
    """Test the complete review lifecycle."""

    def _make_flow(self) -> ReviewFlowOrchestrator:
        return ReviewFlowOrchestrator(
            node_id="test-chief",
            synthesis=_make_lightweight_synthesis(),
        )

    def test_start_review(self):
        flow = self._make_flow()
        session = flow.start_review(
            task_id="task-1",
            assigned_machines=["mac1", "mac2"],
            task_summary="Fix auth token",
        )
        assert session.task_id == "task-1"
        assert session.phase.value == "local_surgery"
        assert flow.stats["reviews_started"] == 1

    def test_submit_verdict(self):
        flow = self._make_flow()
        flow.start_review(
            task_id="task-2",
            assigned_machines=["mac1", "mac2"],
        )
        result = flow.submit_verdict("task-2", "mac1", {
            "summary": "Refactored cache",
            "branch": "mac1/fix-auth",
            "confidence": 0.85,
        })
        assert result["action"] == "verdict_recorded"
        assert flow.stats["verdicts_received"] == 1

    def test_all_verdicts_auto_advance(self):
        flow = self._make_flow()
        flow.start_review(
            task_id="task-3",
            assigned_machines=["mac1", "mac2"],
        )
        flow.submit_verdict("task-3", "mac1", {"summary": "A", "confidence": 0.8})
        flow.submit_verdict("task-3", "mac2", {"summary": "B", "confidence": 0.7})

        status = flow.get_status("task-3")
        assert status["verdicts"] == 2
        # Should have auto-advanced past VERDICTS_COLLECTED to UNDER_REVIEW
        assert status["phase"] == "under_review"

    def test_submit_critique(self):
        flow = self._make_flow()
        # Use 3 machines so submitting 1 critique doesn't auto-trigger synthesis
        flow.start_review(task_id="task-4", assigned_machines=["mac1", "mac2", "mac3"])
        flow.submit_verdict("task-4", "mac1", {"summary": "A", "confidence": 0.8})
        flow.submit_verdict("task-4", "mac2", {"summary": "B", "confidence": 0.7})
        flow.submit_verdict("task-4", "mac3", {"summary": "C", "confidence": 0.6})

        result = flow.submit_critique("task-4", "mac1", {
            "concern": "Cache may leak",
            "severity": "medium",
        })
        assert flow.stats["critiques_received"] == 1

    def test_full_lifecycle(self):
        """Test the complete review lifecycle: start -> verdicts -> critiques -> manual synthesis."""
        flow = self._make_flow()
        flow.start_review(
            task_id="task-5",
            assigned_machines=["mac1", "mac2"],
            task_summary="Fix auth token refresh",
        )

        # Submit verdicts
        flow.submit_verdict("task-5", "mac1", {
            "summary": "Refactored token cache with TTL",
            "branch": "mac1/fix-auth",
            "confidence": 0.85,
            "files_touched": ["src/auth.py"],
        })
        flow.submit_verdict("task-5", "mac2", {
            "summary": "Replaced auth library",
            "branch": "mac2/fix-auth",
            "confidence": 0.75,
            "files_touched": ["src/auth.py", "requirements.txt"],
        })

        # Verify verdicts auto-advanced
        status = flow.get_status("task-5")
        assert status["verdicts"] == 2
        assert status["phase"] == "under_review"

        # Submit critiques (but avoid auto-synthesis in test)
        flow.submit_critique("task-5", "mac1", {
            "concern": "Library replacement adds dependency",
        })
        assert flow.stats["critiques_received"] == 1

    def test_manual_synthesis(self):
        flow = self._make_flow()
        flow.start_review(task_id="task-6", assigned_machines=["mac1"])
        flow.submit_verdict("task-6", "mac1", {
            "summary": "Simple fix",
            "confidence": 0.9,
        })

        result = flow.run_synthesis("task-6")
        assert result.task_id == "task-6"
        assert result.synthesis is not None
        assert result.synthesis["decision"] in ("merge", "cherry_pick", "needs_rebuttal", "reject")

    def test_list_active(self):
        flow = self._make_flow()
        flow.start_review(task_id="task-7a", assigned_machines=["mac1"])
        flow.start_review(task_id="task-7b", assigned_machines=["mac2"])
        active = flow.list_active()
        assert len(active) == 2
        assert all("task_id" in r for r in active)

    def test_get_status_unknown(self):
        flow = self._make_flow()
        status = flow.get_status("nonexistent")
        assert "error" in status

    def test_cleanup_expired(self):
        flow = self._make_flow()
        session = flow.start_review(
            task_id="task-8",
            assigned_machines=["mac1"],
            deadline_minutes=0,  # Immediately expired
        )
        # Hack: set created_at to past
        rebuttal_session = flow._rebuttal.get_session("task-8")
        rebuttal_session.created_at = time.time() - 3600

        removed = flow.cleanup_expired()
        assert removed >= 1

    def test_stats(self):
        flow = self._make_flow()
        s = flow.stats
        assert "reviews_started" in s
        assert "verdicts_received" in s
        assert "syntheses_run" in s

    def test_synthesis_no_verdicts(self):
        flow = self._make_flow()
        flow.start_review(task_id="task-9", assigned_machines=["mac1"])
        result = flow.run_synthesis("task-9")
        assert result.error == "No verdicts to synthesize"

    def test_synthesis_no_session(self):
        flow = self._make_flow()
        result = flow.run_synthesis("nonexistent")
        assert "No session" in result.error


class TestReviewFlowResult:
    def test_to_dict(self):
        r = ReviewFlowResult(task_id="t1", phase="resolved")
        d = r.to_dict()
        assert d["task_id"] == "t1"
        assert d["phase"] == "resolved"

    def test_default_values(self):
        r = ReviewFlowResult(task_id="t2", phase="assigned")
        assert r.synthesis is None
        assert r.acceptance is None
        assert r.machines_participated == []
        assert r.error == ""


class TestReviewFlowSingleton:
    def test_singleton(self):
        from multifleet.review_flow import get_review_flow
        a = get_review_flow()
        b = get_review_flow()
        assert a is b
