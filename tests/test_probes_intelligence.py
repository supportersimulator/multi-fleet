"""Tests for intelligence probes: repeated_failure_cluster, worktree_divergence, injection_prioritizer.

25 tests covering: cluster detection, git divergence, injection scoring,
edge cases, error handling, and integration with probe engine.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.probe_engine import ProbeCategory, ProbeResult


# ── Repeated Failure Cluster ──

class TestRepeatedFailureCluster:

    def _write_audit(self, audit_dir: Path, filename: str, events: list[dict]):
        audit_dir.mkdir(parents=True, exist_ok=True)
        with open(audit_dir / filename, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

    def test_detects_repeated_errors(self, tmp_path):
        """3+ errors with same tool::target pattern → finding."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [
            {"tool": "ssh", "target": "mac2", "status": "error", "timestamp": f"2026-04-0{i}"}
            for i in range(1, 5)
        ]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert result.status == "warning"
        assert len(result.findings) == 1
        assert "ssh::mac2" in result.findings[0].title
        assert "4x" in result.findings[0].title

    def test_ignores_below_threshold(self, tmp_path):
        """2 errors (below threshold of 3) → ok."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [
            {"tool": "nats", "target": "mac1", "status": "fail", "timestamp": "2026-04-01"},
            {"tool": "nats", "target": "mac1", "status": "fail", "timestamp": "2026-04-02"},
        ]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert result.status == "ok"
        assert len(result.findings) == 0

    def test_multiple_clusters(self, tmp_path):
        """Two different failure patterns, both above threshold."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [
            {"tool": "ssh", "target": "mac2", "status": "error", "timestamp": "t1"},
            {"tool": "ssh", "target": "mac2", "status": "error", "timestamp": "t2"},
            {"tool": "ssh", "target": "mac2", "status": "error", "timestamp": "t3"},
            {"tool": "http", "target": "mac3", "status": "fail", "timestamp": "t1"},
            {"tool": "http", "target": "mac3", "status": "fail", "timestamp": "t2"},
            {"tool": "http", "target": "mac3", "status": "fail", "timestamp": "t3"},
        ]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert len(result.findings) == 2

    def test_skipped_when_no_audit_dir(self, tmp_path):
        """No audit directory → skipped."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        result = probe_repeated_failure_cluster({"audit_dir": str(tmp_path / "nonexistent")})
        assert result.status == "skipped"

    def test_ok_when_empty_audit(self, tmp_path):
        """Audit dir exists but no error events → ok."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [{"tool": "ssh", "target": "mac1", "status": "ok", "timestamp": "t1"}]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert result.status == "ok"

    def test_severity_escalation(self, tmp_path):
        """10+ failures → critical severity."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [
            {"tool": "nats", "target": "mac3", "status": "error", "timestamp": f"t{i}"}
            for i in range(12)
        ]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert result.severity == "critical"

    def test_handles_malformed_jsonl(self, tmp_path):
        """Malformed lines are skipped, valid ones still processed."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        audit.mkdir(parents=True)
        with open(audit / "audit-2026-04.jsonl", "w") as f:
            f.write("not json\n")
            for i in range(3):
                f.write(json.dumps({"tool": "x", "target": "y", "status": "error", "timestamp": f"t{i}"}) + "\n")
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        assert result.status == "warning"

    def test_evidence_has_first_last_seen(self, tmp_path):
        """Evidence includes first and last seen timestamps."""
        from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
        audit = tmp_path / ".fleet-nerve"
        events = [
            {"tool": "ssh", "target": "mac2", "status": "error", "timestamp": f"2026-04-0{i}"}
            for i in range(1, 4)
        ]
        self._write_audit(audit, "audit-2026-04.jsonl", events)
        result = probe_repeated_failure_cluster({"audit_dir": str(audit)})
        evidence_text = " ".join(result.findings[0].evidence)
        assert "First seen" in evidence_text
        assert "Last seen" in evidence_text


# ── Worktree Divergence ──

