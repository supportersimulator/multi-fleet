"""
Tests for SelfHealManager — repair escalation, SSH validation, cooldown,
idle backoff, channel health, vacation markers, message suppression.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from multifleet.self_heal import SelfHealManager


# ── Helpers ──


def _make_nerve(node_id="mac1", chief="mac1", peers_config=None):
    """Build a minimal mock FleetNerveNATS for SelfHealManager."""
    nerve = MagicMock()
    nerve.node_id = node_id
    nerve._chief = chief
    nerve._peers = {}
    nerve._peers_config = peers_config or {"mac1": {}, "mac2": {}, "mac3": {}}
    nerve._channel_health = {}
    nerve._heal_in_flight = {}
    nerve._pending_acks = {}
    nerve._running = True
    nerve._stats = {"started_at": time.time() - 600}
    nerve._ack_timeout_s = 60.0
    nerve._ack_max_retries = 3
    nerve._store = None
    nerve._eventbus = None
    nerve.nc = MagicMock()
    nerve.nc.is_connected = True
    nerve.send = AsyncMock()
    nerve.send_with_fallback = AsyncMock(return_value={"delivered": False})
    nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
    nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
    nerve._resolve_peer = MagicMock(return_value={"user": "admin", "ip": "192.168.1.10"})
    return nerve


def _make_manager(node_id="mac1", **kwargs):
    """Build a SelfHealManager with mocked nerve."""
    nerve = _make_nerve(node_id=node_id, **kwargs)
    with patch.object(SelfHealManager, "_load_vacation_markers"):
        mgr = SelfHealManager(nerve)
    return mgr


# ═══════════════════════════════════════════════════════════════════
# 1. SSH Target Validation (_validate_repair_target)
# ═══════════════════════════════════════════════════════════════════


class TestValidateRepairTarget:
    """Strict allowlist: only clean IPv4 + simple usernames pass."""

    def test_valid_ip_and_user(self):
        assert SelfHealManager._validate_repair_target("192.168.1.10", "admin") is True

    def test_valid_localhost(self):
        assert SelfHealManager._validate_repair_target("127.0.0.1", "root") is True

    def test_valid_user_with_dash_underscore(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "fleet-node_2") is True

    def test_empty_ip(self):
        assert SelfHealManager._validate_repair_target("", "admin") is False

    def test_empty_user(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "") is False

    def test_none_ip(self):
        assert SelfHealManager._validate_repair_target(None, "admin") is False

    def test_none_user(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", None) is False

    def test_ip_with_semicolon_injection(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1; rm -rf /", "admin") is False

    def test_ip_with_pipe_injection(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1|curl evil.com", "admin") is False

    def test_ip_with_backtick_injection(self):
        assert SelfHealManager._validate_repair_target("`whoami`", "admin") is False

    def test_user_with_semicolon(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "admin;id") is False

    def test_user_with_dollar_sign(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "admin$HOME") is False

    def test_user_with_quotes(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", 'admin"') is False

    def test_ip_octet_out_of_range(self):
        assert SelfHealManager._validate_repair_target("999.999.999.999", "admin") is False

    def test_ip_octet_256(self):
        assert SelfHealManager._validate_repair_target("192.168.1.256", "admin") is False

    def test_ip_too_few_octets(self):
        assert SelfHealManager._validate_repair_target("192.168.1", "admin") is False

    def test_ip_too_many_octets(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1.1", "admin") is False

    def test_ip_with_hostname(self):
        assert SelfHealManager._validate_repair_target("evil.com", "admin") is False

    def test_user_too_long(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "a" * 33) is False

    def test_user_max_length(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "a" * 32) is True

    def test_ip_with_newline(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1\nid", "admin") is False

    def test_user_with_null_byte(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "admin\x00") is False

    def test_ip_boundary_values(self):
        assert SelfHealManager._validate_repair_target("0.0.0.0", "user") is True
        assert SelfHealManager._validate_repair_target("255.255.255.255", "user") is True


# ═══════════════════════════════════════════════════════════════════
# 2. Repair Command Allowlist
# ═══════════════════════════════════════════════════════════════════


class TestRepairCommands:
    """REPAIR_COMMANDS dict inside _ssh_repair_peer must be a strict allowlist."""

    @pytest.mark.asyncio
    async def test_unknown_action_rejected(self):
        mgr = _make_manager()
        with patch.object(SelfHealManager, "_validate_repair_target", return_value=True):
            with patch("multifleet.self_heal.subprocess") as mock_sp:
                await mgr._ssh_repair_peer("10.0.0.1", "admin", "drop_tables")
                mock_sp.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_known_action_restart_nats(self):
        mgr = _make_manager()
        with patch("multifleet.self_heal.subprocess"):
            with patch("asyncio.get_event_loop") as mock_loop:
                mock_executor = AsyncMock()
                mock_loop.return_value.run_in_executor = mock_executor
                mock_executor.return_value = MagicMock(returncode=0)
                await mgr._ssh_repair_peer("10.0.0.1", "admin", "restart_nats")
                mock_executor.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_target_blocks_ssh(self):
        mgr = _make_manager()
        with patch("multifleet.self_heal.subprocess") as mock_sp:
            await mgr._ssh_repair_peer("evil.com;rm -rf /", "admin", "restart_nats")
            mock_sp.run.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 3. Repair Lock (cascade prevention)
# ═══════════════════════════════════════════════════════════════════


class TestRepairLock:
    def test_acquire_first_time(self):
        mgr = _make_manager()
        assert mgr._acquire_repair_lock("mac2") is True

    def test_acquire_while_locked(self):
        mgr = _make_manager()
        mgr._acquire_repair_lock("mac2")
        assert mgr._acquire_repair_lock("mac2") is False

    def test_release_allows_reacquire(self):
        mgr = _make_manager()
        mgr._acquire_repair_lock("mac2")
        mgr._release_repair_lock("mac2")
        assert mgr._acquire_repair_lock("mac2") is True

    def test_lock_expires_after_ttl(self):
        mgr = _make_manager()
        mgr._acquire_repair_lock("mac2")
        # Simulate expiry
        mgr._repair_locks["mac2"] = time.time() - mgr._repair_lock_ttl - 1
        assert mgr._is_repair_locked("mac2") is False
        assert mgr._acquire_repair_lock("mac2") is True

    def test_independent_targets(self):
        mgr = _make_manager()
        assert mgr._acquire_repair_lock("mac2") is True
        assert mgr._acquire_repair_lock("mac3") is True
        assert mgr._acquire_repair_lock("mac2") is False


# ═══════════════════════════════════════════════════════════════════
# 4. Chief Detection
# ═══════════════════════════════════════════════════════════════════


class TestChiefDetection:
    def test_mac1_is_chief(self):
        mgr = _make_manager(node_id="mac1", chief="mac1")
        assert mgr._is_chief() is True

    def test_mac2_is_not_chief(self):
        mgr = _make_manager(node_id="mac2", chief="mac1")
        assert mgr._is_chief() is False

    def test_custom_chief(self):
        mgr = _make_manager(node_id="mac3", chief="mac3")
        assert mgr._is_chief() is True

    def test_no_chief_attr_fallback_to_mac1(self):
        mgr = _make_manager(node_id="mac1")
        mgr._nerve._chief = None
        assert mgr._is_chief() is True

    def test_no_chief_attr_non_mac1(self):
        mgr = _make_manager(node_id="mac2")
        mgr._nerve._chief = None
        assert mgr._is_chief() is False


# ═══════════════════════════════════════════════════════════════════
# 5. Idle Backoff & Vacation
# ═══════════════════════════════════════════════════════════════════


class TestIdleBackoff:
    def test_initial_state_no_skip(self):
        mgr = _make_manager()
        assert mgr._should_skip_repair("mac2") is False

    def test_failed_repair_increases_backoff(self):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        state = mgr._idle_backoff["mac2"]
        assert state["failed_repairs"] == 1
        assert state["backoff_s"] == mgr.IDLE_BACKOFF_INITIAL_S * mgr.IDLE_BACKOFF_MULTIPLIER

    def test_backoff_caps_at_max(self):
        mgr = _make_manager()
        for _ in range(20):
            mgr._record_failed_repair("mac2")
        state = mgr._idle_backoff["mac2"]
        assert state["backoff_s"] <= mgr.IDLE_BACKOFF_MAX_S

    def test_should_skip_during_backoff(self):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        # Just recorded, so last_repair_attempt is recent
        assert mgr._should_skip_repair("mac2") is True

    def test_should_not_skip_after_backoff_elapsed(self):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        # Simulate backoff elapsed
        mgr._idle_backoff["mac2"]["last_repair_attempt"] = time.time() - 9999
        assert mgr._should_skip_repair("mac2") is False

    def test_successful_repair_resets_backoff(self):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        mgr._record_failed_repair("mac2")
        mgr._record_successful_repair("mac2")
        assert "mac2" not in mgr._idle_backoff
        assert mgr._should_skip_repair("mac2") is False

    def test_extended_idle_threshold(self):
        mgr = _make_manager()
        assert mgr._is_extended_idle("mac2") is False
        for _ in range(mgr.EXTENDED_IDLE_THRESHOLD):
            mgr._record_failed_repair("mac2")
        assert mgr._is_extended_idle("mac2") is True


class TestVacation:
    def test_vacation_skip_repair(self):
        mgr = _make_manager()
        mgr._vacation_nodes.add("mac2")
        assert mgr._should_skip_repair("mac2") is True

    def test_is_on_vacation(self):
        mgr = _make_manager()
        assert mgr.is_on_vacation("mac2") is False
        mgr._vacation_nodes.add("mac2")
        assert mgr.is_on_vacation("mac2") is True

    def test_mark_vacation_on(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            mgr.mark_vacation("mac2", on_vacation=True)
            assert mgr.is_on_vacation("mac2") is True
            marker = tmp_path / ".fleet-messages" / "mac2" / ".vacation"
            assert marker.exists()

    def test_mark_vacation_off(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            mgr.mark_vacation("mac2", on_vacation=True)
            mgr.mark_vacation("mac2", on_vacation=False)
            assert mgr.is_on_vacation("mac2") is False
            marker = tmp_path / ".fleet-messages" / "mac2" / ".vacation"
            assert not marker.exists()

    def test_vacation_off_resets_backoff(self, tmp_path):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            mgr.mark_vacation("mac2", on_vacation=True)
            mgr.mark_vacation("mac2", on_vacation=False)
        assert "mac2" not in mgr._idle_backoff


# ═══════════════════════════════════════════════════════════════════
# 6. Channel Health Tracking
# ═══════════════════════════════════════════════════════════════════


class TestChannelHealth:
    def test_update_channel_health_initializes(self):
        mgr = _make_manager()
        mgr._update_channel_health("mac2", "P1_nats", "broken")
        assert mgr._nerve._channel_health["mac2"]["P1_nats"] == "broken"

    def test_health_score_all_ok(self):
        mgr = _make_manager()
        from multifleet.channels import ALL_CHANNELS
        mgr._nerve._channel_health["mac2"] = {ch: "ok" for ch in ALL_CHANNELS}
        score, broken = mgr._get_peer_health_score("mac2")
        assert score == len(ALL_CHANNELS)
        assert broken == []

    def test_health_score_some_broken(self):
        mgr = _make_manager()
        from multifleet.channels import ALL_CHANNELS
        mgr._nerve._channel_health["mac2"] = {ch: "ok" for ch in ALL_CHANNELS}
        mgr._nerve._channel_health["mac2"]["P1_nats"] = "broken"
        mgr._nerve._channel_health["mac2"]["P2_http"] = "broken"
        score, broken = mgr._get_peer_health_score("mac2")
        assert score == len(ALL_CHANNELS) - 2
        assert "P1_nats" in broken
        assert "P2_http" in broken

    def test_health_score_unknown_peer(self):
        mgr = _make_manager()
        from multifleet.channels import ALL_CHANNELS
        score, broken = mgr._get_peer_health_score("unknown_peer")
        # Empty dict -> no channels marked broken -> full score
        assert broken == []
        assert score == len(ALL_CHANNELS)


# ═══════════════════════════════════════════════════════════════════
# 7. Message Suppression
# ═══════════════════════════════════════════════════════════════════


class TestMessageSuppression:
    def test_vacation_suppresses(self):
        mgr = _make_manager()
        mgr._vacation_nodes.add("mac2")
        assert mgr._should_suppress_message("mac2") is True

    def test_max_unread_suppresses(self, tmp_path):
        mgr = _make_manager()
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        for i in range(mgr.MAX_UNREAD_MESSAGES_PER_NODE):
            (inbox / f"msg-{i}.md").write_text("test")
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            assert mgr._should_suppress_message("mac2") is True

    def test_below_max_unread_not_suppressed(self, tmp_path):
        mgr = _make_manager()
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        (inbox / "msg-0.md").write_text("test")
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            assert mgr._should_suppress_message("mac2") is False


# ═══════════════════════════════════════════════════════════════════
# 8. Process Messages on Return
# ═══════════════════════════════════════════════════════════════════


class TestProcessMessagesOnReturn:
    def test_empty_inbox(self, tmp_path):
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            result = SelfHealManager.process_messages_on_return("mac2")
            assert result == []

    def test_processes_newest_first(self, tmp_path):
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        # Create messages with different mtimes
        for i in range(3):
            f = inbox / f"msg-{i}.md"
            f.write_text(f"Message {i}")
            # Set mtime so msg-2 is newest
            os.utime(f, (1000 + i * 100, 1000 + i * 100))

        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            result = SelfHealManager.process_messages_on_return("mac2", max_messages=2)
            assert len(result) == 2
            # Newest first
            assert result[0]["file"] == "msg-2.md"
            assert result[1]["file"] == "msg-1.md"
            # Oldest archived without reading
            archive = inbox / "archive"
            assert (archive / "msg-0.md").exists()

    def test_moves_to_archive(self, tmp_path):
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        (inbox / "msg-0.md").write_text("hello")
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            result = SelfHealManager.process_messages_on_return("mac2")
            assert len(result) == 1
            assert not (inbox / "msg-0.md").exists()
            assert (inbox / "archive" / "msg-0.md").exists()

    def test_truncates_content(self, tmp_path):
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        (inbox / "msg-0.md").write_text("x" * 1000)
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            result = SelfHealManager.process_messages_on_return("mac2")
            assert len(result[0]["content"]) == 500


# ═══════════════════════════════════════════════════════════════════
# 9. Escalation Flow (repair_with_escalation)
# ═══════════════════════════════════════════════════════════════════


class TestEscalation:
    @pytest.mark.asyncio
    async def test_repair_locked_skips(self):
        mgr = _make_manager()
        mgr._acquire_repair_lock("mac2")
        result = await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert result["repaired"] is False
        assert result["reason"] == "repair_locked"

    @pytest.mark.asyncio
    async def test_l1_success_returns_level_1(self):
        mgr = _make_manager()
        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P1_nats"})
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert result["repaired"] is True
        assert result["level"] == 1

    @pytest.mark.asyncio
    async def test_non_chief_escalates_l4_to_chief(self):
        mgr = _make_manager(node_id="mac2", chief="mac1")
        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": False})
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac3", ["P1_nats"], "restart_nats")
        assert result["repaired"] is False
        assert result.get("reason") == "escalated_to_chief"
        assert result["level"] == 4

    @pytest.mark.asyncio
    async def test_all_levels_fail(self):
        mgr = _make_manager(node_id="mac1", chief="mac1")
        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": False})
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        mgr._nerve._resolve_peer = MagicMock(return_value={"user": None, "ip": None})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac3", ["P1_nats"], "restart_nats")
        assert result["repaired"] is False
        assert result["reason"] == "all_levels_failed"
        assert result["level"] == 4

    @pytest.mark.asyncio
    async def test_repair_lock_released_after_completion(self):
        mgr = _make_manager()
        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P1_nats"})
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert not mgr._is_repair_locked("mac2")


# ═══════════════════════════════════════════════════════════════════
# 10. Unread Message Count
# ═══════════════════════════════════════════════════════════════════


class TestUnreadCount:
    def test_nonexistent_inbox(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            assert mgr._count_unread_messages("mac2") == 0

    def test_counts_md_files_only(self, tmp_path):
        mgr = _make_manager()
        inbox = tmp_path / ".fleet-messages" / "mac2"
        inbox.mkdir(parents=True)
        (inbox / "msg.md").write_text("yes")
        (inbox / "notes.txt").write_text("no")
        (inbox / ".vacation").write_text("no")
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            assert mgr._count_unread_messages("mac2") == 1


# ═══════════════════════════════════════════════════════════════════
# 11. Self-Heal Trigger (dedup)
# ═══════════════════════════════════════════════════════════════════


class TestAttemptSelfHeal:
    def test_no_broken_channels_noop(self):
        mgr = _make_manager()
        mgr._attempt_self_heal("mac2", "P2_http", [])
        # No task created
        assert "mac2" not in mgr._nerve._heal_in_flight

    def test_dedup_inflight(self):
        mgr = _make_manager()
        fake_task = MagicMock()
        fake_task.done.return_value = False
        mgr._nerve._heal_in_flight["mac2"] = fake_task
        mgr._attempt_self_heal("mac2", "P2_http", ["P1_nats"])
        # Should not replace the existing task
        assert mgr._nerve._heal_in_flight["mac2"] is fake_task


# ═══════════════════════════════════════════════════════════════════
# 12. Load Vacation Markers
# ═══════════════════════════════════════════════════════════════════


class TestLoadVacationMarkers:
    def test_loads_from_disk(self, tmp_path):
        fleet_dir = tmp_path / ".fleet-messages" / "mac2"
        fleet_dir.mkdir(parents=True)
        (fleet_dir / ".vacation").write_text("on vacation")

        nerve = _make_nerve()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            mgr = SelfHealManager(nerve)
        assert mgr.is_on_vacation("mac2")

    def test_no_fleet_dir(self, tmp_path):
        nerve = _make_nerve()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path / "nonexistent"):
            mgr = SelfHealManager(nerve)
        assert len(mgr._vacation_nodes) == 0


# ═══════════════════════════════════════════════════════════════════
# 13. Repair Actions (individual handlers)
# ═══════════════════════════════════════════════════════════════════


class TestRepairActionHandlers:
    @pytest.mark.asyncio
    async def test_restart_nats_skips_when_healthy(self):
        """If NATS connected with recent peers, skip restart to prevent cascade."""
        mgr = _make_manager()
        mgr._nerve._peers = {"mac2": {"_received_at": time.time() - 10}}
        await mgr._repair_restart_nats({"reporter": "mac3"})
        mgr._nerve.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_nats_reconnects_when_stale(self):
        """If no recent peers, reconnect."""
        mgr = _make_manager()
        mgr._nerve._peers = {"mac2": {"_received_at": time.time() - 120}}
        mgr._nerve.connect = AsyncMock()
        mgr._nerve._subscribe_all = AsyncMock()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr._repair_restart_nats({})
        mgr._nerve.connect.assert_called_once()
        mgr._nerve._subscribe_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_nats_reconnects_when_disconnected(self):
        """If NATS client is None, reconnect."""
        mgr = _make_manager()
        mgr._nerve.nc = None
        mgr._nerve.connect = AsyncMock()
        mgr._nerve._subscribe_all = AsyncMock()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await mgr._repair_restart_nats({})
        mgr._nerve.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_repair_git_pull(self):
        mgr = _make_manager()
        with patch("multifleet.self_heal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            await mgr._repair_git_pull({})
            args = mock_sp.run.call_args[0][0]
            assert "git" in args
            assert "pull" in args

    @pytest.mark.asyncio
    async def test_repair_kill_zombie_no_pid(self):
        mgr = _make_manager()
        with patch("os.kill") as mock_kill:
            await mgr._repair_kill_zombie({})
            mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_repair_kill_zombie_with_pid(self):
        mgr = _make_manager()
        with patch("os.kill") as mock_kill:
            await mgr._repair_kill_zombie({"pid": 99999})
            mock_kill.assert_called_once_with(99999, 9)

    @pytest.mark.asyncio
    async def test_repair_kill_zombie_not_found(self):
        """ProcessLookupError is handled gracefully."""
        mgr = _make_manager()
        with patch("os.kill", side_effect=ProcessLookupError):
            await mgr._repair_kill_zombie({"pid": 99999})  # no raise

    @pytest.mark.asyncio
    async def test_repair_restart_tunnel(self):
        mgr = _make_manager()
        with patch("multifleet.self_heal.subprocess") as mock_sp:
            mock_sp.run.return_value = MagicMock(returncode=0)
            await mgr._repair_restart_tunnel({})
            args = mock_sp.run.call_args[0][0]
            assert "launchctl" in args
            assert "kickstart" in args


# ═══════════════════════════════════════════════════════════════════
# 14. Verify Channel
# ═══════════════════════════════════════════════════════════════════


class TestVerifyChannel:
    @pytest.mark.asyncio
    async def test_verify_p1_nats_success(self):
        mgr = _make_manager()
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        result = await mgr._verify_channel("mac2", "P1_nats")
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_p2_http_success(self):
        mgr = _make_manager()
        mgr._nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        result = await mgr._verify_channel("mac2", "P2_http")
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_unknown_channel(self):
        mgr = _make_manager()
        result = await mgr._verify_channel("mac2", "P99_nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_channel_delivery_fails(self):
        mgr = _make_manager()
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        result = await mgr._verify_channel("mac2", "P1_nats")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_channel_exception(self):
        mgr = _make_manager()
        mgr._nerve._send_p1_nats = AsyncMock(side_effect=ConnectionError("down"))
        result = await mgr._verify_channel("mac2", "P1_nats")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_channel_timeout(self):
        mgr = _make_manager()
        async def slow(*a, **kw):
            await asyncio.sleep(30)
            return {"delivered": True}
        mgr._nerve._send_p1_nats = slow
        result = await mgr._verify_channel("mac2", "P1_nats")
        assert result is False


# ═══════════════════════════════════════════════════════════════════
# 15. Degradation Alert & Fleet Assist
# ═══════════════════════════════════════════════════════════════════


class TestDegradationAlert:
    @pytest.mark.asyncio
    async def test_alert_writes_file(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            with patch("multifleet.self_heal.subprocess"):
                await mgr._alert_degradation("mac2", ["P1_nats"], "restart_nats")
        fleet_dir = tmp_path / ".fleet-messages" / "all"
        files = list(fleet_dir.glob("*degradation*"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "mac2" in content
        assert "P1_nats" in content

    @pytest.mark.asyncio
    async def test_alert_rate_limited(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            with patch("multifleet.self_heal.subprocess"):
                await mgr._alert_degradation("mac2", ["P1_nats"], "restart_nats")
        # Second call within 30min cooldown
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            with patch("multifleet.self_heal.subprocess") as mock_sp:
                await mgr._alert_degradation("mac2", ["P1_nats"], "restart_nats")
                mock_sp.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_git_failure_falls_back_to_seed(self, tmp_path):
        mgr = _make_manager()
        with patch("multifleet.self_heal.REPO_ROOT", tmp_path):
            with patch("multifleet.self_heal.subprocess") as mock_sp:
                mock_sp.run.side_effect = Exception("git broken")
                with patch("multifleet.self_heal.Path") as mock_path_cls:
                    # Let REPO_ROOT paths work normally but seed path be mock
                    mock_path_cls.side_effect = lambda x: Path(x)
                    await mgr._alert_degradation("mac3", ["P2_http"], "check")
        # Degradation should still be tracked (cooldown set)
        assert "degrade:mac3" in mgr._degradation_cooldowns


class TestFleetAssist:
    @pytest.mark.asyncio
    async def test_broadcast_via_nats(self):
        mgr = _make_manager()
        mgr._nerve.send = AsyncMock()
        await mgr._request_fleet_assist("mac2", ["P1_nats"], "restart_nats")
        mgr._nerve.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_to_seed_files(self):
        mgr = _make_manager()
        mgr._nerve.nc.is_connected = False
        await mgr._request_fleet_assist("mac2", ["P1_nats"], "restart_nats")
        # Should attempt seed files (mac3 only -- skip self=mac1, target=mac2)

    @pytest.mark.asyncio
    async def test_nats_send_failure_falls_back(self):
        mgr = _make_manager()
        mgr._nerve.send = AsyncMock(side_effect=Exception("nats down"))
        mgr._nerve.nc.is_connected = True
        await mgr._request_fleet_assist("mac2", ["P1_nats"], "restart_nats")
        # Should not raise — gracefully degrades


class TestHandleFleetAssist:
    @pytest.mark.asyncio
    async def test_ignores_self_request(self):
        mgr = _make_manager(node_id="mac1")
        msg = {"payload": {"body": json.dumps({
            "target": "mac2", "reporter": "mac1",
            "broken_channels": ["P1_nats"], "action": "restart_nats",
        })}}
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock) as mock_rep:
            await mgr._handle_fleet_assist(msg)
            mock_rep.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_self_target(self):
        mgr = _make_manager(node_id="mac1")
        msg = {"payload": {"body": json.dumps({
            "target": "mac1", "reporter": "mac2",
            "broken_channels": ["P1_nats"], "action": "restart_nats",
        })}}
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock) as mock_rep:
            await mgr._handle_fleet_assist(msg)
            mock_rep.assert_not_called()

    @pytest.mark.asyncio
    async def test_triggers_repair_for_other_node(self):
        mgr = _make_manager(node_id="mac1")
        msg = {"payload": {"body": json.dumps({
            "target": "mac3", "reporter": "mac2",
            "broken_channels": ["P1_nats"], "action": "restart_nats",
        })}}
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock,
                          return_value={"repaired": True, "level": 2}) as mock_rep:
            await mgr._handle_fleet_assist(msg)
            mock_rep.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_duplicate_assist(self):
        mgr = _make_manager(node_id="mac1")
        # Clear class-level cooldowns from prior tests
        SelfHealManager._degradation_cooldowns.clear()
        msg = {"payload": {"body": json.dumps({
            "target": "mac3", "reporter": "mac2",
            "broken_channels": ["P1_nats"], "action": "restart_nats",
        })}}
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock,
                          return_value={"repaired": False}) as mock_rep:
            await mgr._handle_fleet_assist(msg)
            mock_rep.assert_called_once()
            # Second call within cooldown
            mock_rep.reset_mock()
            await mgr._handle_fleet_assist(msg)
            mock_rep.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_malformed_body(self):
        mgr = _make_manager()
        msg = {"payload": {"body": "not json"}}
        await mgr._handle_fleet_assist(msg)  # no raise

    @pytest.mark.asyncio
    async def test_handles_missing_payload(self):
        mgr = _make_manager()
        await mgr._handle_fleet_assist({})  # no raise


# ═══════════════════════════════════════════════════════════════════
# 16. Repair With Backoff (learning store integration)
# ═══════════════════════════════════════════════════════════════════


class TestRepairWithBackoff:
    @pytest.mark.asyncio
    async def test_success_resets_backoff(self):
        mgr = _make_manager()
        mgr._record_failed_repair("mac2")
        assert "mac2" in mgr._idle_backoff
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock,
                          return_value={"repaired": True, "level": 1}):
            with patch("multifleet.self_heal._get_memory_feedback") as mock_fb:
                mock_fb.return_value = MagicMock(record_repair=MagicMock())
                await mgr._repair_with_backoff("mac2", ["P1_nats"], "restart_nats")
        assert "mac2" not in mgr._idle_backoff

    @pytest.mark.asyncio
    async def test_failure_increases_backoff(self):
        mgr = _make_manager()
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock,
                          return_value={"repaired": False, "reason": "all_levels_failed", "level": 4}):
            with patch("multifleet.self_heal._get_memory_feedback") as mock_fb:
                mock_fb.return_value = MagicMock(record_repair=MagicMock())
                await mgr._repair_with_backoff("mac2", ["P1_nats"], "restart_nats")
        assert "mac2" in mgr._idle_backoff
        assert mgr._idle_backoff["mac2"]["failed_repairs"] == 1

    @pytest.mark.asyncio
    async def test_memory_feedback_errors_dont_block(self):
        """Memory feedback failures must never block fleet ops."""
        mgr = _make_manager()
        with patch.object(mgr, "repair_with_escalation", new_callable=AsyncMock,
                          return_value={"repaired": True, "level": 1}):
            with patch("multifleet.self_heal._get_memory_feedback", side_effect=Exception("boom")):
                await mgr._repair_with_backoff("mac2", ["P1_nats"], "restart_nats")
        # Should complete without raising


# ═══════════════════════════════════════════════════════════════════
# 17. L2/L3 Escalation Paths
# ═══════════════════════════════════════════════════════════════════


class TestEscalationLevels:
    @pytest.mark.asyncio
    async def test_l1_fails_l2_succeeds(self):
        """L1 probe fails, L2 repair succeeds."""
        mgr = _make_manager()
        probe_call = 0

        async def probe_fn(target, msg):
            nonlocal probe_call
            probe_call += 1
            # L1 probe fails, L2 probe succeeds
            return {"delivered": probe_call >= 2}

        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P4"})
        mgr._nerve._send_p1_nats = probe_fn
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert result["repaired"] is True
        assert result["level"] == 2

    @pytest.mark.asyncio
    async def test_l1_l2_fail_l3_succeeds(self):
        """L1 and L2 probes fail, L3 succeeds."""
        mgr = _make_manager()
        probe_call = 0

        async def probe_fn(target, msg):
            nonlocal probe_call
            probe_call += 1
            return {"delivered": probe_call >= 3}

        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P4"})
        mgr._nerve._send_p1_nats = probe_fn
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert result["repaired"] is True
        assert result["level"] == 3

    @pytest.mark.asyncio
    async def test_l1_delivery_fails_skips_wait(self):
        """If L1 message can't even be delivered, skip straight to L2."""
        mgr = _make_manager()
        call_count = 0

        async def fallback_fn(target, msg):
            nonlocal call_count
            call_count += 1
            # L1 delivery fails, L2 delivery succeeds
            return {"delivered": call_count >= 2, "channel": "P2"}

        mgr._nerve.send_with_fallback = fallback_fn
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
        assert result["repaired"] is True
        assert result["level"] == 2

    @pytest.mark.asyncio
    async def test_prior_repair_knowledge_logged(self):
        """Verify repair_with_escalation queries learning store for prior repairs."""
        mgr = _make_manager()
        mgr._nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        mgr._nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with patch("multifleet.self_heal._get_memory_feedback") as mock_fb:
                fb_instance = MagicMock()
                fb_instance.search_prior_repairs.return_value = [
                    {"title": "Prior fix for NATS"}
                ]
                mock_fb.return_value = fb_instance
                await mgr.repair_with_escalation("mac2", ["P1_nats"], "restart_nats")
                fb_instance.search_prior_repairs.assert_called_once()
