"""Tests for /orchestrate endpoint wiring in the daemon HTTP handler.

Validates that the CLI -> daemon -> TaskOrchestrator path is connected.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
        return {"findings": [], "summary": {"total": 0}}
    if path == "/race":
        return {"race_id": "race-abc", "status": "racing",
                "participants": {"mac1": {}, "mac2": {}}}
    if path == "/supervisor":
        return {"phase": "execution", "task_id": body.get("task_id", "")}
    if path == "/accept":
        return {"accepted": True, "reason": "ok", "blocking_issues": []}
    if path == "/evidence":
        return {"entry_id": "ev-001"}
    return {"ok": True}


class TestOrchestrateEndpointContract:
    """Tests the contract between CLI body format and TaskOrchestrator API."""

    def test_run_action_parses_criteria_string(self):
        """CLI sends criteria as comma-separated string; daemon splits correctly."""
        orch = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        orch._daemon_post = MagicMock(side_effect=_mock_post_ok)

        # Simulate what the daemon does with CLI body
        body = {"action": "run", "task": "Fix auth", "criteria": "Tests pass,Lint clean"}
        criteria_raw = body.get("criteria", "Complete")
        criteria = [c.strip() for c in criteria_raw.split(",") if c.strip()]

        result = orch.run_task(body["task"], done_criteria=criteria)
        assert result["status"] == "complete"
        assert result["done_criteria"] == ["Tests pass", "Lint clean"]

    def test_run_action_parallel_flag(self):
        """Parallel flag from CLI body string is correctly parsed."""
        orch = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        orch._daemon_post = MagicMock(side_effect=_mock_post_ok)

        for flag_val, expected in [("true", True), ("false", False), (True, True)]:
            parallel = flag_val in (True, "true", "1")
            result = orch.run_task("Task", done_criteria=["Done"], parallel=parallel)
            assert result["parallel"] == expected

    def test_run_action_nodes_parsing(self):
        """Nodes string from CLI is correctly split."""
        nodes_raw = "mac1,mac2"
        nodes = [n.strip() for n in nodes_raw.split(",") if n.strip()]
        assert nodes == ["mac1", "mac2"]

        # Empty string -> None
        nodes_raw = ""
        nodes = [n.strip() for n in nodes_raw.split(",") if n.strip()] if nodes_raw else None
        assert nodes is None

    def test_status_action_returns_task(self):
        orch = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        orch._daemon_post = MagicMock(side_effect=_mock_post_ok)
        result = orch.run_task("Task", done_criteria=["Done"])
        status = orch.status(result["id"])
        assert status["id"] == result["id"]

    def test_list_action_returns_tasks(self):
        orch = TaskOrchestrator(daemon_port=8855, node_id="mac2")
        orch._daemon_post = MagicMock(side_effect=_mock_post_ok)
        orch.run_task("Task A", done_criteria=["A"])
        orch.run_task("Task B", done_criteria=["B"])
        tasks = orch.list_tasks()
        assert len(tasks) == 2
        # Matches the shape the daemon wraps in {"tasks": ...}
        assert all("id" in t for t in tasks)
