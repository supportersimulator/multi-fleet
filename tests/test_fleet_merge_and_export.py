"""Tests for fleet_merge and fleet_export MCP tools.

Covers: merge preview, gate check, cherry-pick, export formats, billing gate.
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure tools/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ── fleet_merge Tests ──


class TestFleetMergePreview:
    """Test fleet_merge action=preview."""

    @patch("subprocess.run")
    def test_preview_returns_diff_stat(self, mock_run):
        """Preview shows diff stat, file list, conflict check, and commits."""
        from tools.fleet_nerve_mcp import fleet_merge

        def side_effect(cmd, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if "diff" in cmd and "--stat" in cmd:
                m.stdout = " file.py | 10 +++\n 1 file changed"
            elif "diff" in cmd and "--name-only" in cmd:
                m.stdout = "file.py"
            elif "merge-tree" in cmd:
                m.returncode = 0
            elif "log" in cmd:
                m.stdout = "abc1234 feat: something\ndef5678 fix: other"
            return m

        mock_run.side_effect = side_effect
        result = fleet_merge(action="preview", branch="feature-x")

        assert result["branch"] == "feature-x"
        assert "diff_stat" in result
        assert result["has_conflicts"] is False
        assert result["commit_count"] == 2

    def test_preview_requires_branch(self):
        """Preview without branch returns error."""
        from tools.fleet_nerve_mcp import fleet_merge
        result = fleet_merge(action="preview")
        assert "error" in result

    @patch("subprocess.run")
    def test_preview_detects_conflicts(self, mock_run):
        """Preview detects merge conflicts."""
        from tools.fleet_nerve_mcp import fleet_merge

        def side_effect(cmd, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if "merge-tree" in cmd:
                m.returncode = 1
                m.stderr = "CONFLICT (content): Merge conflict in file.py"
            elif "log" in cmd:
                m.stdout = "abc commit"
            return m

        mock_run.side_effect = side_effect
        result = fleet_merge(action="preview", branch="conflict-branch")
        assert result["has_conflicts"] is True


class TestFleetMergeCheck:
    """Test fleet_merge action=check."""

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_check_runs_gate(self, _audit, mock_run):
        """Check action runs FleetMergeGate and returns result."""
        from tools.fleet_nerve_mcp import fleet_merge

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = fleet_merge(action="check", branch="feature-x")

        assert "gate" in result
        assert "passed" in result
        assert "checks" in result

    def test_check_requires_branch(self):
        """Check without branch returns error."""
        from tools.fleet_nerve_mcp import fleet_merge
        result = fleet_merge(action="check")
        assert "error" in result


class TestFleetMergeExecute:
    """Test fleet_merge action=execute."""

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_execute_with_passing_gate(self, _audit, mock_run):
        """Execute merges when gate passes."""
        from tools.fleet_nerve_mcp import fleet_merge

        # merge-tree (gate check) returns clean, then git merge returns success
        def side_effect(cmd, **kwargs):
            if "merge-tree" in cmd:
                return MagicMock(returncode=0, stdout="treehash", stderr="")
            if "merge" in cmd and "--no-ff" in cmd:
                return MagicMock(returncode=0, stdout="Merge made", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        # Mock gains gate import to prevent it from interfering
        mock_gains = MagicMock()
        mock_gains_cls = MagicMock(return_value=mock_gains)
        mock_gains.run.return_value = MagicMock(passed=True, passed_count=5, total=5)

        with patch("tools.fleet_nerve_mcp._get_evidence_ledger", return_value=None), \
             patch("tools.fleet_nerve_mcp._get_lease_manager", return_value=None), \
             patch("tools.fleet_nerve_mcp._get_rebuttal_manager", return_value=None), \
             patch("multifleet.gains_gate.FleetGainsGate", mock_gains_cls):
            result = fleet_merge(action="execute", branch="feature-x",
                                 reason="ship it")

        assert result["merged"] is True
        assert result["branch"] == "feature-x"

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_execute_blocked_by_gate(self, _audit, mock_run):
        """Execute blocked when gate fails (e.g., merge conflicts)."""
        from tools.fleet_nerve_mcp import fleet_merge

        # merge-tree returns conflict (inside the gate check)
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="CONFLICT in file.py"
        )

        with patch("tools.fleet_nerve_mcp._get_lease_manager", return_value=None), \
             patch("tools.fleet_nerve_mcp._get_rebuttal_manager", return_value=None):
            result = fleet_merge(action="execute", branch="conflict-branch")

        assert result["merged"] is False
        assert "gate" in result

    def test_execute_requires_branch(self):
        """Execute without branch returns error."""
        from tools.fleet_nerve_mcp import fleet_merge
        result = fleet_merge(action="execute")
        assert "error" in result


class TestFleetMergeCherryPick:
    """Test fleet_merge action=cherry_pick."""

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_cherry_pick_success(self, _audit, mock_run):
        """Cherry-pick succeeds for valid commits."""
        from tools.fleet_nerve_mcp import fleet_merge

        mock_run.return_value = MagicMock(returncode=0, stdout="Applied", stderr="")

        with patch("tools.fleet_nerve_mcp._get_evidence_ledger", return_value=None):
            result = fleet_merge(action="cherry_pick", commits="abc123,def456")

        assert result["cherry_picked"] is True
        assert len(result["results"]) == 2

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_cherry_pick_aborts_on_failure(self, _audit, mock_run):
        """Cherry-pick aborts after first failure."""
        from tools.fleet_nerve_mcp import fleet_merge

        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if "cherry-pick" in cmd and "--abort" not in cmd:
                if call_count[0] == 2:  # second cherry-pick (first is merge-tree)
                    return MagicMock(returncode=0, stdout="ok", stderr="")
                return MagicMock(returncode=1, stdout="", stderr="conflict")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = side_effect

        with patch("tools.fleet_nerve_mcp._get_evidence_ledger", return_value=None):
            result = fleet_merge(action="cherry_pick", commits="abc,def,ghi")

        assert result["cherry_picked"] is False

    def test_cherry_pick_requires_commits(self):
        """Cherry-pick without commits returns error."""
        from tools.fleet_nerve_mcp import fleet_merge
        result = fleet_merge(action="cherry_pick")
        assert "error" in result


class TestFleetMergeStatus:
    """Test fleet_merge action=status."""

    @patch("subprocess.run")
    @patch("multifleet.invariance_gates._record_audit")
    def test_status_lists_unmerged_branches(self, _audit, mock_run):
        """Status lists unmerged branches with gate check."""
        from tools.fleet_nerve_mcp import fleet_merge

        def side_effect(cmd, **kwargs):
            m = MagicMock(returncode=0, stdout="", stderr="")
            if "branch" in cmd:
                m.stdout = "  origin/feat-a\n  origin/feat-b"
            return m

        mock_run.side_effect = side_effect
        result = fleet_merge(action="status")

        assert "pending_merges" in result
        assert result["count"] >= 0


# ── fleet_export Tests ──


class TestFleetExportBillingGate:
    """Test billing tier gating on fleet_export."""

    def test_community_tier_blocked(self):
        """Community tier cannot export."""
        from tools.fleet_nerve_mcp import fleet_export

        with patch.dict(os.environ, {"MULTIFLEET_BILLING_TIER": "community"}):
            result = fleet_export(format="json", scope="decisions")

        assert "error" in result
        assert "Pro" in result["error"]
        assert result["tier"] == "community"

    def test_pro_tier_allowed(self):
        """Pro tier can export."""
        from tools.fleet_nerve_mcp import fleet_export

        mock_ledger = MagicMock()
        mock_ledger.query.return_value = [{"event_type": "merge_decision", "node_id": "mac1"}]

        with patch.dict(os.environ, {"MULTIFLEET_BILLING_TIER": "pro"}), \
             patch("tools.fleet_nerve_mcp._get_evidence_ledger", return_value=mock_ledger):
            result = fleet_export(format="json", scope="decisions")

        assert "error" not in result
        assert result["format"] == "json"
        assert result["count"] == 1


class TestFleetExportFormats:
    """Test export output formats."""

    def _export_with_data(self, fmt, scope="evidence"):
        from tools.fleet_nerve_mcp import fleet_export

        mock_ledger = MagicMock()
        mock_ledger.query.return_value = [
            {"entry_id": "a1", "event_type": "merge_decision",
             "node_id": "mac1", "timestamp": 1700000000.0,
             "subject": "merged feat", "payload": {"branch": "feat-x"}},
            {"entry_id": "b2", "event_type": "gate_merge",
             "node_id": "mac2", "timestamp": 1700001000.0,
             "subject": "gate check", "payload": {"passed": True}},
        ]

        with patch.dict(os.environ, {"MULTIFLEET_BILLING_TIER": "pro"}), \
             patch("tools.fleet_nerve_mcp._get_evidence_ledger", return_value=mock_ledger):
            return fleet_export(format=fmt, scope=scope)

    def test_json_format(self):
        """JSON export returns data array."""
        result = self._export_with_data("json")
        assert result["format"] == "json"
        assert result["count"] == 2
        assert isinstance(result["data"], list)

    def test_csv_format(self):
        """CSV export returns header + rows."""
        result = self._export_with_data("csv")
        assert result["format"] == "csv"
        assert "entry_id" in result["csv"]
        assert "a1" in result["csv"]

    def test_markdown_format(self):
        """Markdown export returns table."""
        result = self._export_with_data("markdown")
        assert result["format"] == "markdown"
        assert "| entry_id" in result["markdown"]
        assert "---" in result["markdown"]

    def test_invalid_format_rejected(self):
        """Invalid format returns error."""
        from tools.fleet_nerve_mcp import fleet_export

        with patch.dict(os.environ, {"MULTIFLEET_BILLING_TIER": "pro"}):
            result = fleet_export(format="xml", scope="decisions")

        assert "error" in result
        assert "xml" in result["error"]

    def test_invalid_scope_rejected(self):
        """Invalid scope returns error."""
        from tools.fleet_nerve_mcp import fleet_export

        with patch.dict(os.environ, {"MULTIFLEET_BILLING_TIER": "pro"}):
            result = fleet_export(format="json", scope="secrets")

        assert "error" in result
        assert "secrets" in result["error"]

    def test_unknown_merge_action(self):
        """Unknown action returns helpful error."""
        from tools.fleet_nerve_mcp import fleet_merge
        result = fleet_merge(action="destroy")
        assert "error" in result
        assert "preview" in result["error"]
