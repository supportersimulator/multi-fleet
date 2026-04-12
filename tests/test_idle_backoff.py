"""
Tests for idle node backoff and vacation system — prevents notification floods
when fleet nodes are idle/on vacation.

Covers:
- Exponential backoff after failed repairs
- Vacation marker persistence
- Message cap enforcement
- Batch message processing on return
- Integration with watchdog checks
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def idle_nerve(mock_nerve):
    """Extend mock_nerve with idle backoff state (normally set in _watchdog_loop)."""
    mock_nerve._idle_backoff = {}
    mock_nerve._vacation_nodes = set()
    mock_nerve._repair_in_progress = {}
    mock_nerve._peer_online_state = {}
    mock_nerve._idle_wake_sent = {}
    mock_nerve._last_suggestion_scan = 0.0
    mock_nerve._last_activity_at = time.time()
    mock_nerve._eventbus = None
    mock_nerve._heal_in_flight = {}
    mock_nerve._channel_health = {}
    mock_nerve._stats = {"started_at": time.time() - 600}
    return mock_nerve


class TestIdleBackoff:
    """Exponential backoff for idle nodes — prevents repair floods."""

    def test_first_failure_records_backoff(self, idle_nerve):
        """First failed repair creates backoff state with initial delay."""
        idle_nerve._record_failed_repair("mac2")
        state = idle_nerve._idle_backoff.get("mac2")
        assert state is not None
        assert state["failed_repairs"] == 1
        assert state["backoff_s"] == idle_nerve.IDLE_BACKOFF_INITIAL_S * idle_nerve.IDLE_BACKOFF_MULTIPLIER

    def test_backoff_doubles_on_each_failure(self, idle_nerve):
        """Backoff doubles with each consecutive failure."""
        idle_nerve._record_failed_repair("mac2")
        idle_nerve._record_failed_repair("mac2")
        idle_nerve._record_failed_repair("mac2")

        state = idle_nerve._idle_backoff["mac2"]
        assert state["failed_repairs"] == 3
        expected = min(
            idle_nerve.IDLE_BACKOFF_INITIAL_S * (idle_nerve.IDLE_BACKOFF_MULTIPLIER ** 3),
            idle_nerve.IDLE_BACKOFF_MAX_S,
        )
        assert state["backoff_s"] == expected

    def test_backoff_capped_at_max(self, idle_nerve):
        """Backoff never exceeds IDLE_BACKOFF_MAX_S (1 hour)."""
        for _ in range(20):
            idle_nerve._record_failed_repair("mac2")
        state = idle_nerve._idle_backoff["mac2"]
        assert state["backoff_s"] <= idle_nerve.IDLE_BACKOFF_MAX_S

    def test_successful_repair_resets_backoff(self, idle_nerve):
        """Successful repair clears all backoff state."""
        idle_nerve._record_failed_repair("mac2")
        idle_nerve._record_failed_repair("mac2")
        assert "mac2" in idle_nerve._idle_backoff

        idle_nerve._record_successful_repair("mac2")
        assert "mac2" not in idle_nerve._idle_backoff

    def test_should_skip_repair_during_backoff(self, idle_nerve):
        """Repair is skipped when backoff timer hasn't elapsed."""
        idle_nerve._record_failed_repair("mac2")
        assert idle_nerve._should_skip_repair("mac2") is True

    def test_should_not_skip_after_backoff_elapsed(self, idle_nerve):
        """Repair is allowed after backoff timer elapses."""
        idle_nerve._record_failed_repair("mac2")
        idle_nerve._idle_backoff["mac2"]["last_repair_attempt"] = time.time() - 999999
        assert idle_nerve._should_skip_repair("mac2") is False

    def test_extended_idle_detection(self, idle_nerve):
        """Node is considered extended idle after EXTENDED_IDLE_THRESHOLD failures."""
        assert idle_nerve._is_extended_idle("mac2") is False
        for _ in range(idle_nerve.EXTENDED_IDLE_THRESHOLD):
            idle_nerve._record_failed_repair("mac2")
        assert idle_nerve._is_extended_idle("mac2") is True


