"""Tests for multifleet.race — Competitive Branch Racing (simple module).

Covers: RaceResult, FleetRace init/start/collect_verdict, Superset bridge integration, CLI.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.race import FleetRace, RaceResult, fleet_race_cli


class TestRaceResultCreation:
    """RaceResult dataclass construction and defaults."""

    def test_defaults(self):
        r = RaceResult(race_id="abc-123", prompt="Fix auth", nodes=["mac1"])
        assert r.decision == "pending"
        assert r.winner_branch is None
        assert r.verdicts == []
        assert r.critiques == []
        assert r.error is None

    def test_summary_pending(self):
        r = RaceResult(race_id="abc-12345", prompt="Fix auth", nodes=["mac1"])
        s = r.summary()
        assert "abc-1234" in s
        assert "pending" in s

    def test_summary_with_error(self):
        r = RaceResult(race_id="abc-12345", prompt="Fix", nodes=[], error="timeout")
        s = r.summary()
        assert "FAILED" in s
        assert "timeout" in s

    def test_summary_with_verdicts(self):
        r = RaceResult(
            race_id="abc-12345", prompt="Fix", nodes=["mac1", "mac2"],
            decision="merged", winner_branch="race/abc/mac1", elapsed_s=12.5,
            verdicts=[
                {"from": "mac1", "verdict": "pass"},
                {"from": "mac2", "verdict": "pass"},
            ],
        )
        s = r.summary()
        assert "merged" in s
        assert "mac1=pass" in s
        assert "12.5s" in s


class TestFleetRaceInit:
    """FleetRace initialization."""

    def test_default_url(self):
        race = FleetRace()
        assert race.daemon_url == "http://127.0.0.1:8855"

    def test_custom_url(self):
        race = FleetRace(daemon_url="http://localhost:9000")
        assert race.daemon_url == "http://localhost:9000"

    def test_superset_lazy_not_loaded(self):
        race = FleetRace()
        assert race._superset is None


class TestFleetRaceStart:
    """FleetRace.start() dispatches tasks to nodes."""

    def _mock_urlopen(self, response_data=None):
        """Create a mock urlopen that returns given JSON."""
        data = response_data or {"sent": True}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_start_returns_result(self):
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            race = FleetRace()
            result = race.start("Fix auth", nodes=["mac1", "mac2"])
            assert isinstance(result, RaceResult)
            assert result.prompt == "Fix auth"
            assert result.nodes == ["mac1", "mac2"]

    def test_start_assigns_race_id(self):
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            race = FleetRace()
            result = race.start("Task", nodes=["mac1"])
            assert len(result.race_id) == 36  # UUID format

    def test_start_dispatches_to_each_node(self):
        mock_resp = self._mock_urlopen()
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            race = FleetRace()
            race.start("Task", nodes=["mac1", "mac2", "mac3"])
            # At least one HTTP call per node for dispatch
            assert mock_open.call_count >= 3

    def test_start_records_elapsed(self):
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen()):
            race = FleetRace()
            result = race.start("Task", nodes=["mac1"])
            assert result.elapsed_s > 0

    def test_start_handles_daemon_error(self):
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("refused")):
            race = FleetRace()
            # Should not raise — logs warning and continues
            result = race.start("Task", nodes=["mac1", "mac2"])
            assert isinstance(result, RaceResult)


class TestCollectVerdict:
    """FleetRace.collect_verdict stores incoming verdicts."""

    def test_collect_verdict_does_not_raise(self):
        race = FleetRace()
        # Should accept verdict without error
        race.collect_verdict("race-abc", "mac1", {"verdict": "pass", "summary": "LGTM"})

    def test_get_status(self):
        race = FleetRace()
        status = race.get_status("race-xyz")
        assert status["race_id"] == "race-xyz"
        assert status["status"] == "in_progress"


class TestSupersetIntegration:
    """Superset bridge lazy-loading and workspace creation."""

    def test_superset_import_error_returns_none(self):
        race = FleetRace()
        with patch.dict("sys.modules", {"multifleet.superset_bridge": None}):
            with patch("builtins.__import__", side_effect=ImportError("no module")):
                result = race._get_superset()
                assert result is None

    def test_superset_no_api_key(self):
        mock_bridge = MagicMock()
        mock_bridge._api_key = None
        mock_module = MagicMock()
        mock_module.SupersetBridge.return_value = mock_bridge

        race = FleetRace()
        with patch.dict("sys.modules", {"multifleet.superset_bridge": mock_module}):
            result = race._get_superset()
            assert result is None

    def test_superset_available(self):
        mock_bridge = MagicMock()
        mock_bridge._api_key = "tk_test"
        mock_module = MagicMock()
        mock_module.SupersetBridge.return_value = mock_bridge

        race = FleetRace()
        with patch.dict("sys.modules", {"multifleet.superset_bridge": mock_module}):
            result = race._get_superset()
            assert result is mock_bridge


class TestCli:
    """CLI entry point."""

    def test_fleet_race_cli_returns_summary(self):
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"sent": true}'
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_open.return_value = mock_resp

            output = fleet_race_cli("Fix the bug", nodes="mac1,mac2")
            assert "Race" in output
            assert "pending" in output

    def test_cli_parses_node_list(self):
        with patch.object(FleetRace, "start") as mock_start:
            mock_start.return_value = RaceResult(
                race_id="test", prompt="Task", nodes=["mac1", "mac3"],
            )
            fleet_race_cli("Task", nodes="mac1, mac3")
            called_nodes = mock_start.call_args[1]["nodes"]
            assert called_nodes == ["mac1", "mac3"]
