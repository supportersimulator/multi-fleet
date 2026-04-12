"""Tests for surgeon verdict broadcasting — verifies verdict packaging,
fleet broadcast emission, and graceful degradation when channels fail."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure multi-fleet package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestBroadcastSurgeonVerdict:
    """Tests for the broadcast_surgeon_verdict() function."""

    def _make_verdict(self, **overrides):
        base = {
            "task_id": "fix-auth-bypass",
            "agreed": True,
            "confidence": 0.85,
            "dissent_count": 0,
            "surgeons_consulted": ["atlas", "cardiologist", "neurologist"],
            "node_id": "mac1",
        }
        base.update(overrides)
        return base

    def test_p7_writes_fleet_message(self, tmp_path):
        """Verdict is written to .fleet-messages/all/ as markdown."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict

        verdict = self._make_verdict()

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError):
            results = broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        # P7 should succeed
        assert results["p7"] is True

        fleet_dir = tmp_path / ".fleet-messages" / "all"
        assert fleet_dir.exists()

        files = list(fleet_dir.glob("*surgeon-verdict*.md"))
        assert len(files) == 1

        content = files[0].read_text()
        assert "CONSENSUS" in content
        assert "fix-auth-bypass" in content
        assert "85.0%" in content
        assert "from: mac1" in content
        assert "to: all" in content
        assert "type: surgeon_verdict" in content

    def test_p7_dissent_verdict(self, tmp_path):
        """Dissent verdicts are labeled correctly."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict

        verdict = self._make_verdict(agreed=False, confidence=0.4, dissent_count=2)

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError):
            results = broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        assert results["p7"] is True
        files = list((tmp_path / ".fleet-messages" / "all").glob("*.md"))
        content = files[0].read_text()
        assert "DISSENT" in content
        assert "40.0%" in content

    def test_bridge_route_called(self, tmp_path):
        """Product bridge surgeons_to_fleet is called when available."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict

        mock_bridge = MagicMock()
        mock_bridge.surgeons_to_fleet.return_value = {"delivered_to": "all"}

        with patch("multifleet.product_bridge.get_bridge", return_value=mock_bridge):
            verdict = self._make_verdict()
            results = broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        assert results["bridge"] is True
        mock_bridge.surgeons_to_fleet.assert_called_once()
        call_arg = mock_bridge.surgeons_to_fleet.call_args[0][0]
        assert call_arg["task_id"] == "fix-auth-bypass"
        assert call_arg["target_nodes"] == "all"

    def test_eventbus_emission(self, tmp_path):
        """Verdict is emitted on the EventBus as surgeon.verdict.broadcast."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict
        from multifleet.event_bus import IDEEventBus

        mock_bus = MagicMock()

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError), \
             patch.object(IDEEventBus, "get", return_value=mock_bus):
            verdict = self._make_verdict()
            results = broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        assert results["eventbus"] is True
        mock_bus.emit.assert_called_once()
        args, kwargs = mock_bus.emit.call_args
        assert args[0] == "surgeon.verdict.broadcast"
        assert kwargs["payload"]["task_id"] == "fix-auth-bypass"

    def test_graceful_degradation_all_fail(self, tmp_path):
        """When all channels fail, returns all False without raising."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict
        from multifleet.event_bus import IDEEventBus

        # Make P7 fail by providing a path that can't be written
        bad_root = Path("/nonexistent/impossible/path")

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError), \
             patch.object(IDEEventBus, "get", side_effect=RuntimeError("bus down")):
            verdict = self._make_verdict()
            results = broadcast_surgeon_verdict(verdict, superrepo_root=bad_root)

        assert results["bridge"] is False
        assert results["p7"] is False
        assert results["eventbus"] is False

    def test_partial_channel_failure(self, tmp_path):
        """Some channels can fail while others succeed."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict
        from multifleet.event_bus import IDEEventBus

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError), \
             patch.object(IDEEventBus, "get", side_effect=RuntimeError("bus down")):
            verdict = self._make_verdict()
            results = broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        # Bridge and EventBus fail, but P7 should succeed
        assert results["bridge"] is False
        assert results["p7"] is True
        assert results["eventbus"] is False

    def test_filename_sanitization(self, tmp_path):
        """Task IDs with special characters are sanitized in filenames."""
        from multifleet.adapters.surgeon_events import broadcast_surgeon_verdict

        verdict = self._make_verdict(task_id="fix/auth bypass@v2!")

        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError):
            broadcast_surgeon_verdict(verdict, superrepo_root=tmp_path)

        files = list((tmp_path / ".fleet-messages" / "all").glob("*.md"))
        assert len(files) == 1
        # No slashes, spaces, or special chars in filename
        name = files[0].name
        assert "/" not in name
        assert " " not in name
        assert "@" not in name


class TestSurgeonEventsAdapterBroadcast:
    """Test that on_consensus triggers broadcast."""

    def test_on_consensus_triggers_broadcast(self):
        """on_consensus calls broadcast_surgeon_verdict."""
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter

        adapter = SurgeonEventsAdapter(node_id="mac1")

        with patch("multifleet.adapters.surgeon_events.emit"), \
             patch("multifleet.adapters.surgeon_events.broadcast_surgeon_verdict") as mock_broadcast:
            mock_broadcast.return_value = {"bridge": True, "p7": True, "eventbus": True}
            adapter.on_consensus(
                task_id="test-task",
                agreed=True,
                confidence=0.9,
                dissent_count=0,
                surgeons_consulted=["atlas", "cardiologist"],
            )

        mock_broadcast.assert_called_once()
        call_arg = mock_broadcast.call_args[0][0]
        assert call_arg["task_id"] == "test-task"
        assert call_arg["agreed"] is True
        assert call_arg["confidence"] == 0.9
        assert call_arg["node_id"] == "mac1"

    def test_on_consensus_broadcast_failure_nonfatal(self):
        """Broadcast failure does not prevent consensus event emission."""
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter

        adapter = SurgeonEventsAdapter(node_id="mac1")

        with patch("multifleet.adapters.surgeon_events.emit") as mock_emit, \
             patch("multifleet.adapters.surgeon_events.broadcast_surgeon_verdict",
                   side_effect=RuntimeError("channel down")):
            event = adapter.on_consensus(
                task_id="test-task",
                agreed=True,
                confidence=0.85,
            )

        # Event was still emitted despite broadcast failure
        mock_emit.assert_called_once()
        assert event is not None
        # broadcast_errors counter incremented
        assert adapter.stats.get("broadcast_errors", 0) == 1

    def test_on_consensus_tracks_broadcast_stats(self):
        """Successful broadcasts increment the broadcast_events counter."""
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter

        adapter = SurgeonEventsAdapter(node_id="mac1")

        with patch("multifleet.adapters.surgeon_events.emit"), \
             patch("multifleet.adapters.surgeon_events.broadcast_surgeon_verdict",
                   return_value={"bridge": True, "p7": True, "eventbus": True}):
            adapter.on_consensus(task_id="t1", agreed=True, confidence=0.9)
            adapter.on_consensus(task_id="t2", agreed=False, confidence=0.3)

        assert adapter.stats.get("broadcast_events", 0) == 2