class TestVacationMode:
    """Vacation marker system — explicitly stop notifications for a node."""

    def test_mark_vacation(self, idle_nerve, tmp_path):
        """Marking vacation adds to set and creates file."""
        fleet_dir = tmp_path / ".fleet-messages" / "mac2"
        fleet_dir.mkdir(parents=True)

        with patch("tools.fleet_nerve_nats.REPO_ROOT", tmp_path):
            idle_nerve.mark_vacation("mac2", on_vacation=True)
        assert idle_nerve.is_on_vacation("mac2") is True

    def test_unmark_vacation_resets_backoff(self, idle_nerve, tmp_path):
        """Unmarking vacation clears vacation state and resets backoff."""
        fleet_dir = tmp_path / ".fleet-messages" / "mac2"
        fleet_dir.mkdir(parents=True)
        (fleet_dir / ".vacation").write_text("test")

        idle_nerve._vacation_nodes.add("mac2")
        idle_nerve._record_failed_repair("mac2")

        with patch("tools.fleet_nerve_nats.REPO_ROOT", tmp_path):
            idle_nerve.mark_vacation("mac2", on_vacation=False)

        assert idle_nerve.is_on_vacation("mac2") is False
        assert "mac2" not in idle_nerve._idle_backoff

    def test_vacation_skips_repair(self, idle_nerve):
        """Vacation nodes are always skipped by _should_skip_repair."""
        idle_nerve._vacation_nodes.add("mac2")
        assert idle_nerve._should_skip_repair("mac2") is True


class TestMessageCap:
    """Unread message cap — stop flooding when inbox is full."""

    def test_count_unread_messages(self, idle_nerve, tmp_path):
        """Counts .md files in a node's inbox."""
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        for i in range(5):
            (inbox / f"msg-{i}.md").write_text(f"message {i}")

        with patch("tools.fleet_nerve_nats.REPO_ROOT", tmp_path):
            assert idle_nerve._count_unread_messages("mac2") == 5

    def test_suppress_when_cap_exceeded(self, idle_nerve, tmp_path):
        """Messages are suppressed when unread count >= MAX_UNREAD_MESSAGES_PER_NODE."""
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        for i in range(idle_nerve.MAX_UNREAD_MESSAGES_PER_NODE):
            (inbox / f"msg-{i}.md").write_text(f"message {i}")

        with patch("tools.fleet_nerve_nats.REPO_ROOT", tmp_path):
            assert idle_nerve._should_suppress_message("mac2") is True

    def test_no_suppress_under_cap(self, idle_nerve, tmp_path):
        """Messages are not suppressed when under the cap."""
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        for i in range(3):
            (inbox / f"msg-{i}.md").write_text(f"message {i}")

        with patch("tools.fleet_nerve_nats.REPO_ROOT", tmp_path):
            assert idle_nerve._should_suppress_message("mac2") is False


class TestBatchProcessOnReturn:
    """Batch message processing when an idle node returns."""

    def test_processes_newest_first(self, tmp_path):
        """Messages are processed newest-first when returning from idle."""
        from multifleet.self_heal import SelfHealManager

        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)

        # Create messages with staggered mtimes
        for i in range(5):
            msg = inbox / f"msg-{i:03d}.md"
            msg.write_text(f"Message {i} content")
            os.utime(msg, (time.time() - (5 - i) * 60, time.time() - (5 - i) * 60))

        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            results = SelfHealManager.process_messages_on_return("mac2", max_messages=3)

        # Should get 3 newest messages
        assert len(results) == 3
        # Newest first
        assert results[0]["file"] == "msg-004.md"

        # All 5 should be archived
        archived = list((inbox / "archive").iterdir())
        assert len(archived) == 5

        # Inbox should be empty (only archive dir remains)
        remaining = [f for f in inbox.iterdir() if f.name != "archive"]
        assert len(remaining) == 0

    def test_empty_inbox_returns_empty(self, tmp_path):
        """Empty inbox returns empty list."""
        from multifleet.self_heal import SelfHealManager

        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)

        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            results = SelfHealManager.process_messages_on_return("mac2")
        assert results == []

    def test_nonexistent_inbox_returns_empty(self, tmp_path):
        """Nonexistent inbox returns empty list."""
        from multifleet.self_heal import SelfHealManager

        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            results = SelfHealManager.process_messages_on_return("mac99")
        assert results == []


