"""Tests for Ghost Mirror, Ghost Anticipate, and Ghost Corroborate."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.ghost_mirror import GhostMirror, MirrorResult, run_mirror
from multifleet.ghost_anticipate import (
    GhostAnticipate, AnticipateResult, Prediction, run_anticipate,
)
from multifleet.ghost_corroborate import (
    GhostCorroborate, CorroborateResult, CorroborationItem, run_corroborate,
)


# ---------------------------------------------------------------------------
# Ghost Mirror tests
# ---------------------------------------------------------------------------

class TestGhostMirror:

    def test_mirror_result_summary_no_drift(self):
        r = MirrorResult()
        assert r.summary() == "no drift"

    def test_mirror_result_summary_with_drift(self):
        r = MirrorResult(
            drift_items=[{"doc": "CLAUDE.md", "missing_ref": "foo/bar.py"}],
            fix_suggestions=["Update CLAUDE.md: remove reference to 'foo/bar.py'"],
        )
        assert "1 drift items" in r.summary()
        assert "1 suggestions" in r.summary()

    def test_mirror_result_to_dict(self):
        r = MirrorResult(docs_checked=2, duration_ms=12.345, timestamp="2026-01-01T00:00:00Z")
        d = r.to_dict()
        assert d["docs_checked"] == 2
        assert d["duration_ms"] == 12.3

    def test_scan_runs_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = GhostMirror(repo_root=tmp, node_id="test")
            result = mirror.scan()
            assert isinstance(result, MirrorResult)
            assert result.duration_ms >= 0

    @patch("multifleet.ghost_mirror.GhostMirror._run_probe")
    def test_scan_with_drift_findings(self, mock_probe):
        mock_probe.return_value = {
            "status": "warning",
            "findings": [{
                "title": "CLAUDE.md references 2 non-existent path(s)",
                "evidence": ["Missing: memory/gone.py", "Missing: scripts/deleted.sh"],
                "file_paths": ["CLAUDE.md"],
            }],
        }
        mirror = GhostMirror(repo_root=".", node_id="test")
        result = mirror.scan()
        assert len(result.drift_items) == 2
        assert len(result.fix_suggestions) == 2
        assert "gone.py" in result.fix_suggestions[0]

    def test_run_mirror_convenience(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = run_mirror(repo_root=tmp)
            assert "docs_checked" in d
            assert "drift_items" in d


# ---------------------------------------------------------------------------
# Ghost Anticipate tests
# ---------------------------------------------------------------------------

class TestGhostAnticipate:

    def test_prediction_to_dict(self):
        p = Prediction(kind="file_edit", target="foo.py", confidence=0.75, reason="recent")
        d = p.to_dict()
        assert d["kind"] == "file_edit"
        assert d["confidence"] == 0.75

    def test_anticipate_result_summary_empty(self):
        r = AnticipateResult()
        assert r.summary() == "no predictions"

    def test_anticipate_result_summary_with_predictions(self):
        r = AnticipateResult(predictions=[
            Prediction("file_edit", "a.py", 0.7),
            Prediction("file_edit", "b.py", 0.6),
            Prediction("probe_run", "mirror_drift", 0.8),
        ])
        s = r.summary()
        assert "file_edit" in s
        assert "probe_run" in s

    def test_anticipate_result_to_dict(self):
        r = AnticipateResult(
            predictions=[Prediction("file_edit", "a.py", 0.7)],
            backlog_items_added=1,
            duration_ms=5.678,
            timestamp="2026-01-01T00:00:00Z",
        )
        d = r.to_dict()
        assert d["backlog_items_added"] == 1
        assert len(d["predictions"]) == 1

    @patch("multifleet.ghost_anticipate.subprocess.run")
    def test_anticipate_with_recent_files(self, mock_run):
        # Mock git log, git branch, git diff
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc1234 Fix thing\nfoo.py\nbar.py\n"),
            MagicMock(returncode=0, stdout="feature/ghost-layer\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            # Create the files so they pass existence check
            (Path(tmp) / "foo.py").write_text("x")
            (Path(tmp) / "bar.py").write_text("x")
            ghost = GhostAnticipate(repo_root=tmp, node_id="test")
            result = ghost.anticipate()
            assert isinstance(result, AnticipateResult)
            # Should predict file edits + memory query from branch name
            file_preds = [p for p in result.predictions if p.kind == "file_edit"]
            mem_preds = [p for p in result.predictions if p.kind == "memory_query"]
            assert len(file_preds) >= 1
            assert len(mem_preds) >= 1
            assert "ghost layer" in mem_preds[0].target

    def test_predict_probes_from_docs(self):
        ghost = GhostAnticipate(repo_root=".", node_id="test")
        preds = ghost._predict_probes(["README.md", "CLAUDE.md"])
        assert any(p.target == "mirror_drift" for p in preds)

    def test_predict_probes_from_deps(self):
        ghost = GhostAnticipate(repo_root=".", node_id="test")
        preds = ghost._predict_probes(["requirements.txt"])
        assert any(p.target == "dependency_drift" for p in preds)

    def test_run_anticipate_convenience(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = run_anticipate(repo_root=tmp)
            assert "predictions" in d
            assert "backlog_items_added" in d


# ---------------------------------------------------------------------------
# Ghost Corroborate tests
# ---------------------------------------------------------------------------

class TestGhostCorroborate:

    def test_corroboration_item_to_dict(self):
        item = CorroborationItem(
            finding_title="drift detected",
            original_confidence=0.5,
            adjusted_confidence=0.6,
            prior_occurrences=2,
            verdict="unchanged",
        )
        d = item.to_dict()
        assert d["verdict"] == "unchanged"
        assert d["adjusted_confidence"] == 0.6

    def test_corroborate_result_summary_empty(self):
        r = CorroborateResult()
        assert r.summary() == "no findings to corroborate"

    def test_corroborate_result_summary_with_items(self):
        r = CorroborateResult(strengthened=2, novel=1)
        s = r.summary()
        assert "2 strengthened" in s
        assert "1 novel" in s

    def test_novel_finding_gets_penalty(self):
        ghost = GhostCorroborate(node_id="test")
        # Patch history to return nothing
        with patch.object(ghost, "_load_history", return_value=[]):
            result = ghost.corroborate([
                {"title": "Brand new finding never seen", "confidence": 0.7},
            ])
            assert result.novel == 1
            item = result.items[0]
            assert item.verdict == "novel"
            assert item.adjusted_confidence < item.original_confidence

    def test_recurring_finding_gets_boost(self):
        # Create fake history with matching entries
        fake_history = [
            {"payload": {"title": "Mirror drift path references broken"}},
            {"payload": {"title": "Mirror drift path references broken"}},
            {"payload": {"title": "Mirror drift path references broken"}},
        ]
        ghost = GhostCorroborate(node_id="test")
        with patch.object(ghost, "_load_history", return_value=fake_history):
            result = ghost.corroborate([
                {"title": "Mirror drift path references broken", "confidence": 0.5},
            ])
            assert result.strengthened == 1
            item = result.items[0]
            assert item.verdict == "strengthened"
            assert item.adjusted_confidence > item.original_confidence

    def test_confidence_ceiling(self):
        ghost = GhostCorroborate(node_id="test")
        item = ghost._assess("recurring finding", 0.92, 10)
        assert item.adjusted_confidence <= GhostCorroborate.CONFIDENCE_CEILING

    def test_confidence_floor(self):
        ghost = GhostCorroborate(node_id="test")
        item = ghost._assess("novel finding", 0.12, 0)
        assert item.adjusted_confidence >= GhostCorroborate.CONFIDENCE_FLOOR

    def test_run_corroborate_convenience(self):
        d = run_corroborate(findings=[], node_id="test")
        assert "items" in d
        assert "strengthened" in d
