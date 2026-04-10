"""Tests for multifleet.discord_bridge — Discord fleet channel (P0).

All tests run without discord.py installed. External deps are mocked.
"""

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Import resilience ──


class TestImportGracefulDegradation:
    """Module must import even when discord.py is not installed."""

    def test_import_without_discord_installed(self):
        """discord_bridge imports cleanly when discord.py is absent."""
        saved = sys.modules.pop("discord", None)
        try:
            with patch.dict("sys.modules", {"discord": None}):
                sys.modules.pop("multifleet.discord_bridge", None)
                from multifleet import discord_bridge
                assert discord_bridge is not None
                assert hasattr(discord_bridge, "DiscordFleetBridge")
        finally:
            if saved is not None:
                sys.modules["discord"] = saved
            else:
                sys.modules.pop("discord", None)

    def test_has_available_flag(self):
        """Module exposes DISCORD_AVAILABLE bool for feature gating."""
        from multifleet import discord_bridge
        assert hasattr(discord_bridge, "DISCORD_AVAILABLE")
        assert isinstance(discord_bridge.DISCORD_AVAILABLE, bool)


# ── Token retrieval ──


class TestTokenRetrieval:
    """Bot token fetched from macOS Keychain via subprocess."""

    def test_get_token_from_keychain(self):
        from multifleet.discord_bridge import get_bot_token

        fake_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.XYZABC.fake-token-value"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=fake_token + "\n",
                stderr="",
            )
            # Clear env so keychain path is used
            with patch.dict("os.environ", {}, clear=True):
                token = get_bot_token()
            assert token == fake_token
            cmd_args = mock_run.call_args[0][0]
            assert "security" in cmd_args
            assert "find-generic-password" in cmd_args

    def test_get_token_keychain_failure_returns_none(self):
        from multifleet.discord_bridge import get_bot_token

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=44,
                stdout="",
                stderr="SecKeychainSearchCopyNext: not found",
            )
            with patch.dict("os.environ", {}, clear=True):
                token = get_bot_token()
            assert token is None

    def test_get_token_env_fallback(self):
        """Env var DISCORD_BOT_TOKEN is checked first (before keychain)."""
        from multifleet.discord_bridge import get_bot_token

        with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "env-token-123"}):
            token = get_bot_token()
            assert token == "env-token-123"


# ── Message formatting ──


class TestMessageFormatting:
    """Fleet message dict -> Discord embed format."""

    def _make_fleet_msg(self, **overrides):
        base = {
            "type": "context",
            "from": "mac1",
            "to": "mac2",
            "timestamp": "2026-04-09T12:00:00Z",
            "payload": {"subject": "test subject", "body": "test body"},
        }
        base.update(overrides)
        return base

    def test_format_basic_message(self):
        from multifleet.discord_bridge import format_fleet_embed

        msg = self._make_fleet_msg()
        embed = format_fleet_embed(msg)
        assert embed["title"] is not None
        # Source node appears in description ("**mac1** | context")
        assert "mac1" in embed.get("description", "")
        assert embed.get("fields") or embed.get("description")

    def test_format_includes_source_node(self):
        from multifleet.discord_bridge import format_fleet_embed

        msg = self._make_fleet_msg(type="alert", payload={"subject": "fire"})
        embed = format_fleet_embed(msg)
        flat = str(embed)
        assert "mac1" in flat

    def test_format_includes_message_type(self):
        from multifleet.discord_bridge import format_fleet_embed

        msg = self._make_fleet_msg(type="critique")
        embed = format_fleet_embed(msg)
        flat = str(embed)
        assert "critique" in flat.lower()

    def test_format_includes_subject(self):
        from multifleet.discord_bridge import format_fleet_embed

        msg = self._make_fleet_msg(
            payload={"subject": "deployment ready", "body": "all tests pass"}
        )
        embed = format_fleet_embed(msg)
        flat = str(embed)
        assert "deployment ready" in flat

    def test_format_handles_missing_payload(self):
        from multifleet.discord_bridge import format_fleet_embed

        msg = {"type": "heartbeat", "from": "mac3"}
        embed = format_fleet_embed(msg)
        assert isinstance(embed, dict)
        assert "title" in embed

    def test_format_color_varies_by_type(self):
        """Different message types from different nodes produce different colors."""
        from multifleet.discord_bridge import format_fleet_embed

        # Use "unknown" source so node color doesn't override type color
        alert_embed = format_fleet_embed({"type": "alert", "from": "unknown",
                                          "payload": {"subject": "fire"}})
        info_embed = format_fleet_embed({"type": "context", "from": "unknown",
                                         "payload": {"subject": "info"}})
        assert alert_embed.get("color") != info_embed.get("color")


# ── Rate limiting ──


