"""Tests for AutoCoordinator -- inbox monitoring, dispatch, stall escalation."""

import time
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.auto_coordinator import AutoCoordinator, TrackedTask


@pytest.fixture
def tmp_inbox(tmp_path):
    """Create a temporary fleet inbox structure."""
    inbox = tmp_path / "mac1"
    inbox.mkdir()
    all_dir = tmp_path / "all"
    all_dir.mkdir()
    return inbox


@pytest.fixture
def coord(tmp_inbox):
    """AutoCoordinator with tmp inbox and short stall timeout."""
    with patch("multifleet.auto_coordinator.FLEET_MSG_DIR", tmp_inbox.parent):
        c = AutoCoordinator(
            node_id="mac1",
            stall_timeout_s=2.0,
            inbox_dir=tmp_inbox,
        )
        yield c


def _write_task_message(inbox: Path, subject: str, msg_type: str = "task",
                        from_node: str = "mac2", body: str = "") -> Path:
    """Write a fleet message with YAML frontmatter."""
    name = f"{int(time.time() * 1000)}_{subject.replace(' ', '_')[:30]}.md"
    path = inbox / name
    content = f"""---
from: {from_node}
to: mac1
subject: {subject}
type: {msg_type}
timestamp: 2026-04-10T12:00:00Z
---
{body or subject}
"""
    path.write_text(content)
    return path


class TestScanInbox:

    def test_scan_empty_inbox(self, coord, tmp_inbox):
        tasks = coord.scan_inbox()
        assert tasks == []
        assert coord._stats["scans"] == 1

    def test_scan_finds_task_messages(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth flow")
        _write_task_message(tmp_inbox, "Add logging")
        tasks = coord.scan_inbox()
        assert len(tasks) == 2
        descs = {t.description for t in tasks}
        assert "Fix auth flow" in descs
        assert "Add logging" in descs

    def test_scan_skips_non_task_messages(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Just an update", msg_type="info")
        tasks = coord.scan_inbox()
        assert len(tasks) == 0

    def test_scan_deduplicates_on_rescan(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth flow")
        tasks1 = coord.scan_inbox()
        tasks2 = coord.scan_inbox()
        assert len(tasks1) == 1
        assert len(tasks2) == 0  # already processed

    def test_scan_broadcast_inbox(self, coord, tmp_inbox):
        all_dir = tmp_inbox.parent / "all"
        _write_task_message(all_dir, "Fleet-wide refactor")
        tasks = coord.scan_inbox()
        assert len(tasks) == 1
        assert tasks[0].description == "Fleet-wide refactor"


class TestDispatch:

    def test_auto_dispatch_selects_least_loaded(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()
        coord.update_node_sessions("mac1", 5)
        coord.update_node_sessions("mac2", 1)
        coord.update_node_sessions("mac3", 3)
        dispatched = coord._dispatch_pending()
        assert len(dispatched) == 1
        task = list(coord._tasks.values())[0]
        assert task.assigned_to == "mac2"  # least loaded

    def test_manual_dispatch(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        tasks = coord.scan_inbox()
        task_id = tasks[0].task_id
        assert coord.dispatch_task(task_id, "mac3") is True
        assert coord._tasks[task_id].assigned_to == "mac3"

    def test_dispatch_unknown_task(self, coord):
        assert coord.dispatch_task("nonexistent", "mac2") is False

    def test_dispatch_wires_review_flow(self, coord, tmp_inbox):
        """Dispatch wires task into ReviewFlowOrchestrator."""
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()

        # Mock review_flow to avoid real init
        mock_flow = MagicMock()
        mock_session = MagicMock()
        mock_session.task_id = "review-123"
        mock_flow.start_review.return_value = mock_session
        coord._review_flow = mock_flow

        dispatched = coord._dispatch_pending()
        assert len(dispatched) == 1
        task = list(coord._tasks.values())[0]
        assert task.review_task_id == "review-123"
        mock_flow.start_review.assert_called_once()


class TestStallDetection:

    def test_stall_escalates_old_tasks(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()
        task = list(coord._tasks.values())[0]
        task.assigned_to = "mac2"
        task.dispatched_at = time.time() - 10  # 10s ago
        task.status = "dispatched"

        # With 2s timeout, this should escalate
        stalled = coord._check_stalls()
        assert len(stalled) == 1
        assert task.status == "escalated"
        assert coord._stats["tasks_escalated"] == 1

    def test_no_stall_for_recent_tasks(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()
        task = list(coord._tasks.values())[0]
        task.assigned_to = "mac2"
        task.dispatched_at = time.time()  # just now
        task.status = "dispatched"

        stalled = coord._check_stalls()
        assert len(stalled) == 0
        assert task.status == "dispatched"

    def test_completed_tasks_not_stall_checked(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()
        task = list(coord._tasks.values())[0]
        task.assigned_to = "mac2"
        task.dispatched_at = time.time() - 10
        task.status = "completed"

        stalled = coord._check_stalls()
        assert len(stalled) == 0


class TestProgressTracking:

    def test_update_progress_from_review_flow(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.scan_inbox()
        task = list(coord._tasks.values())[0]
        task.assigned_to = "mac2"
        task.dispatched_at = time.time()
        task.status = "dispatched"
        task.review_task_id = "review-abc"

        mock_flow = MagicMock()
        mock_flow.get_status.return_value = {"phase": "resolved"}
        coord._review_flow = mock_flow

        updated = coord._update_progress()
        assert updated == 1
        assert task.status == "completed"

    def test_mark_complete(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        tasks = coord.scan_inbox()
        task_id = tasks[0].task_id
        assert coord.mark_complete(task_id) is True
        assert coord._tasks[task_id].status == "completed"


class TestTick:

    def test_full_tick_cycle(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        result = coord.tick()
        assert "found 1 new task(s)" in result["actions"]
        assert "dispatched 1 task(s)" in result["actions"]
        assert result["active_tasks"] == 1

    def test_tick_idempotent_on_empty(self, coord, tmp_inbox):
        result = coord.tick()
        assert result["active_tasks"] == 0
        assert result["total_tasks"] == 0


class TestStatus:

    def test_status_output(self, coord, tmp_inbox):
        _write_task_message(tmp_inbox, "Fix auth")
        coord.tick()
        status = coord.status()
        assert status["node_id"] == "mac1"
        assert len(status["tasks"]) == 1
        assert status["active"] == 1
        assert "stats" in status


class TestTaskParsing:

    def test_parse_frontmatter(self, coord, tmp_inbox):
        path = _write_task_message(tmp_inbox, "Build feature X", body="Detailed description here")
        task = coord._parse_task_message(path)
        assert task is not None
        assert task.description == "Build feature X"

    def test_parse_no_frontmatter_skips(self, coord, tmp_inbox):
        path = tmp_inbox / "plain.md"
        path.write_text("Just plain text with no frontmatter")
        task = coord._parse_task_message(path)
        assert task is None

    def test_parse_request_type_accepted(self, coord, tmp_inbox):
        path = _write_task_message(tmp_inbox, "Review PR", msg_type="request")
        task = coord._parse_task_message(path)
        assert task is not None
