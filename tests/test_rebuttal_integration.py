"""Integration tests for cross-machine rebuttal wired through the MCP tool layer.

Tests the fleet_cross_rebuttal MCP tool, handle_incoming_packet routing,
auto-advance on collected verdicts/critiques, and full lifecycle via tool calls.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))
# Also add superrepo root so `tools.*` imports work if needed
SUPERREPO_ROOT = FLEET_ROOT.parent
sys.path.insert(1, str(SUPERREPO_ROOT))

from multifleet.cross_rebuttal import RebuttalManager, RebuttalPhase, RebuttalSession


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mgr():
    return RebuttalManager()


@pytest.fixture
def task_brief():
    return {
        "task_id": "task-int-001",
        "objective": "Integration test task",
        "deadline_minutes": 30,
        "assigned_to": ["mac1", "mac2", "mac3"],
        "constraints": ["no breaking changes"],
    }


@pytest.fixture
def two_machine_brief():
    return {
        "task_id": "task-int-002",
        "objective": "Two-machine test",
        "deadline_minutes": 15,
        "assigned_to": ["mac1", "mac2"],
    }


@pytest.fixture
def started_session(mgr, task_brief):
    """A session that has been started and advanced to LOCAL_SURGERY."""
    session = mgr.start_session(task_brief)
    mgr.advance_session(session.task_id)
    return session


# ── MCP Tool: fleet_cross_rebuttal ──────────────────────────────────────────


class TestFleetCrossRebuttalTool:
    """Test fleet_cross_rebuttal via direct function calls (no MCP transport)."""

    def _import_tool(self):
        """Import the tool function, patching NODE_ID."""
        # Reset the global manager so each test is independent
        import multifleet.cross_rebuttal as cr_mod
        import importlib

        # We call the function directly, so we need to set up module state.
        # Import the MCP module and reset its cross-rebuttal manager.
        import bin.fleet_nerve_mcp as mcp_mod
        mcp_mod._cross_rebuttal_manager = None
        mcp_mod._chief_synthesis = None
        return mcp_mod.fleet_cross_rebuttal

    def test_start_creates_session(self):
        tool = self._import_tool()
        brief = {
            "task_id": "tool-start-001",
            "objective": "Test start",
            "deadline_minutes": 10,
            "assigned_to": ["mac1", "mac2"],
        }
        result = tool(action="start", payload=json.dumps(brief))
        assert result["status"] == "started"
        assert result["task_id"] == "tool-start-001"
        assert result["phase"] == "local_surgery"
        assert result["expected_machines"] == ["mac1", "mac2"]

    def test_start_requires_payload(self):
        tool = self._import_tool()
        result = tool(action="start")
        assert "error" in result

    def test_verdict_adds_to_session(self):
        tool = self._import_tool()
        brief = {
            "task_id": "tool-verd-001",
            "objective": "Test verdict",
            "deadline_minutes": 10,
            "assigned_to": ["mac1", "mac2"],
        }
        tool(action="start", payload=json.dumps(brief))
        verdict = {"from": "mac1", "summary": "looks good", "confidence": 0.85}
        result = tool(action="verdict", task_id="tool-verd-001",
                      payload=json.dumps(verdict))
        assert result["action"] == "verdict_recorded"
        assert result["verdicts_count"] == 1

    def test_verdict_requires_task_id(self):
        tool = self._import_tool()
        result = tool(action="verdict", payload='{"x":1}')
        assert "error" in result

    def test_critique_advances_phase(self):
        tool = self._import_tool()
        brief = {
            "task_id": "tool-crit-001",
            "objective": "Test critique",
            "deadline_minutes": 10,
            "assigned_to": ["mac1", "mac2"],
        }
        tool(action="start", payload=json.dumps(brief))

        # Submit all verdicts to advance to VERDICTS_COLLECTED
        for m in ["mac1", "mac2"]:
            tool(action="verdict", task_id="tool-crit-001",
                 payload=json.dumps({"from": m, "summary": "ok", "confidence": 0.8}))

        # Status should show verdicts_collected (auto-advanced)
        status = tool(action="status", task_id="tool-crit-001")
        assert status["phase"] == "verdicts_collected"

        # Submit critiques — first one advances to UNDER_REVIEW
        tool(action="critique", task_id="tool-crit-001",
             payload=json.dumps({"from": "mac1", "reviewing": "mac2", "stance": "agree"}))

        status = tool(action="status", task_id="tool-crit-001")
        assert status["phase"] == "under_review"

    def test_synthesize_produces_decision(self):
        tool = self._import_tool()
        brief = {
            "task_id": "tool-synth-001",
            "objective": "Test synthesis",
            "deadline_minutes": 10,
            "assigned_to": ["mac1", "mac2"],
        }
        tool(action="start", payload=json.dumps(brief))

        # Submit verdicts
        for m in ["mac1", "mac2"]:
            tool(action="verdict", task_id="tool-synth-001",
                 payload=json.dumps({
                     "from": m, "summary": f"{m} analysis",
                     "confidence": 0.85, "branch": f"feat/{m}",
                 }))

        # Submit critiques
        for m in ["mac1", "mac2"]:
            tool(action="critique", task_id="tool-synth-001",
                 payload=json.dumps({"from": m, "reviewing": "other", "stance": "agree"}))

        # Synthesize
        result = tool(action="synthesize", task_id="tool-synth-001")
        assert result["status"] == "synthesized"
        assert "decision" in result
        assert result["decision"]["decision"] in ("merge", "cherry_pick", "needs_rebuttal", "reject")
        assert result["session_phase"] == "resolved"

    def test_status_shows_current_state(self):
        tool = self._import_tool()
        brief = {
            "task_id": "tool-stat-001",
            "objective": "Test status",
            "deadline_minutes": 10,
            "assigned_to": ["mac1"],
        }
        tool(action="start", payload=json.dumps(brief))
        status = tool(action="status", task_id="tool-stat-001")
        assert status["task_id"] == "tool-stat-001"
        assert status["phase"] == "local_surgery"
        assert status["expected_machines"] == ["mac1"]
        assert status["verdicts_count"] == 0
        assert status["has_decision"] is False

    def test_status_nonexistent_task(self):
        tool = self._import_tool()
        result = tool(action="status", task_id="nonexistent")
        assert "error" in result

    def test_list_shows_all_sessions(self):
        tool = self._import_tool()
        # Start two sessions
        for i in range(2):
            brief = {
                "task_id": f"tool-list-{i}",
                "objective": f"List test {i}",
                "deadline_minutes": 10,
                "assigned_to": ["mac1"],
            }
            tool(action="start", payload=json.dumps(brief))

        result = tool(action="list")
        assert result["active_count"] == 2
        assert result["total_count"] == 2

    def test_unknown_action(self):
        tool = self._import_tool()
        result = tool(action="bogus")
        assert "error" in result
        assert "bogus" in result["error"]


# ── handle_incoming_packet routing ──────────────────────────────────────────


class TestHandleIncomingPacket:
    def test_routes_local_verdict(self, mgr, started_session):
        result = mgr.handle_incoming_packet("local_verdict", {
            "task_id": started_session.task_id,
            "from": "mac1",
            "summary": "done",
            "confidence": 0.9,
        })
        assert result["action"] == "verdict_recorded"
        assert result["verdicts_count"] == 1

    def test_routes_critique(self, mgr, started_session):
        # First collect all verdicts and advance
        for m in ["mac1", "mac2", "mac3"]:
            started_session.add_verdict(m, {"summary": "ok"})
        mgr.advance_session(started_session.task_id)  # -> VERDICTS_COLLECTED
        started_session.advance(RebuttalPhase.UNDER_REVIEW)

        result = mgr.handle_incoming_packet("critique", {
            "task_id": started_session.task_id,
            "from": "mac1",
            "stance": "agree",
        })
        assert result["action"] == "critique_recorded"

    def test_routes_chief_decision(self, mgr, started_session):
        # Walk to CRITIQUES_COLLECTED
        for m in ["mac1", "mac2", "mac3"]:
            started_session.add_verdict(m, {"summary": "ok"})
        started_session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        started_session.advance(RebuttalPhase.UNDER_REVIEW)
        for m in ["mac1", "mac2", "mac3"]:
            started_session.add_critique(m, {"stance": "agree"})
        started_session.advance(RebuttalPhase.CRITIQUES_COLLECTED)

        result = mgr.handle_incoming_packet("chief_decision", {
            "task_id": started_session.task_id,
            "decision": "merge",
            "winner_branch": "mac1/feat",
        })
        assert result["action"] == "auto_advanced"
        assert result["phase"] == "resolved"

    def test_unknown_packet_type(self, mgr, started_session):
        result = mgr.handle_incoming_packet("bogus_type", {
            "task_id": started_session.task_id,
        })
        assert result["action"] == "error"
        assert "bogus_type" in result["error"]

    def test_missing_task_id(self, mgr):
        result = mgr.handle_incoming_packet("local_verdict", {})
        assert result["action"] == "error"
        assert "task_id" in result["error"]

    def test_nonexistent_session(self, mgr):
        result = mgr.handle_incoming_packet("local_verdict", {
            "task_id": "no-such-task",
            "from": "mac1",
        })
        assert result["action"] == "error"

    def test_missing_machine_id_verdict(self, mgr, started_session):
        result = mgr.handle_incoming_packet("local_verdict", {
            "task_id": started_session.task_id,
        })
        assert result["action"] == "error"
        assert "from" in result["error"] or "machine" in result["error"]


# ── Auto-advance on all verdicts ────────────────────────────────────────────


class TestAutoAdvance:
    def test_auto_advance_on_all_verdicts(self, mgr, started_session):
        """When all expected machines submit verdicts, phase auto-advances."""
        for m in ["mac1", "mac2"]:
            mgr.handle_incoming_packet("local_verdict", {
                "task_id": started_session.task_id,
                "from": m,
                "confidence": 0.8,
            })
        # Not yet — still waiting for mac3
        assert started_session.phase == RebuttalPhase.LOCAL_SURGERY

        result = mgr.handle_incoming_packet("local_verdict", {
            "task_id": started_session.task_id,
            "from": "mac3",
            "confidence": 0.9,
        })
        assert result["action"] == "auto_advanced"
        assert result["advanced_to"] == "verdicts_collected"
        assert started_session.phase == RebuttalPhase.VERDICTS_COLLECTED

    def test_auto_advance_on_all_critiques(self, mgr, started_session):
        """When all expected machines submit critiques, phase auto-advances."""
        # Advance to UNDER_REVIEW
        for m in ["mac1", "mac2", "mac3"]:
            started_session.add_verdict(m, {"summary": "ok"})
        started_session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        started_session.advance(RebuttalPhase.UNDER_REVIEW)

        for m in ["mac1", "mac2"]:
            mgr.handle_incoming_packet("critique", {
                "task_id": started_session.task_id,
                "from": m,
                "stance": "agree",
            })
        assert started_session.phase == RebuttalPhase.UNDER_REVIEW

        result = mgr.handle_incoming_packet("critique", {
            "task_id": started_session.task_id,
            "from": "mac3",
            "stance": "partial_agree",
        })
        assert result["action"] == "auto_advanced"
        assert result["advanced_to"] == "critiques_collected"
        assert result["needs_synthesis"] is True


# ── Full lifecycle via MCP tool calls ───────────────────────────────────────


class TestFullLifecycle:
    def _import_tool(self):
        import bin.fleet_nerve_mcp as mcp_mod
        mcp_mod._cross_rebuttal_manager = None
        mcp_mod._chief_synthesis = None
        return mcp_mod.fleet_cross_rebuttal

    def test_full_4_phase_cycle(self):
        """Complete rebuttal lifecycle: start -> verdicts -> critiques -> synthesize."""
        tool = self._import_tool()

        # Phase 1: Start
        brief = {
            "task_id": "lifecycle-001",
            "objective": "Full lifecycle test",
            "deadline_minutes": 30,
            "assigned_to": ["mac1", "mac2", "mac3"],
        }
        start = tool(action="start", payload=json.dumps(brief))
        assert start["status"] == "started"
        assert start["phase"] == "local_surgery"

        # Phase 2: Submit verdicts from all machines
        for m in ["mac1", "mac2", "mac3"]:
            result = tool(action="verdict", task_id="lifecycle-001",
                          payload=json.dumps({
                              "from": m,
                              "summary": f"{m} completed analysis",
                              "confidence": 0.85,
                              "branch": f"feat/lifecycle-001-{m}",
                              "risks": [],
                              "dissent": [],
                          }))

        # Verify auto-advance to verdicts_collected
        status = tool(action="status", task_id="lifecycle-001")
        assert status["phase"] == "verdicts_collected"
        assert status["verdicts_count"] == 3

        # Phase 3: Submit critiques from all machines
        for m in ["mac1", "mac2", "mac3"]:
            result = tool(action="critique", task_id="lifecycle-001",
                          payload=json.dumps({
                              "from": m,
                              "reviewing": "all",
                              "stance": "agree",
                              "agreements": ["solid approach"],
                          }))

        # Verify auto-advance to critiques_collected
        status = tool(action="status", task_id="lifecycle-001")
        assert status["phase"] == "critiques_collected"
        assert status["critiques_count"] == 3

        # Phase 4: Synthesize
        result = tool(action="synthesize", task_id="lifecycle-001")
        assert result["status"] == "synthesized"
        assert result["session_phase"] == "resolved"

        # Final status
        status = tool(action="status", task_id="lifecycle-001")
        assert status["phase"] == "resolved"
        assert status["has_decision"] is True

        # List should show 0 active (resolved), 1 total
        listing = tool(action="list")
        assert listing["active_count"] == 0
        assert listing["total_count"] == 1
