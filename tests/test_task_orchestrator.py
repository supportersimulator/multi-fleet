"""Tests for TaskOrchestrator — unified lifecycle from idea to wisdom."""

import time
from unittest.mock import MagicMock, patch

import pytest
import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.task_orchestrator import TaskOrchestrator


def _mock_post_ok(path, body, **kw):
    """Smart mock returning correct response shape per endpoint."""
    if path == "/truth":
        action = body.get("action", "")
        if action == "add_inbox":
            return {"id": "truth-001", "layer": "inbox"}
        if action == "promote":
            target = body.get("target_layer", "vision")
            return {"id": f"truth-{target}-002", "layer": target}
    if path == "/scan":
        return {"findings": [{"title": "test finding"}], "summary": {"total": 1}}
    if path == "/race":
        return {"race_id": "race-abc", "status": "racing",
                "participants": {"mac1": {"node": "mac1"}, "mac2": {"node": "mac2"}},
                "task": body.get("task", "")}
    if path == "/supervisor":
        return {"phase": "execution", "task_id": body.get("task_id", "")}
    if path == "/accept":
        return {"accepted": True, "reason": "all criteria met", "blocking_issues": []}
    if path == "/evidence":
        return {"entry_id": "ev-001", "recorded": True}
    return {"ok": True}


@pytest.fixture
def orch():
    """Orchestrator with mocked daemon HTTP."""
    o = TaskOrchestrator(daemon_port=8855, node_id="mac2")
    o._daemon_post = MagicMock(side_effect=_mock_post_ok)
    o._daemon_get = MagicMock(return_value={"status": "ok"})
    return o


class TestRunTask:

    def test_full_lifecycle_local(self, orch):
        result = orch.run_task("Fix auth flow", done_criteria=["Tests pass"])
        assert result["status"] == "complete"
        assert result["id"]
        assert "inbox" in result["phases"]
        assert "probe_baseline" in result["phases"]
        assert "execute" in result["phases"]
        assert "review" in result["phases"]
        assert "promote" in result["phases"]
        assert "record" in result["phases"]

    def test_full_lifecycle_parallel(self, orch):
        result = orch.run_task("Optimize queries",
                               done_criteria=["Latency < 50ms"],
                               parallel=True, nodes=["mac1", "mac2"])
        assert result["status"] == "complete"
        assert result["parallel"] is True
        assert result["phases"]["execute"]["name"] == "race"
        assert "race_id" in result

    def test_six_phases_executed(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        assert len(result["phases"]) == 6

    def test_timing_recorded(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        assert "elapsed_s" in result
        assert result["elapsed_s"] >= 0
        for phase in result["phases"].values():
            assert "elapsed_ms" in phase

    def test_truth_id_propagated(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        assert result.get("truth_id") == "truth-001"

    def test_acceptance_propagated(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        assert result["accepted"] is True
        assert result["phases"]["review"]["accepted"] is True

    def test_promotion_chain(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        promotions = result["phases"]["promote"]["promotions"]
        # inbox -> vision, vision -> snapshot (since accepted=True)
        assert len(promotions) == 2
        assert promotions[0]["to_layer"] == "vision"
        assert promotions[1]["to_layer"] == "snapshot"


class TestParallelFlag:

    def test_race_endpoint_called(self, orch):
        orch.run_task("Task", done_criteria=["Done"], parallel=True, nodes=["mac1"])
        calls = [c for c in orch._daemon_post.call_args_list if c[0][0] == "/race"]
        assert len(calls) == 1
        assert calls[0][0][1]["task"] == "Task"

    def test_local_supervisor_called(self, orch):
        orch.run_task("Task", done_criteria=["Done"], parallel=False)
        calls = [c for c in orch._daemon_post.call_args_list if c[0][0] == "/supervisor"]
        assert len(calls) >= 1


class TestStatusTracking:

    def test_status_returns_task(self, orch):
        result = orch.run_task("Task", done_criteria=["Done"])
        status = orch.status(result["id"])
        assert status["id"] == result["id"]
        assert status["status"] == "complete"

    def test_status_unknown_task(self, orch):
        status = orch.status("nonexistent")
        assert "error" in status

    def test_list_tasks(self, orch):
        orch.run_task("Task A", done_criteria=["A"])
        orch.run_task("Task B", done_criteria=["B"])
        tasks = orch.list_tasks()
        assert len(tasks) == 2
        assert all("id" in t for t in tasks)
        assert all("status" in t for t in tasks)


class TestErrorHandling:

    def test_daemon_failure_captured(self):
        o = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        o._daemon_post = MagicMock(side_effect=ConnectionError("unreachable"))
        result = o.run_task("Task", done_criteria=["Done"])
        assert result["status"] == "complete_with_errors"
        assert len(result["errors"]) > 0

    def test_partial_failure_continues(self):
        """Even if inbox fails, later phases still run."""
        call_count = 0
        def _fail_first(path, body, **kw):
            nonlocal call_count
            call_count += 1
            if path == "/truth" and body.get("action") == "add_inbox":
                raise ConnectionError("truth service down")
            return _mock_post_ok(path, body)

        o = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        o._daemon_post = MagicMock(side_effect=_fail_first)
        result = o.run_task("Task", done_criteria=["Done"])
        # Should still have all 6 phases attempted
        assert len(result["phases"]) == 6
        assert result["phases"]["inbox"]["status"] == "error"
        # Promote should be skipped (no truth_id)
        assert result["phases"]["promote"]["status"] == "skipped"

    def test_no_snapshot_when_not_accepted(self):
        """If acceptance gate rejects, no snapshot promotion."""
        def _reject_accept(path, body, **kw):
            if path == "/accept":
                return {"accepted": False, "reason": "tests fail", "blocking_issues": ["test failure"]}
            return _mock_post_ok(path, body)

        o = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        o._daemon_post = MagicMock(side_effect=_reject_accept)
        result = o.run_task("Task", done_criteria=["Tests pass"])
        promotions = result["phases"]["promote"]["promotions"]
        # Only inbox -> vision, NOT vision -> snapshot
        assert len(promotions) == 1
        assert promotions[0]["to_layer"] == "vision"
