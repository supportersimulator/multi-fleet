"""
Tests for send_with_fallback — 7-priority channel ordering, message format, error handling.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSendWithFallback:
    """The 7-priority fallback chain: P1 NATS > P2 HTTP > ... > P7 Git."""

    @pytest.mark.asyncio
    async def test_p1_nats_delivers_first(self, mock_nerve, mock_nats_client):
        """When NATS is connected, P1 delivers and no other channel is tried."""
        message = {"type": "context", "payload": {"subject": "test", "body": "hello"}}
        result = await mock_nerve.send_with_fallback("mac2", message)
        assert result["delivered"] is True
        assert result["channel"] == "P1_nats"
        mock_nats_client.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_to_p2_when_nats_disconnected(self, mock_nerve, mock_nats_client):
        """When NATS is disconnected, P1 fails and P2 HTTP is attempted."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client

        # Mock P2 HTTP to succeed
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"status": "ok"}'
            mock_urlopen.return_value = mock_resp

            result = await mock_nerve.send_with_fallback("mac2", {
                "type": "context", "payload": {"subject": "test", "body": "hello"}
            })
            assert result["delivered"] is True
            assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_message_gets_id_if_missing(self, mock_nerve):
        """Messages without an ID get one auto-assigned."""
        message = {"type": "context", "payload": {"subject": "test", "body": "hello"}}
        assert "id" not in message
        await mock_nerve.send_with_fallback("mac2", message)
        assert "id" in message
        assert len(message["id"]) == 36  # UUID format

    @pytest.mark.asyncio
    async def test_message_gets_timestamp_if_missing(self, mock_nerve):
        """Messages without a timestamp get one auto-assigned."""
        message = {"type": "context", "payload": {"subject": "test", "body": "hello"}}
        await mock_nerve.send_with_fallback("mac2", message)
        assert "timestamp" in message
        assert message["timestamp"].endswith("Z")

    @pytest.mark.asyncio
    async def test_all_channels_fail_returns_errors(self, mock_nerve, mock_nats_client):
        """When all 7 channels fail, result contains error list."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._peers_config = {}  # No peer info = P2-P6 can't resolve target
        mock_nerve._chief = None

        with patch("subprocess.run", side_effect=Exception("no ssh")):
            with patch("urllib.request.urlopen", side_effect=Exception("no http")):
                result = await mock_nerve.send_with_fallback("mac2", {
                    "type": "context", "payload": {"subject": "x", "body": "y"}
                })
                assert result["delivered"] is False
                assert "errors" in result


class TestSendDirect:
    """Direct send via NATS pub/sub."""

    @pytest.mark.asyncio
    async def test_send_to_specific_node(self, mock_nerve, mock_nats_client):
        """Sending to a specific node publishes to fleet.<node>.message."""
        await mock_nerve.send("mac2", "context", "test subject", "test body")
        call_args = mock_nats_client.publish.call_args
        assert call_args[0][0] == "fleet.mac2.message"
        # Verify payload structure
        payload = json.loads(call_args[0][1].decode())
        assert payload["type"] == "context"
        assert payload["from"] == "mac1"
        assert payload["to"] == "mac2"
        assert payload["payload"]["subject"] == "test subject"

    @pytest.mark.asyncio
    async def test_send_broadcast(self, mock_nerve, mock_nats_client):
        """Sending to 'all' publishes to fleet.all.message."""
        await mock_nerve.send("all", "announcement", "hello fleet", "broadcast test")
        call_args = mock_nats_client.publish.call_args
        assert call_args[0][0] == "fleet.all.message"

    @pytest.mark.asyncio
    async def test_send_increments_stats(self, mock_nerve, mock_nats_client):
        """Each send increments the sent counter."""
        initial = mock_nerve._stats["sent"]
        await mock_nerve.send("mac2", "context", "test", "body")
        assert mock_nerve._stats["sent"] == initial + 1

    @pytest.mark.asyncio
    async def test_broadcast_increments_broadcast_stat(self, mock_nerve, mock_nats_client):
        """Broadcast sends increment both sent and broadcasts counters."""
        initial_bc = mock_nerve._stats["broadcasts"]
        await mock_nerve.send("all", "context", "test", "body")
        assert mock_nerve._stats["broadcasts"] == initial_bc + 1


class TestP4SeedFile:
    """P4 seed file delivery — local file write."""

    @pytest.mark.asyncio
    async def test_seed_local_writes_file(self, mock_nerve):
        """P4 writes a seed file to /tmp for local delivery."""
        import pathlib
        mock_nerve.node_id = "testnode"
        seed_path = pathlib.Path("/tmp/fleet-seed-testnode.md")
        # Clean up before test
        seed_path.unlink(missing_ok=True)

        result = await mock_nerve._send_p4_seed("testnode", {
            "from": "mac2",
            "payload": {"subject": "test seed", "body": "seed body"},
        })
        assert result["delivered"] is True
        assert seed_path.exists()
        content = seed_path.read_text()
        assert "test seed" in content
        # Clean up
        seed_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_seed_remote_returns_false(self, mock_nerve):
        """P4 cannot deliver remotely (falls through to P5 SSH)."""
        result = await mock_nerve._send_p4_seed("mac2", {
            "from": "mac1",
            "payload": {"subject": "x", "body": "y"},
        })
        assert result["delivered"] is False
