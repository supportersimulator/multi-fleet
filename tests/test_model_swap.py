"""Tests for ModelSwapCheckpoint — invariance across model fallback events."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.model_swap import ModelSwapCheckpoint, SwapCheckpointData


class TestSwapCheckpointData:
    """Dataclass round-trip and defaults."""

    def test_to_dict_roundtrip(self):
        data = SwapCheckpointData(
            checkpoint_id="swap-20260415-120000",
            timestamp="2026-04-15T12:00:00Z",
            reason="model-swap",
            source_model="claude-opus-4",
            target_model="deepseek-v3",
            branch="main",
            last_5_commits=["abc1234 fix auth", "def5678 add tests"],
            dirty_files=["M src/app.py"],
            active_task="build checkpoint hook",
            node_id="mac1",
        )
        d = data.to_dict()
        restored = SwapCheckpointData.from_dict(d)
        assert restored.checkpoint_id == data.checkpoint_id
        assert restored.source_model == "claude-opus-4"
        assert restored.target_model == "deepseek-v3"
        assert restored.last_5_commits == data.last_5_commits
        assert restored.active_task == "build checkpoint hook"

    def test_from_dict_missing_keys(self):
        """Gracefully handles missing keys with defaults."""
        data = SwapCheckpointData.from_dict({"checkpoint_id": "test"})
        assert data.checkpoint_id == "test"
        assert data.branch == ""
        assert data.last_5_commits == []
        assert data.dirty_files == []
        assert data.pending_critical_findings == 0

    def test_from_dict_empty(self):
        data = SwapCheckpointData.from_dict({})
        assert data.checkpoint_id == ""
        assert data.fleet_inbox_count == 0


class TestModelSwapCheckpointCapture:
    """Capture creates a valid checkpoint file."""

    def test_capture_creates_file(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        # Create minimal git repo
        (tmp_path / "CLAUDE.md").write_text("## Atlas Identity\nNavigator.")
        with patch.object(mc, "_git") as mock_git:
            mock_git.side_effect = lambda *args: {
                ("branch", "--show-current"): "main",
                ("log", "--oneline", "-5"): "abc1234 first\ndef5678 second",
                ("status", "--porcelain"): " M file.py\n?? new.py",
                ("diff", "--cached", "--name-only"): "staged.py",
            }.get(args, "")

            data = mc.capture(reason="test-swap", source_model="opus", target_model="deepseek")

        assert cp_path.exists()
        raw = json.loads(cp_path.read_text())
        assert raw["reason"] == "test-swap"
        assert raw["source_model"] == "opus"
        assert raw["target_model"] == "deepseek"
        assert raw["branch"] == "main"
        assert len(raw["last_5_commits"]) == 2
        assert len(raw["dirty_files"]) == 2
        assert raw["staged_files"] == ["staged.py"]

    def test_capture_reads_claude_md(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        (tmp_path / "CLAUDE.md").write_text("## Heading\nThis is the summary line.\nMore content.")
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        with patch.object(mc, "_git", return_value=""):
            data = mc.capture()

        assert data.claude_md_hash != ""
        assert "summary line" in data.claude_md_summary

    def test_capture_reads_fleet_inbox(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        inbox = tmp_path / "fleet" / "testnode"
        inbox.mkdir(parents=True)
        (inbox / "msg1.md").write_text("subject: build auth module\nbody: details here")

        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        mc._node_id = "testnode"
        with patch.object(mc, "_git", return_value=""):
            data = mc.capture()

        assert data.active_task == "build auth module"
        assert data.fleet_inbox_count == 1

    def test_capture_survives_missing_everything(self, tmp_path):
        """No git, no fleet, no CLAUDE.md — still produces a checkpoint."""
        cp_path = tmp_path / "checkpoint.json"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path / "nonexistent"),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "nofleet"),
        )
        with patch.object(mc, "_git", return_value=""):
            data = mc.capture(reason="resilience-test")

        assert data.reason == "resilience-test"
        assert data.branch == ""
        assert data.active_task == ""
        assert cp_path.exists()


class TestModelSwapCheckpointRestore:
    """Restore reads checkpoint back from disk."""

    def test_restore_round_trip(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        with patch.object(mc, "_git", return_value=""):
            original = mc.capture(reason="roundtrip", source_model="opus")

        restored = mc.restore()
        assert restored is not None
        assert restored.checkpoint_id == original.checkpoint_id
        assert restored.source_model == "opus"

    def test_restore_missing_file(self, tmp_path):
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(tmp_path / "missing.json"),
        )
        assert mc.restore() is None

    def test_restore_corrupt_file(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text("not json{{{")
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
        )
        assert mc.restore() is None


class TestModelSwapDiff:
    """diff_from_checkpoint detects changes since last snapshot."""

    def test_diff_detects_branch_change(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )

        # Capture on main
        with patch.object(mc, "_git") as mock_git:
            mock_git.side_effect = lambda *args: {
                ("branch", "--show-current"): "main",
            }.get(args, "")
            original = mc.capture(reason="before")

        # Now diff — pretend we switched to feature branch
        with patch.object(mc, "_git") as mock_git:
            mock_git.side_effect = lambda *args: {
                ("branch", "--show-current"): "feature/auth",
            }.get(args, "")
            diff = mc.diff_from_checkpoint(original)

        assert any("branch:" in c for c in diff["changes"])

    def test_diff_detects_new_commits(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )

        # Capture with 2 commits
        with patch.object(mc, "_git") as mock_git:
            mock_git.side_effect = lambda *args: {
                ("log", "--oneline", "-5"): "abc first\ndef second",
            }.get(args, "")
            original = mc.capture(reason="before")

        # Diff with new commit added
        with patch.object(mc, "_git") as mock_git:
            mock_git.side_effect = lambda *args: {
                ("log", "--oneline", "-5"): "111 new commit\nabc first\ndef second",
            }.get(args, "")
            diff = mc.diff_from_checkpoint(original)

        assert any("new_commits" in c for c in diff["changes"])
        assert "111 new commit" in diff["new_commits"]

    def test_diff_no_checkpoint_returns_error(self, tmp_path):
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(tmp_path / "missing.json"),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        diff = mc.diff_from_checkpoint()
        assert "error" in diff


class TestArchive:
    """Checkpoint history is maintained."""

    def test_archive_creates_history(self, tmp_path):
        cp_path = tmp_path / "checkpoint.json"
        hist_dir = tmp_path / "history"
        mc = ModelSwapCheckpoint(
            repo_root=str(tmp_path),
            checkpoint_path=str(cp_path),
            fleet_messages_dir=str(tmp_path / "fleet"),
        )
        # Patch HISTORY_DIR module-level
        with patch("multifleet.model_swap.HISTORY_DIR", hist_dir):
            with patch.object(mc, "_git", return_value=""):
                mc.capture(reason="archive-test")

        assert hist_dir.exists()
        assert len(list(hist_dir.glob("*.json"))) == 1
