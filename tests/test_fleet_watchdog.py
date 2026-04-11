"""
Tests for watchdog logic — peer staleness, health building, self-heal triggers.
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestPeerStaleness:
    """Heartbeat tracking and stale peer detection."""

    @pytest.mark.asyncio
    async def test_heartbeat_updates_peer_cache(self, mock_nerve):
        """Incoming heartbeat from another node updates _peers cache."""
        msg = MagicMock()
        msg.data = json.dumps({
            "node": "mac2",
            "ip": "TEST_IP_2",
            "status": "ok",
        }).encode()

        await mock_nerve._handle_heartbeat(msg)
        assert "mac2" in mock_nerve._peers
        assert mock_nerve._peers["mac2"]["status"] == "ok"
        assert "_received_at" in mock_nerve._peers["mac2"]

    @pytest.mark.asyncio
    async def test_own_heartbeat_ignored(self, mock_nerve):
        """Heartbeats from self are not stored in peer cache."""
        msg = MagicMock()
        msg.data = json.dumps({"node": "mac1"}).encode()  # mac1 = self

        await mock_nerve._handle_heartbeat(msg)
        assert "mac1" not in mock_nerve._peers

    def test_stale_peer_detection_via_health(self, mock_nerve):
        """Peers in _peers with old _received_at show high lastSeen in health."""
        mock_nerve._peers = {
            "mac2": {"node": "mac2", "status": "ok", "_received_at": time.time() - 120},
            "mac3": {"node": "mac3", "status": "ok", "_received_at": time.time() - 5},
        }

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
            health = mock_nerve._build_health()

        peers_health = health["peers"]
        assert peers_health["mac2"]["lastSeen"] >= 119  # ~120s ago
        assert peers_health["mac3"]["lastSeen"] <= 6     # ~5s ago

    @pytest.mark.asyncio
    async def test_malformed_heartbeat_ignored(self, mock_nerve):
        """Malformed heartbeat data doesn't crash the handler."""
        msg = MagicMock()
        msg.data = b"not json"

        await mock_nerve._handle_heartbeat(msg)
        assert len(mock_nerve._peers) == 0  # no crash, no data stored


class TestHealthBuilding:
    """_build_health() returns comprehensive node status."""

    def test_health_contains_node_id(self, mock_nerve):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
            health = mock_nerve._build_health()
        assert health["nodeId"] == "mac1"
        assert health["status"] == "ok"
        assert health["transport"] == "nats"

    def test_health_contains_stats(self, mock_nerve):
        mock_nerve._stats["sent"] = 42
        mock_nerve._stats["received"] = 13

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
            health = mock_nerve._build_health()

        assert health["stats"]["sent"] == 42
        assert health["stats"]["received"] == 13

    def test_health_includes_uptime(self, mock_nerve):
        mock_nerve._stats["started_at"] = time.time() - 3600

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
            health = mock_nerve._build_health()

        assert health["uptime_s"] >= 3599


class TestSelfHealTrigger:
    """When send_with_fallback succeeds on P3+, self-heal notification fires."""

    @pytest.mark.asyncio
    async def test_self_heal_triggered_on_p3_plus(self, mock_nerve, mock_nats_client):
        """Successful delivery on P3+ triggers _attempt_self_heal."""
        mock_nats_client.is_connected = False  # P1 fails
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac3"  # Not self — enables P3 in cascade
        mock_nerve._attempt_self_heal = MagicMock()

        # P2 HTTP also fails
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "refused"})
        # P3 chief succeeds
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": True, "url": "http://chief:8844/message"})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"}
        })

        assert result["delivered"] is True
        assert result["channel"] == "P3_chief"
        # self-heal should have been triggered (called synchronously, not awaited)
        mock_nerve._attempt_self_heal.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_self_heal_on_p1_p2(self, mock_nerve, mock_nats_client):
        """P1/P2 success does NOT trigger self-heal (channels are healthy)."""
        mock_nerve._attempt_self_heal = AsyncMock()

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "x", "body": "y"}
        })
        assert result["channel"] == "P1_nats"
        await asyncio.sleep(0.1)
        mock_nerve._attempt_self_heal.assert_not_called()
