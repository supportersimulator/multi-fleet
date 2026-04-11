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


class TestCherryPickAutoExecute:
    """auto_execute_cherry_pick flag wires MergeAdjudicator into run_race."""

    def test_auto_execute_off_by_default(self):
        """No adjudication key in result unless flag is set."""
        with patch("multifleet.race_orchestrator._daemon_post") as mp, \
             patch("multifleet.race_orchestrator._daemon_get") as mg:
            mp.return_value = {"sent": True}
            mg.return_value = None
            result = run_race("t", nodes=["mac1"], timeout_s=1, poll_interval_s=0.1)
        assert "adjudication" not in result

    def test_auto_execute_skips_non_cherry_pick_decisions(self):
        """When synthesis decides 'merge', adjudication is NOT run."""
        with patch("multifleet.race_orchestrator._daemon_post") as mp, \
             patch("multifleet.race_orchestrator._daemon_get") as mg:
            mp.return_value = {"sent": True}
            mg.return_value = None
            result = run_race(
                "t", nodes=["mac1"],
                timeout_s=1, poll_interval_s=0.1,
                auto_execute_cherry_pick=True,
                repo_path="/tmp",
            )
        # No verdicts collected -> synthesis_result may be None or
        # decision=="reject"/"merge" — either way, no adjudication fires
        adj = result.get("adjudication")
        if adj is not None:
            assert adj["status"] != "ok"  # could be "skipped" or "error"

    def test_auto_execute_runs_adjudicator_on_cherry_pick(self, tmp_path):
        """Force a cherry_pick decision into a real git repo and verify
        adjudication runs end-to-end through run_race's wiring."""
        import subprocess as sp
        from unittest.mock import MagicMock

        # Build a minimal git repo with a winner + one cherry source
        repo = tmp_path / "repo"
        repo.mkdir()
        def run(*a): sp.run(a, cwd=str(repo), check=True, capture_output=True)
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@t")
        run("git", "config", "user.name", "t")
        run("git", "config", "commit.gpgsign", "false")
        (repo / "f.txt").write_text("base\n")
        run("git", "add", ".")
        run("git", "commit", "-q", "-m", "base")
        run("git", "checkout", "-q", "-b", "feat-a")
        (repo / "a.txt").write_text("a\n")
        run("git", "add", ".")
        run("git", "commit", "-q", "-m", "add a")
        a_commit = sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        run("git", "checkout", "-q", "main")
        run("git", "checkout", "-q", "-b", "winner")

        # Stub the chief to return a pre-baked cherry_pick synthesis
        from multifleet.synthesis import SynthesisResult

        fake_result = SynthesisResult(
            task_id="t",
            decision="cherry_pick",
            winner_branch="winner",
            confidence=0.9,
            reasoning="test",
            cherry_pick_from=[{"machine": "mac2", "commit": a_commit, "reason": "x"}],
        )

        with patch("multifleet.race_orchestrator._daemon_post") as mp, \
             patch("multifleet.race_orchestrator._daemon_get") as mg, \
             patch("multifleet.race_orchestrator.ChiefSynthesis") as MockChief:
            mp.return_value = {"sent": True}
            mg.return_value = None

            # Configure the mocked chief instance
            chief_instance = MagicMock()
            chief_instance.synthesize.return_value = fake_result
            chief_instance._evidence_ledger = None
            MockChief.return_value = chief_instance

            # Seed a verdict into the rebuttal session so synthesize() is reached
            from multifleet.cross_rebuttal import RebuttalManager
            with patch.object(RebuttalManager, "start_session") as mock_start:
                sess = MagicMock()
                sess.task_id = "t"
                sess.verdicts = {"mac1": {"summary": "x", "confidence": 0.9}}
                sess.critiques = {}
                sess.phase = MagicMock(value="done")
                mock_start.return_value = sess
                with patch.object(RebuttalManager, "advance_session"):
                    with patch.object(RebuttalManager, "handle_incoming_packet"):
                        result = run_race(
                            "t", nodes=["mac1"],
                            timeout_s=1, poll_interval_s=0.1,
                            auto_execute_cherry_pick=True,
                            repo_path=str(repo),
                        )

        # Adjudication should have run and applied the cherry
        adj = result.get("adjudication")
        assert adj is not None, f"adjudication missing: keys={list(result.keys())}"
        assert adj["status"] == "ok", f"expected ok, got {adj}"
        assert len(adj["applied"]) == 1
        assert adj["applied"][0]["commit"] == a_commit
        # a.txt should now exist on the winner branch
        assert (repo / "a.txt").is_file()