class TestWorktreeDivergence:

    def _mock_git(self, responses: dict):
        """Create a _git mock that returns based on first arg keyword."""
        def fake_git(args, cwd, timeout=10):
            for key, val in responses.items():
                if key in " ".join(args):
                    return val
            return (0, "")
        return fake_git

    def test_detects_high_divergence(self):
        """50+ commits behind → high severity."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        commits_behind = "\n".join([f"abc{i} commit" for i in range(55)])
        responses = {
            "rev-parse --git-dir": (0, ".git"),
            "rev-parse --abbrev-ref": (0, "feature-x"),
            "log --oneline main..HEAD": (0, "abc1 commit\nabc2 commit"),
            "log --oneline HEAD..main": (0, commits_behind),
            "diff --stat": (0, "file1 | 2+\nfile2 | 3+\n2 files changed"),
            "diff --name-only main...HEAD": (0, "file1\nfile2"),
            "diff --name-only HEAD...main": (0, "file3\nfile4"),
        }
        with patch("multifleet.probes.worktree_divergence._git", side_effect=self._mock_git(responses)):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.severity == "high"
        assert result.status == "warning"

    def test_ok_on_main_branch(self):
        """On main itself → ok, no divergence."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        responses = {
            "rev-parse --git-dir": (0, ".git"),
            "rev-parse --abbrev-ref": (0, "main"),
        }
        with patch("multifleet.probes.worktree_divergence._git", side_effect=self._mock_git(responses)):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.status == "ok"
        assert result.severity == "info"

    def test_skipped_not_git_repo(self):
        """Not a git repo → skipped."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        with patch("multifleet.probes.worktree_divergence._git", return_value=(-1, "not a repo")):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.status == "skipped"

    def test_medium_divergence(self):
        """15 commits behind, 8 files → medium severity."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        behind = "\n".join([f"abc{i} commit" for i in range(15)])
        files = "\n".join([f"file{i}.py | 2+" for i in range(8)] + ["8 files changed"])
        responses = {
            "rev-parse --git-dir": (0, ".git"),
            "rev-parse --abbrev-ref": (0, "feature-y"),
            "log --oneline main..HEAD": (0, "a1 c1"),
            "log --oneline HEAD..main": (0, behind),
            "diff --stat": (0, files),
            "diff --name-only main...HEAD": (0, ""),
            "diff --name-only HEAD...main": (0, ""),
        }
        with patch("multifleet.probes.worktree_divergence._git", side_effect=self._mock_git(responses)):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.severity == "medium"

    def test_conflict_likelihood_high(self):
        """Many overlapping files → high conflict likelihood."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        behind = "\n".join([f"c{i}" for i in range(55)])
        overlap_files = "\n".join([f"shared{i}.py" for i in range(8)])
        responses = {
            "rev-parse --git-dir": (0, ".git"),
            "rev-parse --abbrev-ref": (0, "feature-z"),
            "log --oneline main..HEAD": (0, "a1"),
            "log --oneline HEAD..main": (0, behind),
            "diff --stat": (0, "f1 | 1+\n1 file"),
            "diff --name-only main...HEAD": (0, overlap_files),
            "diff --name-only HEAD...main": (0, overlap_files),
        }
        with patch("multifleet.probes.worktree_divergence._git", side_effect=self._mock_git(responses)):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.status == "warning"
        assert any("high" in e.lower() for f in result.findings for e in f.evidence)

    def test_clean_branch_no_findings(self):
        """Branch with minimal drift → ok, no findings."""
        from multifleet.probes.worktree_divergence import probe_worktree_divergence
        responses = {
            "rev-parse --git-dir": (0, ".git"),
            "rev-parse --abbrev-ref": (0, "small-fix"),
            "log --oneline main..HEAD": (0, "a1 fix"),
            "log --oneline HEAD..main": (0, "b1 other"),
            "diff --stat": (0, "f1 | 1+\n1 file"),
            "diff --name-only main...HEAD": (0, ""),
            "diff --name-only HEAD...main": (0, ""),
        }
        with patch("multifleet.probes.worktree_divergence._git", side_effect=self._mock_git(responses)):
            result = probe_worktree_divergence({"repo_root": "/fake"})
        assert result.status == "ok"
        assert len(result.findings) == 0


# ── Injection Prioritizer ──

class TestInjectionPrioritizer:

    def test_scores_changed_files(self, tmp_path):
        """Changed files get base score."""
        from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
        with patch("multifleet.probes.injection_prioritizer._git", return_value=(0, "")) as mock_git:
            mock_git.side_effect = [
                (0, ".git"),              # rev-parse
                (0, "file1.py\nfile2.py"), # diff HEAD
                (0, ""),                   # diff cached
            ]
            result = probe_injection_prioritizer({
                "repo_root": str(tmp_path),
                "audit_dir": str(tmp_path / "no-audit"),
            })
        assert result.probe_id == "injection_prioritizer"
        assert len(result.findings) > 0

    def test_failure_files_boost_score(self, tmp_path):
        """Files with failures score higher."""
        from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
        audit = tmp_path / ".fleet-nerve"
        audit.mkdir(parents=True)
        events = [
            {"tool": "test", "file": "src/broken.py", "status": "error"},
            {"tool": "test", "file": "src/broken.py", "status": "error"},
        ]
        with open(audit / "audit-2026-04.jsonl", "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        with patch("multifleet.probes.injection_prioritizer._git") as mock_git:
            mock_git.side_effect = [
                (0, ".git"),
                (0, "src/broken.py"),
                (0, ""),
            ]
            result = probe_injection_prioritizer({
                "repo_root": str(tmp_path),
                "audit_dir": str(audit),
            })
        assert result.status == "warning"  # failure-driven
        assert any("failure" in e.lower() for f in result.findings for e in f.evidence)

    def test_skipped_not_git(self, tmp_path):
        """Not a git repo → skipped."""
        from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
        with patch("multifleet.probes.injection_prioritizer._git", return_value=(-1, "not repo")):
            result = probe_injection_prioritizer({"repo_root": str(tmp_path)})
        assert result.status == "skipped"

    def test_empty_context_returns_ok(self, tmp_path):
        """No changed files and no failures → ok."""
        from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
        with patch("multifleet.probes.injection_prioritizer._git") as mock_git:
            mock_git.side_effect = [
                (0, ".git"),
                (0, ""),
                (0, ""),
            ]
            result = probe_injection_prioritizer({
                "repo_root": str(tmp_path),
                "audit_dir": str(tmp_path / "none"),
            })
        assert result.status == "ok"
        assert len(result.findings) == 0

    def test_hotspot_boost(self, tmp_path):
        """Architectural hotspot files get score boost."""
        from multifleet.probes.injection_prioritizer import _score_snippets
        scored = _score_snippets(
            ["some/__init__.py", "regular.py"],
            {},
            str(tmp_path),
        )
        init_score = next(s["score"] for s in scored if "__init__.py" in s["snippet"])
        reg_score = next(s["score"] for s in scored if s["snippet"] == "regular.py")
        assert init_score > reg_score

    def test_top_5_limit(self, tmp_path):
        """Never returns more than 5 recommendations."""
        from multifleet.probes.injection_prioritizer import _score_snippets
        files = [f"file{i}.py" for i in range(20)]
        scored = _score_snippets(files, {}, str(tmp_path))
        assert len(scored) <= 5

    def test_claude_md_injection(self, tmp_path):
        """CLAUDE.md sections recommended when matching changed dirs."""
        from multifleet.probes.injection_prioritizer import _score_snippets
        (tmp_path / "CLAUDE.md").write_text("# Instructions\n")
        scored = _score_snippets(["memory/brain.py"], {}, str(tmp_path))
        assert any("CLAUDE.md" in s["snippet"] for s in scored)


# ── Integration: all probes registered ──

class TestIntelligenceProbeIntegration:

    def test_all_new_probes_registered(self):
        """All 3 new probes appear in engine registry."""
        from multifleet.probes import create_engine
        engine = create_engine()
        ids = {p["probe_id"] for p in engine.registry.list_probes()}
        assert "repeated_failure_cluster" in ids
        assert "worktree_divergence" in ids
        assert "injection_prioritizer" in ids

    def test_categories_correct(self):
        """Each new probe has expected category."""
        from multifleet.probes import create_engine
        engine = create_engine()
        probes = {p["probe_id"]: p for p in engine.registry.list_probes()}
        assert probes["repeated_failure_cluster"]["category"] == "reliability"
        assert probes["worktree_divergence"]["category"] == "change"
        assert probes["injection_prioritizer"]["category"] == "agent_readiness"

    def test_total_probe_count(self):
        """Engine now has 6 probes total (3 original + 3 new)."""
        from multifleet.probes import create_engine
        engine = create_engine()
        assert len(engine.registry.list_probes()) == 6
