"""Tests for BranchConvergence — fleet branch visualization."""

import time
from unittest.mock import MagicMock, patch

import pytest
import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.branch_convergence import BranchConvergence, BranchInfo


# ── Fixtures ──

def _make_branch(name="fleet/feature-x", machine="mac1", ahead=3, behind=1,
                 files=None, ts=None):
    return BranchInfo(
        branch=name, machine_id=machine,
        commits_ahead=ahead, commits_behind=behind,
        files_changed=files or ["src/app.py"],
        last_commit_ts=ts or time.time(),
    )


@pytest.fixture
def conv(tmp_path):
    """BranchConvergence with mocked git."""
    c = BranchConvergence(str(tmp_path))
    return c


@pytest.fixture
def branches():
    """Sample branch set with known conflicts."""
    now = time.time()
    return [
        _make_branch("fleet/auth", "mac1", 5, 2, ["src/auth.py", "src/db.py"], now - 3600),
        _make_branch("mac2/dashboard", "mac2", 3, 0, ["src/dashboard.py", "src/db.py"], now - 1800),
        _make_branch("race/perf-opt", "mac3", 10, 5, ["src/perf.py"], now - 600),
    ]


# ── BranchInfo ──

class TestBranchInfo:
    def test_dataclass_fields(self):
        b = BranchInfo(branch="x", machine_id="mac1", commits_ahead=1,
                       commits_behind=0)
        assert b.files_changed == []
        assert b.last_commit_ts == 0.0

    def test_with_files(self):
        b = _make_branch(files=["a.py", "b.py"])
        assert len(b.files_changed) == 2


# ── conflict_matrix ──

class TestConflictMatrix:
    def test_detects_overlapping_files(self, conv, branches):
        matrix = conv.conflict_matrix(branches)
        # auth and dashboard both touch src/db.py
        assert ("fleet/auth", "mac2/dashboard") in matrix
        assert "src/db.py" in matrix[("fleet/auth", "mac2/dashboard")]

    def test_no_conflict_when_disjoint(self, conv):
        bs = [
            _make_branch("a", files=["x.py"]),
            _make_branch("b", files=["y.py"]),
        ]
        matrix = conv.conflict_matrix(bs)
        assert len(matrix) == 0

    def test_multiple_overlaps(self, conv):
        bs = [
            _make_branch("a", files=["x.py", "y.py", "z.py"]),
            _make_branch("b", files=["y.py", "z.py"]),
        ]
        matrix = conv.conflict_matrix(bs)
        assert len(matrix[("a", "b")]) == 2

    def test_empty_branches(self, conv):
        assert conv.conflict_matrix([]) == {}


# ── merge_recommendations ──

class TestMergeRecommendations:
    def test_returns_ranked_list(self, conv, branches):
        recs = conv.merge_recommendations(branches)
        assert len(recs) == 3
        assert recs[0]["rank"] == 1
        assert recs[2]["rank"] == 3

    def test_oldest_first(self, conv):
        now = time.time()
        bs = [
            _make_branch("new", ts=now, files=["a.py"]),
            _make_branch("old", ts=now - 7200, files=["b.py"]),
        ]
        recs = conv.merge_recommendations(bs)
        assert recs[0]["branch"] == "old"

    def test_strategy_fast_forward(self, conv):
        bs = [_make_branch("clean", ahead=2, behind=0, files=["a.py"])]
        recs = conv.merge_recommendations(bs)
        assert recs[0]["strategy"] == "fast-forward"

    def test_strategy_rebase_when_behind(self, conv):
        bs = [_make_branch("behind", ahead=2, behind=3, files=["a.py"])]
        recs = conv.merge_recommendations(bs)
        assert recs[0]["strategy"] == "rebase"

    def test_empty_returns_empty(self, conv):
        assert conv.merge_recommendations([]) == []


# ── render_ascii ──

class TestRenderAscii:
    def test_includes_branch_names(self, conv, branches):
        out = conv.render_ascii(branches)
        assert "fleet/auth" in out
        assert "mac2/dashboard" in out
        assert "race/perf-opt" in out

    def test_shows_conflict_section(self, conv, branches):
        out = conv.render_ascii(branches)
        assert "Conflicts:" in out
        assert "src/db.py" in out

    def test_empty_message(self, conv):
        out = conv.render_ascii([])
        assert "No fleet branches" in out


# ── render_html ──

class TestRenderHtml:
    def test_contains_table(self, conv, branches):
        html = conv.render_html(branches)
        assert "<table>" in html
        assert "fleet/auth" in html

    def test_conflict_table(self, conv, branches):
        html = conv.render_html(branches)
        assert "Conflict Matrix" in html

    def test_empty_html(self, conv):
        html = conv.render_html([])
        assert "No fleet branches" in html

    def test_html_escaping(self, conv):
        bs = [_make_branch("fleet/<script>", files=["a.py"])]
        html = conv.render_html(bs)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── scan_branches (mocked git) ──

class TestScanBranches:
    def test_scan_filters_fleet_branches(self, conv):
        def mock_git(*args):
            cmd = " ".join(args)
            if "branch -r" in cmd:
                return "origin/main\norigin/fleet/fix\norigin/feature/unrelated\norigin/mac2/work"
            if "rev-list --count" in cmd:
                return "3"
            if "diff --name-only" in cmd:
                return "file.py"
            if "log -1 --format=%ct" in cmd:
                return str(int(time.time()))
            if "log -1 --format=%an" in cmd:
                return "atlas"
            return ""

        conv._git = MagicMock(side_effect=mock_git)
        branches = conv.scan_branches()
        names = [b.branch for b in branches]
        assert "fleet/fix" in names
        assert "mac2/work" in names
        assert "feature/unrelated" not in names

    def test_scan_skips_zero_divergence(self, conv):
        def mock_git(*args):
            cmd = " ".join(args)
            if "branch -r" in cmd:
                return "origin/fleet/synced"
            if "rev-list --count" in cmd:
                return "0"
            return ""

        conv._git = MagicMock(side_effect=mock_git)
        assert conv.scan_branches() == []


# ── to_dict ──

class TestToDict:
    def test_structure(self, conv):
        conv.scan_branches = MagicMock(return_value=[
            _make_branch("fleet/x", files=["a.py"]),
        ])
        d = conv.to_dict()
        assert "branches" in d
        assert "conflicts" in d
        assert "recommendations" in d
        assert "ascii" in d
        assert "timestamp" in d
        assert len(d["branches"]) == 1


# ── guess_machine ──

class TestGuessMachine:
    def test_prefix_match(self, conv):
        assert conv._guess_machine("mac1/feature") == "mac1"
        assert conv._guess_machine("mac3/fix") == "mac3"

    def test_unknown_fallback(self, conv):
        conv._git = MagicMock(side_effect=RuntimeError("no git"))
        assert conv._guess_machine("fleet/mystery") == "unknown"
