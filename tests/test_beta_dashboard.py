"""Tests for BetaDashboard -- beta user analytics and feedback collection.

All tests use a temp directory for the SQLite database.
"""

import sys
import tempfile
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.beta_dashboard import BetaDashboard, VALID_TIERS, VALID_CATEGORIES, VALID_ISSUE_TYPES


@pytest.fixture
def dashboard(tmp_path):
    return BetaDashboard(data_dir=str(tmp_path))


# ── Registration ──

class TestRegisterUser:
    def test_register_success(self, dashboard):
        result = dashboard.register_user("alice", 3, "pro")
        assert result["status"] == "registered"
        assert result["user_id"] == "alice"
        assert result["node_count"] == 3
        assert result["tier"] == "pro"

    def test_register_duplicate(self, dashboard):
        dashboard.register_user("bob", 1, "free")
        result = dashboard.register_user("bob", 2, "starter")
        assert "error" in result
        assert "already registered" in result["error"]

    def test_register_invalid_tier(self, dashboard):
        result = dashboard.register_user("carol", 1, "invalid_tier")
        assert "error" in result
        assert "Invalid tier" in result["error"]

    def test_register_empty_user_id(self, dashboard):
        result = dashboard.register_user("", 1, "free")
        assert "error" in result

    def test_register_invalid_node_count(self, dashboard):
        result = dashboard.register_user("dave", 0, "free")
        assert "error" in result


# ── Feedback ──

class TestRecordFeedback:
    def test_feedback_success(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        result = dashboard.record_feedback("alice", "bug", "Login is broken")
        assert result["recorded"] is True
        assert result["category"] == "bug"

    def test_feedback_unknown_user(self, dashboard):
        result = dashboard.record_feedback("ghost", "bug", "Something broke")
        assert "error" in result
        assert "Unknown user" in result["error"]

    def test_feedback_invalid_category(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        result = dashboard.record_feedback("alice", "invalid_cat", "text")
        assert "error" in result
        assert "Invalid category" in result["error"]


# ── Issues ──

class TestRecordIssue:
    def test_issue_success(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        result = dashboard.record_issue("alice", "crash", "Segfault on start")
        assert result["recorded"] is True
        assert result["issue_type"] == "crash"

    def test_issue_unknown_user(self, dashboard):
        result = dashboard.record_issue("ghost", "crash", "boom")
        assert "error" in result

    def test_issue_invalid_type(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        result = dashboard.record_issue("alice", "invalid_type", "details")
        assert "error" in result


# ── Stats ──

class TestUserStats:
    def test_user_stats(self, dashboard):
        dashboard.register_user("alice", 2, "starter")
        dashboard.record_feedback("alice", "feature", "Add dark mode")
        dashboard.record_issue("alice", "config", "Bad yaml parse")
        stats = dashboard.user_stats("alice")
        assert stats["user_id"] == "alice"
        assert stats["node_count"] == 2
        assert stats["feedback_count"] == 1
        assert stats["issue_count"] == 1
        assert stats["open_issues"] == 1

    def test_user_stats_unknown(self, dashboard):
        result = dashboard.user_stats("nobody")
        assert "error" in result


class TestGlobalStats:
    def test_global_stats_empty(self, dashboard):
        stats = dashboard.global_stats()
        assert stats["total_users"] == 0
        assert stats["total_nodes"] == 0
        assert stats["onboarding_complete_pct"] == 0.0

    def test_global_stats_populated(self, dashboard):
        dashboard.register_user("alice", 3, "pro")
        dashboard.register_user("bob", 1, "free")
        dashboard.record_feedback("alice", "bug", "broken")
        dashboard.record_feedback("bob", "feature", "want X")
        dashboard.record_issue("alice", "crash", "segfault")
        stats = dashboard.global_stats()
        assert stats["total_users"] == 2
        assert stats["total_nodes"] == 4
        assert stats["total_feedback"] == 2
        assert stats["feedback_by_category"]["bug"] == 1
        assert stats["feedback_by_category"]["feature"] == 1
        assert stats["total_issues"] == 1
        assert stats["open_issues"] == 1


# ── HTML + Export ──

class TestRenderAndExport:
    def test_render_html(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        html = dashboard.render_html()
        assert "<!DOCTYPE html>" in html
        assert "alice" in html
        assert "Beta Dashboard" in html

    def test_export_feedback(self, dashboard):
        dashboard.register_user("alice", 1, "free")
        dashboard.record_feedback("alice", "ux", "Confusing nav")
        dashboard.record_feedback("alice", "bug", "404 on save")
        exported = dashboard.export_feedback()
        assert len(exported) == 2
        assert exported[0]["category"] in ("ux", "bug")
        assert all("text" in e for e in exported)
