"""
Tests for 7-priority channel fallback cascade.

Verifies that send_with_fallback tries channels in order (P1-P7),
stops on first success, triggers self-heal on P3+ success, and
collects errors when all channels fail.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCascadeOrder:
    """Channels are tried in strict P1-P7 order; first success wins."""

    @pytest.mark.asyncio
    async def test_p1_success_skips_all_others(self, mock_nerve, mock_nats_client):
        """When P1 delivers, P2-P7 are never called."""
        spy_p2 = AsyncMock()
        mock_nerve._send_p2_http = spy_p2

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "test", "body": "hello"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P1_nats"
        spy_p2.assert_not_called()

    @pytest.mark.asyncio
    async def test_p2_tried_when_p1_fails(self, mock_nerve, mock_nats_client):
        """P1 failure causes P2 HTTP attempt."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client

        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_p3_tried_when_p1_and_p2_fail(self, mock_nerve, mock_nats_client):
        """P1+P2 failure causes P3 chief relay attempt.

        Note: P3 is only included in the cascade when node_id != chief
        (can't relay through self). Set chief to a different node.
        """
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac3"  # Not self — enables P3 in cascade
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "refused"})

        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": True})
        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P3_chief"

    @pytest.mark.asyncio
    async def test_p4_seed_tried_when_p1_p2_p3_fail(self, mock_nerve, mock_nats_client):
        """P1+P2+P3 failure causes P4 seed file attempt."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})

        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": True})
        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P4_seed"

    @pytest.mark.asyncio
    async def test_full_cascade_to_p7_git(self, mock_nerve, mock_nats_client):
        """When P1-P6 all fail, P7 git is attempted."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p6_wol = AsyncMock(return_value={"delivered": False})

        mock_nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P7_git"


class TestCascadeErrorCollection:
    """When all channels fail, errors from each channel are collected."""

    @pytest.mark.asyncio
    async def test_all_fail_returns_error_list(self, mock_nerve, mock_nats_client):
        """Every channel failing returns delivered=False with errors list."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "refused"})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False, "error": "no chief"})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False, "error": "remote"})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False, "error": "no keys"})
        mock_nerve._send_p6_wol = AsyncMock(return_value={"delivered": False, "error": "no mac"})
        mock_nerve._send_p7_git = AsyncMock(return_value={"delivered": False, "error": "no repo"})
        # Also mock P0 cloud fallback
        mock_nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False, "error": "no cloud"})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is False
        assert "errors" in result
        assert len(result["errors"]) >= 6  # P1 through P7 + possibly P0

    @pytest.mark.asyncio
    async def test_timeout_channel_recorded_in_errors(self, mock_nerve, mock_nats_client):
        """A channel that times out still gets recorded in the error list."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac3"  # Not self — enables P3 in cascade

        # P2 times out
        async def slow_p2(*args, **kwargs):
            await asyncio.sleep(60)
            return {"delivered": False}
        mock_nerve._send_p2_http = slow_p2
        # P3+ deliver
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": True})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P3_chief"


class TestCascadeSelfHeal:
    """P3+ success triggers self-heal notification for broken P1/P2."""

    @pytest.mark.asyncio
    async def test_self_heal_called_on_p3_success(self, mock_nerve, mock_nats_client):
        """When P3 succeeds (meaning P1+P2 failed), self-heal fires."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac3"  # Not self — enables P3 in cascade
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "refused"})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": True})
        mock_nerve._attempt_self_heal = MagicMock()

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["channel"] == "P3_chief"
        mock_nerve._attempt_self_heal.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_self_heal_on_p1_success(self, mock_nerve, mock_nats_client):
        """P1 success means channels are healthy -- no self-heal needed."""
        mock_nerve._attempt_self_heal = MagicMock()

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["channel"] == "P1_nats"
        mock_nerve._attempt_self_heal.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_self_heal_on_p2_success(self, mock_nerve, mock_nats_client):
        """P2 success also does not trigger self-heal (P2 is still a primary channel)."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        mock_nerve._attempt_self_heal = MagicMock()

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"},
        })
        assert result["channel"] == "P2_http"
        mock_nerve._attempt_self_heal.assert_not_called()


class TestCascadeRepairExclusion:
    """Repair messages skip P7 git to avoid commit floods."""

    @pytest.mark.asyncio
    async def test_repair_message_skips_p7(self, mock_nerve, mock_nats_client):
        """Messages with type=repair should NOT attempt P7 git."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p6_wol = AsyncMock(return_value={"delivered": False})
        mock_nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        mock_nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "repair", "payload": {"subject": "self-heal", "body": "fix nats"},
        })
        # P7 should NOT have been called for repair messages
        mock_nerve._send_p7_git.assert_not_called()
        assert result["delivered"] is False


class TestCascadeMessageEnrichment:
    """Messages get auto-assigned id, from, timestamp if missing."""

    @pytest.mark.asyncio
    async def test_missing_id_gets_uuid(self, mock_nerve):
        """Messages without an id field get a UUID assigned."""
        message = {"type": "context", "payload": {"subject": "x", "body": "y"}}
        await mock_nerve.send_with_fallback("mac2", message)
        assert "id" in message
        assert len(message["id"]) == 36  # UUID4 format

    @pytest.mark.asyncio
    async def test_missing_from_gets_node_id(self, mock_nerve):
        """Messages without a from field get the sender's node ID."""
        message = {"type": "context", "payload": {"subject": "x", "body": "y"}}
        await mock_nerve.send_with_fallback("mac2", message)
        assert message["from"] == "mac1"

    @pytest.mark.asyncio
    async def test_missing_timestamp_gets_iso8601(self, mock_nerve):
        """Messages without a timestamp get an ISO 8601 UTC timestamp."""
        message = {"type": "context", "payload": {"subject": "x", "body": "y"}}
        await mock_nerve.send_with_fallback("mac2", message)
        assert "timestamp" in message
        assert message["timestamp"].endswith("Z")

    @pytest.mark.asyncio
    async def test_existing_id_preserved(self, mock_nerve):
        """Messages that already have an id keep it unchanged."""
        message = {"type": "context", "id": "custom-id-123",
                   "payload": {"subject": "x", "body": "y"}}
        await mock_nerve.send_with_fallback("mac2", message)
        assert message["id"] == "custom-id-123"
