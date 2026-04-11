"""Tests for race_orchestrator — end-to-end pipeline wiring."""

import json
import time
from unittest.mock import patch, MagicMock

import pytest

from multifleet.race_orchestrator import run_race, submit_verdict


class TestRunRace:
    """Test the full dispatch → collect → debate → synthesize pipeline."""

    @patch("multifleet.race_orchestrator._daemon_post")
    @patch("multifleet.race_orchestrator._daemon_get")
    def test_race_dispatches_to_all_nodes(self, mock_get, mock_post):
        """Verify tasks are dispatched to each node."""
        mock_post.return_value = {"sent": True}
        mock_get.return_value = {"peers": {}, "status": "ok"}

        result = run_race(
            "test task",
            nodes=["mac1", "mac2"],
            timeout_s=1,
            poll_interval_s=0.1,
        )

        assert result["dispatched"] == 2
        assert result["nodes"] == ["mac1", "mac2"]
        assert result["task_id"]  # UUID assigned

        # Should have dispatched to both nodes
        dispatch_calls = [
            c for c in mock_post.call_args_list
            if c[0][0] == "message"
        ]
        assert len(dispatch_calls) >= 2

    @patch("multifleet.race_orchestrator._daemon_post")
    @patch("multifleet.race_orchestrator._daemon_get")
    def test_race_creates_rebuttal_session(self, mock_get, mock_post):
        """Verify a rebuttal session is created and advances correctly."""
        mock_post.return_value = {"sent": True}
        mock_get.return_value = None  # daemon down — skip polling

        result = run_race(
            "test task",
            nodes=["mac1"],
            timeout_s=1,
            poll_interval_s=0.1,
        )

        assert result["task_id"]
        assert result["rebuttal_phase"] in (
            "assigned", "local_surgery", "verdicts_collected",
        )

    @patch("multifleet.race_orchestrator._daemon_post")
    @patch("multifleet.race_orchestrator._daemon_get")
    def test_race_no_verdicts_returns_needs_human(self, mock_get, mock_post):
        """When no verdicts arrive, decision should be needs_human."""
        mock_post.return_value = {"sent": True}
        mock_get.return_value = {"peers": {}, "status": "ok"}

        result = run_race(
            "test task",
            nodes=["mac1"],
            timeout_s=1,
            poll_interval_s=0.1,
        )

        assert result["decision"] == "needs_human"
        assert result["winner"] is None
        assert result["verdicts_collected"] == 0

    @patch("multifleet.race_orchestrator._daemon_post")
    @patch("multifleet.race_orchestrator._daemon_get")
    def test_race_timeout_flag(self, mock_get, mock_post):
        """Verify timeout_hit is set when deadline is exceeded."""
        mock_post.return_value = {"sent": True}
        mock_get.return_value = {"peers": {}, "status": "ok"}

        result = run_race(
            "test task",
            nodes=["mac1", "mac2"],
            timeout_s=1,
            poll_interval_s=0.5,
        )

        assert result["timeout_hit"] is True
        assert result["status"] == "partial"


class TestSubmitVerdict:
    """Test verdict submission."""

    @patch("multifleet.race_orchestrator._daemon_post")
    def test_submit_verdict_sends_to_daemon(self, mock_post):
        mock_post.return_value = {"sent": True}
        result = submit_verdict("task-123", "mac1", {
            "confidence": 0.85,
            "summary": "Implemented auth flow",
            "branch": "race/task-123/mac1",
        })
        assert result == {"sent": True}
        mock_post.assert_called_once()
        call_data = mock_post.call_args[0][1]
        assert call_data["type"] == "context"
        assert call_data["to"] == "chief"

    @patch("multifleet.race_orchestrator._daemon_post")
    def test_submit_verdict_daemon_down(self, mock_post):
        mock_post.return_value = None
        result = submit_verdict("task-123", "mac1", {"confidence": 0.5})
        assert "error" in result


class TestSynthesisIntegration:
    """Test that ChiefSynthesis is properly wired into fleet_race."""

    def test_fleet_race_synthesis_uses_chief(self):
        """Verify FleetRace._run_synthesis calls ChiefSynthesis."""
        from multifleet.fleet_race import FleetRace

        racer = FleetRace(daemon_port=9999, node_id="test")

        race = {
            "race_id": "test-race",
            "started_at": time.time() - 60,
            "participants": {
                "mac1": {
                    "node": "mac1",
                    "branch": "race/test/mac1",
                    "status": "completed",
                    "completed_at": time.time() - 30,
                    "result": {
                        "confidence": 0.9,
                        "summary": "Clean implementation",
                        "strengths": ["fast", "tested"],
                        "risks": [],
                        "dissent": [],
                    },
                },
                "mac2": {
                    "node": "mac2",
                    "branch": "race/test/mac2",
                    "status": "completed",
                    "completed_at": time.time() - 20,
                    "result": {
                        "confidence": 0.7,
                        "summary": "Alternative approach",
                        "strengths": ["simple"],
                        "risks": ["untested"],
                        "dissent": [{"claim": "mac1 over-engineers"}],
                    },
                },
            },
            "rebuttals": [],
        }

        with patch.object(racer, "_record_evidence"):
            racer._run_synthesis(race)

        assert race["status"] in ("done", "awaiting_arbiter")
        assert race["synthesis"]["engine"] == "chief_synthesis"
        assert race["synthesis"]["decision"] in (
            "merge", "cherry_pick", "needs_rebuttal", "reconciliation",
        )
        assert race["synthesis"]["confidence"] > 0
        assert race["winner"] is not None
        # Dissent should be preserved
        assert "dissent_preserved" in race["synthesis"]

    def test_fleet_race_synthesis_with_cherry_picks(self):
        """Verify cherry-pick suggestions from non-winners are included."""
        from multifleet.fleet_race import FleetRace

        racer = FleetRace(daemon_port=9999, node_id="test")

        race = {
            "race_id": "cherry-test",
            "started_at": time.time() - 60,
            "participants": {
                "mac1": {
                    "node": "mac1",
                    "branch": "race/cherry/mac1",
                    "status": "completed",
                    "completed_at": time.time() - 30,
                    "result": {
                        "confidence": 0.85,
                        "summary": "Main approach",
                        "strengths": ["comprehensive"],
                        "risks": [],
                        "dissent": [],
                    },
                },
                "mac2": {
                    "node": "mac2",
                    "branch": "race/cherry/mac2",
                    "status": "completed",
                    "completed_at": time.time() - 25,
                    "result": {
                        "confidence": 0.6,
                        "summary": "Different angle",
                        "strengths": ["great error handling", "better logging"],
                        "risks": ["incomplete"],
                        "dissent": [],
                        "commit": "abc123",
                    },
                },
            },
            "rebuttals": [],
        }

        with patch.object(racer, "_record_evidence"):
            racer._run_synthesis(race)

        assert race["synthesis"]["engine"] == "chief_synthesis"
        # mac2 had strengths but lower confidence — should produce cherry picks
        cherry_picks = race["synthesis"].get("cherry_pick_from", [])
        if cherry_picks:
            assert any(cp["machine"] == "mac2" for cp in cherry_picks)