class TestRateLimiting:
    """Messages within 1 second window are queued, not sent immediately."""

    def test_rate_limit_queues_rapid_messages(self):
        from multifleet.discord_bridge import DiscordFleetBridge

        bridge = DiscordFleetBridge(bot_token="fake-token", channel_id="123456789012345678")
        bridge._connected = True
        msg1 = {"type": "context", "from": "mac1", "payload": {"subject": "first"}}
        msg2 = {"type": "context", "from": "mac1", "payload": {"subject": "second"}}

        # First message processed (updates _last_send_time), second queued
        bridge.send_to_discord(msg1)
        bridge.send_to_discord(msg2)
        # At least one should be queued due to rate limit
        assert bridge.pending_queue_size >= 1

    def test_rate_limit_allows_after_window(self):
        from multifleet.discord_bridge import DiscordFleetBridge

        bridge = DiscordFleetBridge(bot_token="fake-token", channel_id="123456789012345678")
        bridge._connected = True

        # First send
        bridge.send_to_discord({"type": "context", "from": "mac1", "payload": {}})

        # Simulate time passing beyond rate limit window
        bridge._last_send_time = time.monotonic() - 2.0
        result = bridge.send_to_discord({"type": "context", "from": "mac2", "payload": {}})
        assert result["sent"] is True


# ── Queuing when disconnected ──


class TestDisconnectedQueuing:
    """send_to_discord() queues message when bot is not connected."""

    def test_queues_when_not_connected(self):
        from multifleet.discord_bridge import DiscordFleetBridge

        bridge = DiscordFleetBridge(bot_token="fake-token", channel_id="123456789012345678")
        bridge._connected = False

        msg = {"type": "context", "from": "mac1", "payload": {"subject": "offline msg"}}
        result = bridge.send_to_discord(msg)

        assert result["queued"] is True
        assert result["sent"] is False
        assert bridge.pending_queue_size == 1

    def test_drains_queue_on_reconnect(self):
        from multifleet.discord_bridge import DiscordFleetBridge

        bridge = DiscordFleetBridge(bot_token="fake-token", channel_id="123456789012345678")
        bridge._connected = False

        for i in range(3):
            bridge.send_to_discord({
                "type": "context", "from": "mac1",
                "payload": {"subject": f"msg-{i}"},
            })
        assert bridge.pending_queue_size == 3

        bridge._connected = True
        drained = bridge.drain_queue()
        assert drained == 3
        assert bridge.pending_queue_size == 0


# ── Fleet message parsing ──


class TestFleetMessageParsing:
    """Discord message content -> fleet daemon payload."""

    def test_parse_fleet_command(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("!fleet send mac2 hello world", "123456789")
        assert result is not None
        assert result["to"] == "mac2"
        assert "hello world" in result["payload"]["body"]

    def test_parse_ignores_non_fleet_messages(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("just a normal chat message", "123456789")
        assert result is None

    def test_parse_ignores_empty_command(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("!fleet ", "123456789")
        assert result is None

    def test_parse_extracts_target_node(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("!fleet send mac3 deploy ready", "123456789")
        assert result is not None
        assert result["to"] == "mac3"

    def test_parse_handles_broadcast(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("!fleet broadcast v2 deployed", "123456789")
        assert result is not None
        assert result["to"] == "all"

    def test_parse_status_command(self):
        from multifleet.discord_bridge import parse_discord_message

        result = parse_discord_message("!fleet status", "123456789")
        assert result is not None
        assert result["type"] == "status_request"


# ── Channel ID validation ──


class TestChannelIdValidation:
    """Channel ID must be a numeric Discord snowflake."""

    def test_valid_snowflake(self):
        from multifleet.discord_bridge import validate_channel_id

        assert validate_channel_id("123456789012345678") is True
        assert validate_channel_id("1234567890123456789") is True

    def test_rejects_non_numeric(self):
        from multifleet.discord_bridge import validate_channel_id

        assert validate_channel_id("not-a-number") is False
        assert validate_channel_id("abc123") is False
        assert validate_channel_id("") is False

    def test_rejects_too_short(self):
        from multifleet.discord_bridge import validate_channel_id

        assert validate_channel_id("12345") is False

    def test_rejects_none(self):
        from multifleet.discord_bridge import validate_channel_id

        assert validate_channel_id(None) is False

    def test_accepts_integer(self):
        from multifleet.discord_bridge import validate_channel_id

        assert validate_channel_id(123456789012345678) is True


# ── Bot token validation ──


class TestBotTokenValidation:
    """Bot token must not be empty or whitespace."""

    def test_valid_token(self):
        from multifleet.discord_bridge import validate_bot_token

        assert validate_bot_token("MTIzNDU2Nzg5MDEyMzQ1Njc4OQ.XYZABC.fake") is True

    def test_rejects_empty(self):
        from multifleet.discord_bridge import validate_bot_token

        assert validate_bot_token("") is False

    def test_rejects_none(self):
        from multifleet.discord_bridge import validate_bot_token

        assert validate_bot_token(None) is False

    def test_rejects_whitespace_only(self):
        from multifleet.discord_bridge import validate_bot_token

        assert validate_bot_token("   ") is False
        assert validate_bot_token("\n\t") is False
