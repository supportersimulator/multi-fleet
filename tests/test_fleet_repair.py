"""
Tests for repair escalation — 4-level system and safe action handlers.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestRepairActions:
    """Safe local repair actions — the handlers that execute on self-heal requests."""

    @pytest.mark.asyncio
    async def test_repair_git_pull(self, mock_nerve):
        """git_pull repair action runs git pull origin main."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await mock_nerve._repair_git_pull({})
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "git" in cmd
            assert "pull" in cmd

    @pytest.mark.asyncio
    async def test_repair_kill_zombie_requires_pid(self, mock_nerve):
        """kill_zombie without PID does nothing (safety check)."""
        with patch("os.kill") as mock_kill:
            await mock_nerve._repair_kill_zombie({})
            mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_repair_kill_zombie_with_pid(self, mock_nerve):
        """kill_zombie with valid PID sends SIGKILL."""
        with patch("os.kill") as mock_kill:
            await mock_nerve._repair_kill_zombie({"pid": 12345})
            mock_kill.assert_called_once_with(12345, 9)

    @pytest.mark.asyncio
    async def test_repair_restart_tunnel(self, mock_nerve):
        """restart_tunnel calls launchctl kickstart."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            await mock_nerve._repair_restart_tunnel({})
            cmd = mock_run.call_args[0][0]
            assert "launchctl" in cmd
            assert "kickstart" in cmd


class TestRepairMessageHandler:
    """Incoming repair/sync messages dispatch to safe action handlers."""

    @pytest.mark.asyncio
    async def test_known_action_dispatched(self, mock_nerve):
        """A known repair action (restart_nats) gets dispatched."""
        mock_nerve._repair_restart_nats = AsyncMock()

        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2",
            "type": "repair",
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": "restart_nats",
                    "reporter": "mac2",
                    "broken_channels": ["P1_nats"],
                }),
            },
        }).encode()

        await mock_nerve._handle_message(msg)
        # Give the created task a chance to run
        await asyncio.sleep(0.1)
        mock_nerve._repair_restart_nats.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_action_silently_drops(self, mock_nerve):
        """Unknown repair action is silently dropped (no handler, no alert).

        The repair handler only executes SAFE_ACTIONS. Unknown actions are
        ignored — this prevents arbitrary commands from being run via fleet
        messages. The notification still fires (via _notify) on first sight,
        but _inject_alert is NOT called.
        """
        mock_nerve._inject_alert = MagicMock()
        mock_nerve._notify = MagicMock()
        mock_nerve._find_claude = MagicMock(return_value=None)  # no agent spawning

        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2",
            "type": "sync",
            "payload": {
                "subject": "self-heal-request",
                "body": json.dumps({
                    "action": "format_disk",  # dangerous/unknown
                    "reporter": "mac2",
                }),
            },
        }).encode()

        await mock_nerve._handle_message(msg)
        # Unknown actions do NOT trigger _inject_alert — they are silently
        # dropped after the initial _notify (macOS notification) fires once.
        mock_nerve._inject_alert.assert_not_called()
        mock_nerve._notify.assert_called_once()


class TestRepairEscalation:
    """repair_with_escalation — multi-level escalation chain."""

    @pytest.mark.asyncio
    async def test_l1_notify_sends_context_message(self, mock_nerve):
        """Level 1 sends a context message via fallback chain."""
        mock_nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P1_nats"})
        mock_nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.repair_with_escalation("mac2", ["P1_nats"], action="restart_nats")
        assert result["repaired"] is True
        assert result["level"] == 1
        # Verify send_with_fallback was called with repair message
        # (type:"repair" avoids seed file writes and keystroke injection spam)
        call_args = mock_nerve.send_with_fallback.call_args_list[0]
        msg = call_args[0][1]
        assert msg["type"] == "repair"
        # L1 notify body mentions the broken channels in subject or body
        combined = msg["payload"].get("subject", "") + msg["payload"].get("body", "")
        assert "P1_nats" in combined or "down" in combined

    @pytest.mark.asyncio
    async def test_escalation_stops_on_success(self, mock_nerve):
        """If L1 fixes the problem (P1 probe succeeds), no further escalation."""
        mock_nerve.send_with_fallback = AsyncMock(return_value={"delivered": True, "channel": "P1_nats"})
        mock_nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.repair_with_escalation("mac2", ["P1_nats"])
        assert result["repaired"] is True
        assert result["level"] == 1
        # send_with_fallback should only be called once (L1 notify)
        assert mock_nerve.send_with_fallback.call_count == 1