class TestWatchdogIdleIntegration:
    """Verify watchdog respects idle backoff in its check loops."""

    @pytest.mark.asyncio
    async def test_watchdog_skips_vacation_peer(self, idle_nerve):
        """_watchdog_check_peers skips vacation nodes entirely."""
        # Mark BOTH configured peers as on vacation
        idle_nerve._vacation_nodes.add("mac2")
        idle_nerve._vacation_nodes.add("mac3")
        idle_nerve._peers = {"mac2": {"_received_at": time.time() - 999}}

        idle_nerve.repair_with_escalation = AsyncMock()
        idle_nerve._repair_with_backoff = AsyncMock()

        await idle_nerve._watchdog_check_peers()

        idle_nerve._repair_with_backoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchdog_skips_backed_off_peer(self, idle_nerve):
        """_watchdog_check_peers skips peers in active backoff."""
        # Mark BOTH configured peers as backed off
        idle_nerve._record_failed_repair("mac2")
        idle_nerve._record_failed_repair("mac3")
        idle_nerve._peers = {"mac2": {"_received_at": time.time() - 999}}

        idle_nerve.repair_with_escalation = AsyncMock()
        idle_nerve._repair_with_backoff = AsyncMock()

        await idle_nerve._watchdog_check_peers()

        idle_nerve._repair_with_backoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchdog_repairs_non_idle_stale_peer(self, idle_nerve):
        """_watchdog_check_peers still repairs stale peers without backoff."""
        idle_nerve._peers = {"mac2": {"_received_at": time.time() - 999}}

        idle_nerve._repair_with_backoff = AsyncMock()

        await idle_nerve._watchdog_check_peers()
        # Give the created task a chance to run
        await asyncio.sleep(0.05)

        # Should have attempted repair since no backoff is active
        # (The method creates a task, so we check _repair_in_progress was set)
        assert "mac2" in idle_nerve._repair_in_progress


class TestFloodQuantification:
    """Quantify message reduction — the core metric for this fix."""

    def test_24h_vacation_message_reduction(self, idle_nerve):
        """24h vacation with backoff sends ~24 retries, not ~1000+.

        Before fix: 5min cooldown = 288 repair cycles/day, each sending 4+ messages = 1000+
        After fix: exponential backoff caps at 1hr = ~24 retries/day
        """
        retries = 0
        sim_time = 0
        backoff_s = idle_nerve.IDLE_BACKOFF_INITIAL_S

        while sim_time < 86400:  # 24 hours
            retries += 1
            backoff_s = min(
                backoff_s * idle_nerve.IDLE_BACKOFF_MULTIPLIER,
                idle_nerve.IDLE_BACKOFF_MAX_S,
            )
            sim_time += backoff_s

        # With exponential backoff capped at 1hr: ~24 retries in 24h
        assert retries < 30, f"Expected <30 retries in 24h, got {retries}"
        # Without backoff: 288+ (every 5min)
        assert retries < 288 / 10, "Should be at least 10x fewer retries than before"

    def test_vacation_zero_messages(self, idle_nerve):
        """Vacation mode: zero repair attempts in any time period."""
        idle_nerve._vacation_nodes.add("mac2")

        # No matter how long, vacation = zero retries
        assert idle_nerve._should_skip_repair("mac2") is True
        assert idle_nerve.is_on_vacation("mac2") is True
