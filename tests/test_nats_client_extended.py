"""
Extended tests for multifleet/nats_client.py — covering untested areas.

Focus areas:
  - _sanitize_for_git (secret/IP redaction)
  - P2 HTTP send (direct + tunnel)
  - P3 Chief relay
  - P4 Seed file (local + NATS)
  - P5 SSH (tunnel fallback)
  - P7 Git (rate limiting integration, queuing)
  - P0 Discord / P0 Cloud channels
  - send_with_fallback (budget exhaustion, cloud fallback, ACK tracking, store)
  - _send_broadcast (git fallback, cloud fallback, rate limiting)
  - _handle_message (repair routing, chain_complete, seed_write, fleet_assist)
  - _build_health structure
  - _dispatch_chain_step (local vs remote)
  - _touch_activity
  - broadcast_health
  - _on_error / _on_disconnect callbacks

All network calls mocked. Tests verify LOGIC, not infrastructure.
"""

import asyncio
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock, call

import pytest


# ── Helpers ──

def _make_nerve(node_id="mac1", nats_url="nats://localhost:4222"):
    """Build a FleetNerveNATS with all external deps mocked."""
    with patch.dict(os.environ, {
        "MULTIFLEET_NODE_ID": node_id,
        "NATS_URL": nats_url,
    }):
        with patch("multifleet.nats_client.FleetDiscoveryManager"), \
             patch("multifleet.nats_client.SelfHealManager"), \
             patch("multifleet.nats_client.CommunicationProtocol"), \
             patch("multifleet.nats_client.SessionManager"):
            from multifleet.nats_client import FleetNerveNATS
            nerve = FleetNerveNATS(node_id=node_id, nats_url=nats_url)
            nc = AsyncMock()
            nc.is_connected = True
            nc.publish = AsyncMock()
            nerve.nc = nc
            nerve._peers_config = {
                "mac2": {"user": "bob", "ip": "10.0.0.2", "port": 8855,
                         "mac_address": "AA:BB:CC:DD:EE:02", "tunnel_port": 9002},
                "mac3": {"user": "carol", "ip": "10.0.0.3", "port": 8855,
                         "mac_address": "AA:BB:CC:DD:EE:03"},
            }
            nerve._chief = "mac1"
            nerve._store = None
            return nerve


# ═══════════════════════════════════════════════════════════════════
# 1. _sanitize_for_git
# ═══════════════════════════════════════════════════════════════════

class TestSanitizeForGit:
    """Secret and IP redaction for git-committed payloads."""

    def test_redacts_secret_keys(self):
        from multifleet.nats_client import _sanitize_for_git
        obj = {"api_key": "sk-abc123", "token": "tok-xyz", "name": "safe"}
        result = _sanitize_for_git(obj)
        assert result["api_key"] == "***"
        assert result["token"] == "***"
        assert result["name"] == "safe"

    def test_redacts_ip_addresses(self):
        from multifleet.nats_client import _sanitize_for_git
        obj = {"msg": "connected to 192.168.1.100 on port 8855"}
        result = _sanitize_for_git(obj)
        assert "192.168.1.100" not in result["msg"]
        assert "<redacted-ip>" in result["msg"]

    def test_preserves_fleet_fields(self):
        from multifleet.nats_client import _sanitize_for_git
        obj = {"id": "abc", "type": "context", "timestamp": "2026-01-01T00:00:00Z",
               "node_id": "mac1", "subject": "hello"}
        result = _sanitize_for_git(obj)
        assert result["id"] == "abc"
        assert result["type"] == "context"
        assert result["node_id"] == "mac1"

    def test_handles_nested_dicts(self):
        from multifleet.nats_client import _sanitize_for_git
        obj = {"payload": {"password": "hunter2", "body": "ok"}}
        result = _sanitize_for_git(obj)
        assert result["payload"]["password"] == "***"
        assert result["payload"]["body"] == "ok"

    def test_handles_lists(self):
        from multifleet.nats_client import _sanitize_for_git
        obj = {"items": [{"secret": "x"}, {"name": "y"}]}
        result = _sanitize_for_git(obj)
        assert result["items"][0]["secret"] == "***"
        assert result["items"][1]["name"] == "y"

    def test_depth_limit(self):
        from multifleet.nats_client import _sanitize_for_git
        # Build deeply nested structure (>10 levels)
        obj = {"a": "leaf"}
        for _ in range(12):
            obj = {"nested": obj}
        result = _sanitize_for_git(obj)
        # Should not crash; deepest level becomes "..."
        assert result is not None

    def test_non_dict_non_list_passthrough(self):
        from multifleet.nats_client import _sanitize_for_git
        assert _sanitize_for_git(42) == 42
        assert _sanitize_for_git(True) is True
        assert _sanitize_for_git(None) is None


# ═══════════════════════════════════════════════════════════════════
# 2. P2 HTTP send
# ═══════════════════════════════════════════════════════════════════

class TestP2HTTP:
    """HTTP direct send with direct IP + tunnel fallback."""

    @pytest.mark.asyncio
    async def test_p2_http_no_ip(self):
        nerve = _make_nerve()
        result = await nerve._send_p2_http("mac99", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_p2_http_direct_success(self):
        nerve = _make_nerve()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock(status=200)
            result = await nerve._send_p2_http("mac2", {"id": "1"})
            assert result["delivered"] is True
            assert "10.0.0.2" in result["url"]

    @pytest.mark.asyncio
    async def test_p2_http_tunnel_fallback(self):
        nerve = _make_nerve()
        with patch("urllib.request.urlopen") as mock_open:
            # Direct fails, tunnel succeeds
            mock_open.side_effect = [Exception("refused"), MagicMock(status=200)]
            result = await nerve._send_p2_http("mac2", {"id": "1"})
            assert result["delivered"] is True
            assert "9002" in result["url"]

    @pytest.mark.asyncio
    async def test_p2_http_all_fail(self):
        nerve = _make_nerve()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = Exception("refused")
            result = await nerve._send_p2_http("mac2", {"id": "1"})
            assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 3. P3 Chief relay
# ═══════════════════════════════════════════════════════════════════

class TestP3Chief:
    """Chief relay channel."""

    @pytest.mark.asyncio
    async def test_p3_no_chief(self):
        nerve = _make_nerve()
        nerve._chief = None
        result = await nerve._send_p3_chief("mac2", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_p3_self_is_chief(self):
        nerve = _make_nerve()
        nerve._chief = "mac1"  # self
        result = await nerve._send_p3_chief("mac2", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_p3_chief_success(self):
        nerve = _make_nerve()
        nerve._chief = "mac2"
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock(status=200)
            result = await nerve._send_p3_chief("mac3", {"id": "1", "payload": {"subject": "hi", "body": "test"}})
            assert result["delivered"] is True

    @pytest.mark.asyncio
    async def test_p3_chief_all_urls_fail(self):
        nerve = _make_nerve()
        nerve._chief = "mac2"
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = Exception("refused")
            result = await nerve._send_p3_chief("mac3", {"id": "1", "payload": {}})
            assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 4. P4 Seed file
# ═══════════════════════════════════════════════════════════════════

class TestP4Seed:
    """Seed file write -- local and NATS-based remote."""

    @pytest.mark.asyncio
    async def test_p4_local_self(self):
        nerve = _make_nerve()
        nerve._resolve_seed_path = MagicMock(return_value="/tmp/test-seed-p4.md")
        msg = {"from": "mac2", "payload": {"subject": "test", "body": "seed body"}}
        with patch.object(Path, "write_text") as mock_write:
            result = await nerve._send_p4_seed("mac1", msg)
            assert result["delivered"] is True
            mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_p4_remote_via_nats(self):
        nerve = _make_nerve()
        msg = {"from": "mac1", "payload": {"subject": "test", "body": "hello"}}
        result = await nerve._send_p4_seed("mac2", msg)
        assert result["delivered"] is True
        assert result.get("method") == "seed_via_nats"
        nerve.nc.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_p4_remote_nats_disconnected(self):
        nerve = _make_nerve()
        nerve.nc.is_connected = False
        msg = {"from": "mac1", "payload": {"subject": "test", "body": "hello"}}
        result = await nerve._send_p4_seed("mac2", msg)
        assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 5. P0 Discord
# ═══════════════════════════════════════════════════════════════════

class TestP0Discord:
    """Discord channel tests."""

    @pytest.mark.asyncio
    async def test_discord_no_bridge_import(self):
        nerve = _make_nerve()
        with patch.dict("sys.modules", {"multifleet.discord_bridge": None}):
            # Force ImportError by patching the import mechanism
            result = await nerve._send_p0_discord("mac2", {"id": "1"})
            # Either error or not delivered
            assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_discord_no_bridge_attached(self):
        nerve = _make_nerve()
        # discord_bridge module exists but bridge not attached to nerve
        with patch("multifleet.nats_client.FleetNerveNATS._send_p0_discord", wraps=nerve._send_p0_discord):
            # No _discord_bridge attribute
            if hasattr(nerve, "_discord_bridge"):
                delattr(nerve, "_discord_bridge")
            result = await nerve._send_p0_discord("mac2", {"id": "1"})
            assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_discord_bridge_sends(self):
        nerve = _make_nerve()
        bridge = MagicMock()
        bridge.send_to_discord.return_value = {"sent": True}
        nerve._discord_bridge = bridge
        # Need to patch the import inside the method
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"multifleet.discord_bridge": mock_module}):
            result = await nerve._send_p0_discord("mac2", {"id": "1"})
            assert result["delivered"] is True

    @pytest.mark.asyncio
    async def test_discord_bridge_queued(self):
        nerve = _make_nerve()
        bridge = MagicMock()
        bridge.send_to_discord.return_value = {"queued": True}
        nerve._discord_bridge = bridge
        mock_module = MagicMock()
        with patch.dict("sys.modules", {"multifleet.discord_bridge": mock_module}):
            result = await nerve._send_p0_discord("mac2", {"id": "1"})
            assert result["delivered"] is False
            assert "queued" in result.get("error", "")


# ═══════════════════════════════════════════════════════════════════
# 6. P0 Cloud
# ═══════════════════════════════════════════════════════════════════

class TestP0Cloud:
    """Cloud dispatch channel."""

    @pytest.mark.asyncio
    async def test_cloud_import_fails(self):
        nerve = _make_nerve()
        with patch.dict("sys.modules", {"multifleet.cloud": None}):
            result = await nerve._send_p0_cloud("mac2", {"id": "1", "payload": {"body": "test"}})
            assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_cloud_sends_body(self):
        nerve = _make_nerve()
        mock_cloud = MagicMock()
        mock_send = AsyncMock(return_value={"delivered": True})
        mock_cloud.send_cloud = mock_send
        with patch.dict("sys.modules", {"multifleet.cloud": mock_cloud}):
            result = await nerve._send_p0_cloud("mac2", {"id": "1", "payload": {"body": "do stuff"}})
            assert result["delivered"] is True
            mock_send.assert_called_once()
            assert mock_send.call_args.kwargs["prompt"] == "do stuff"

    @pytest.mark.asyncio
    async def test_cloud_empty_body_uses_json(self):
        nerve = _make_nerve()
        mock_cloud = MagicMock()
        mock_send = AsyncMock(return_value={"delivered": True})
        mock_cloud.send_cloud = mock_send
        with patch.dict("sys.modules", {"multifleet.cloud": mock_cloud}):
            result = await nerve._send_p0_cloud("mac2", {"id": "1", "payload": {"subject": "hi"}})
            assert result["delivered"] is True
            # Body was empty, should fallback to JSON of payload
            prompt = mock_send.call_args.kwargs["prompt"]
            assert "hi" in prompt


# ═══════════════════════════════════════════════════════════════════
# 7. P7 Git (rate limiting + queue)
# ═══════════════════════════════════════════════════════════════════

class TestP7GitExtended:
    """P7 Git send with rate limiting and queue integration."""

    @pytest.mark.asyncio
    async def test_p7_rate_limited_queues_to_store(self):
        nerve = _make_nerve()
        nerve._p7_last_sent["mac2"] = time.time()  # recent send -> rate limited
        store = MagicMock()
        store.get_ack_status.return_value = None
        nerve._store = store
        result = await nerve._send_p7_git("mac2", {"id": "m1", "payload": {}})
        assert result["delivered"] is False
        assert result.get("rate_limited") is True
        store.store_message.assert_called_once()
        store.queue_outbox.assert_called_once_with("m1", "mac2")

    @pytest.mark.asyncio
    async def test_p7_success_records_sent(self):
        nerve = _make_nerve()
        with patch("subprocess.run") as mock_run, \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"):
            mock_run.return_value = MagicMock(returncode=0)
            result = await nerve._send_p7_git("mac2", {"id": "m1", "payload": {"subject": "x", "body": "y"}})
            assert result["delivered"] is True
            assert "mac2" in nerve._p7_last_sent

    @pytest.mark.asyncio
    async def test_p7_subprocess_exception(self):
        nerve = _make_nerve()
        with patch("subprocess.run", side_effect=Exception("git error")), \
             patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"):
            result = await nerve._send_p7_git("mac2", {"id": "m1", "payload": {}})
            assert result["delivered"] is False

    def test_p7_queue_no_store(self):
        nerve = _make_nerve()
        nerve._store = None
        # Should not crash
        nerve._p7_queue_for_retry("mac2", {"id": "m1"}, "reason")


# ═══════════════════════════════════════════════════════════════════
# 8. send_with_fallback -- extended scenarios
# ═══════════════════════════════════════════════════════════════════

class TestSendWithFallbackExtended:
    """Extended fallback scenarios: store, ACK, cloud last resort."""

    @pytest.mark.asyncio
    async def test_stores_message_before_send(self):
        nerve = _make_nerve()
        store = MagicMock()
        store.get_ack_status.return_value = None
        nerve._store = store
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"type": "context", "payload": {"subject": "t", "body": "b"}}
        await nerve.send_with_fallback("mac2", msg)
        store.store_message.assert_called_once()
        store.update_ack_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_store_for_heartbeat(self):
        nerve = _make_nerve()
        store = MagicMock()
        nerve._store = store
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"type": "heartbeat", "payload": {}}
        await nerve.send_with_fallback("mac2", msg)
        store.store_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_fallback_when_all_fail(self):
        nerve = _make_nerve()
        # Make all channels fail
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})
        nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False})
        nerve._send_p6_wol = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": False})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        nerve._attempt_self_heal = MagicMock()
        msg = {"id": "m1", "type": "context", "from": "mac1", "payload": {"subject": "t"}}
        result = await nerve.send_with_fallback("mac2", msg)
        assert result["delivered"] is True
        assert result["channel"] == "P0_cloud"

    @pytest.mark.asyncio
    async def test_ack_tracking_on_success(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "track-1", "type": "context", "from": "mac1", "payload": {}}
        await nerve.send_with_fallback("mac2", msg)
        assert "track-1" in nerve._pending_acks
        assert nerve._pending_acks["track-1"]["target"] == "mac2"
        assert nerve._pending_acks["track-1"]["acked"] is False

    @pytest.mark.asyncio
    async def test_use_cloud_flag_prepends_p0(self):
        nerve = _make_nerve()
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "c1", "type": "context", "from": "mac1", "payload": {}}
        result = await nerve.send_with_fallback("mac2", msg, use_cloud=True)
        assert result["delivered"] is True
        assert result["channel"] == "P0_cloud"

    @pytest.mark.asyncio
    async def test_all_channels_fail_returns_errors(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})
        nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False})
        nerve._send_p6_wol = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": False})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        nerve._attempt_self_heal = MagicMock()
        msg = {"id": "f1", "type": "context", "from": "mac1", "payload": {}}
        result = await nerve.send_with_fallback("mac2", msg)
        assert result["delivered"] is False
        assert "errors" in result

    @pytest.mark.asyncio
    async def test_exception_in_channel_continues(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(side_effect=RuntimeError("boom"))
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "e1", "type": "context", "from": "mac1", "payload": {}}
        result = await nerve.send_with_fallback("mac2", msg)
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"


# ═══════════════════════════════════════════════════════════════════
# 9. _send_broadcast -- extended scenarios
# ═══════════════════════════════════════════════════════════════════

class TestBroadcastExtended:
    """Broadcast path: git fallback and cloud last resort."""

    @pytest.mark.asyncio
    async def test_broadcast_git_fallback(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        nerve._peers = {"mac2": {"_received_at": time.time()}}
        nerve._last_git_broadcast_ts = 0  # not rate limited
        result = await nerve._send_broadcast({"id": "bg1", "to": "all", "type": "context", "payload": {}})
        assert result["delivered"] is True
        assert result["mode"] == "broadcast_git_fallback"

    @pytest.mark.asyncio
    async def test_broadcast_git_rate_limited(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        nerve._peers = {"mac2": {"_received_at": time.time()}}
        nerve._last_git_broadcast_ts = time.time()  # recent -> rate limited
        result = await nerve._send_broadcast({"id": "bg2", "to": "all", "type": "context", "payload": {}})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_broadcast_cloud_last_resort(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": False})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        nerve._peers = {"mac2": {"_received_at": time.time()}}
        nerve._last_git_broadcast_ts = 0
        result = await nerve._send_broadcast({"id": "bc1", "to": "all", "type": "context", "payload": {}})
        assert result["delivered"] is True
        assert result["mode"] == "broadcast_cloud_fallback"

    @pytest.mark.asyncio
    async def test_broadcast_repair_skips_git(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        nerve._peers = {"mac2": {"_received_at": time.time()}}
        nerve._last_git_broadcast_ts = 0
        result = await nerve._send_broadcast({"id": "br1", "to": "all", "type": "repair",
                                              "payload": {"subject": "self-heal-request"}})
        nerve._send_p7_git.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 10. _handle_message -- repair, chain_complete, seed_write
# ═══════════════════════════════════════════════════════════════════

class TestHandleMessageExtended:
    """Extended message routing: repair, chain_complete, seed_write, fleet_assist."""

    @pytest.mark.asyncio
    async def test_repair_first_time_notifies(self):
        nerve = _make_nerve()
        nerve._touch_activity = MagicMock()
        nerve._notify = MagicMock()
        nerve._repair_restart_nats = AsyncMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2", "type": "repair",
            "payload": {"subject": "self-heal-request",
                        "body": json.dumps({"action": "restart_nats", "reporter": "mac2", "broken_channels": ["P1_nats"]})},
        }).encode()
        await nerve._handle_message(msg)
        nerve._notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_repair_cooldown_drops_silently(self):
        nerve = _make_nerve()
        nerve._touch_activity = MagicMock()
        nerve._notify = MagicMock()
        nerve._repair_cooldowns = {"mac2:restart_nats": time.time()}
        nerve._repair_notified = {"mac2:restart_nats": time.time()}
        nerve._repair_agents_spawned = {}
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2", "type": "repair",
            "payload": {"subject": "self-heal-request",
                        "body": json.dumps({"action": "restart_nats", "reporter": "mac2"})},
        }).encode()
        await nerve._handle_message(msg)
        nerve._notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_chain_complete_advances(self):
        nerve = _make_nerve()
        nerve._touch_activity = MagicMock()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}, {"task": "b"}],
            "current": 0, "status": "running", "results": [],
        }
        nerve._dispatch_chain_step = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2", "type": "chain_complete",
            "_chain_id": "c1",
            "payload": {"success": True, "result": "done"},
        }).encode()
        await nerve._handle_message(msg)
        assert nerve._chains["c1"]["current"] == 1

    @pytest.mark.asyncio
    async def test_seed_write_creates_file(self):
        nerve = _make_nerve()
        nerve._touch_activity = MagicMock()
        nerve._resolve_seed_path = MagicMock(return_value="/tmp/test-seed.md")
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2", "type": "seed_write",
            "payload": {"subject": "seed test", "body": "seed content"},
        }).encode()
        with patch.object(Path, "write_text") as mock_write:
            await nerve._handle_message(msg)
            mock_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_fleet_assist_dispatched(self):
        nerve = _make_nerve()
        nerve._touch_activity = MagicMock()
        nerve._healer = MagicMock()
        nerve._healer._handle_fleet_assist = AsyncMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.fleet_assist"
        msg.reply = None
        msg.data = json.dumps({
            "from": "mac2",
            "payload": {"subject": "assist-request", "body": "help"},
        }).encode()
        await nerve._handle_message(msg)
        # fleet_assist is routed by msg_type, which comes from subject parts


# ═══════════════════════════════════════════════════════════════════
# 11. _build_health structure
# ═══════════════════════════════════════════════════════════════════

class TestBuildHealth:
    """Health response structure validation."""

    def test_health_has_required_keys(self):
        nerve = _make_nerve()
        nerve._detect_vscode_state = MagicMock(return_value="running")
        nerve._detect_sessions = MagicMock(return_value=[])
        nerve._get_git_context = MagicMock(return_value={"branch": "main", "commit": "abc"})
        nerve._probe_surgeons = MagicMock(return_value={"neurologist": "ok"})
        nerve._get_peer_health_score = MagicMock(return_value=(5, []))
        with patch("multifleet.protocol.PROTOCOL_VERSION", "1.0"):
            health = nerve._build_health()
        assert health["nodeId"] == "mac1"
        assert health["status"] == "ok"
        assert health["transport"] == "nats"
        assert "uptime_s" in health
        assert "stats" in health
        assert "peers" in health
        assert "git" in health

    def test_health_includes_peer_info(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {"_received_at": time.time(), "sessions": 2, "vscode": "running"}
        nerve._detect_vscode_state = MagicMock(return_value="running")
        nerve._detect_sessions = MagicMock(return_value=[])
        nerve._get_git_context = MagicMock(return_value={})
        nerve._probe_surgeons = MagicMock(return_value={})
        nerve._get_peer_health_score = MagicMock(return_value=(5, []))
        with patch("multifleet.protocol.PROTOCOL_VERSION", "1.0"):
            health = nerve._build_health()
        assert "mac2" in health["peers"]
        assert health["peers"]["mac2"]["transport"] == "nats"


# ═══════════════════════════════════════════════════════════════════
# 12. _dispatch_chain_step
# ═══════════════════════════════════════════════════════════════════

class TestDispatchChainStep:
    """Chain step dispatch: local vs remote."""

    def test_dispatch_local_step_injects_task(self):
        nerve = _make_nerve()
        nerve._inject_task = MagicMock()
        nerve._chains["c1"] = {
            "steps": [{"node": "mac1", "task": "local work"}],
            "current": 0, "status": "running", "results": [],
        }
        nerve._dispatch_chain_step("c1")
        nerve._inject_task.assert_called_once()

    def test_dispatch_remote_step_uses_loop(self):
        nerve = _make_nerve()
        nerve._loop = MagicMock()
        nerve._chains["c1"] = {
            "steps": [{"node": "mac2", "task": "remote work"}],
            "current": 0, "status": "running", "results": [],
        }
        with patch("asyncio.run_coroutine_threadsafe") as mock_rcts:
            nerve._dispatch_chain_step("c1")
            mock_rcts.assert_called_once()

    def test_dispatch_past_end_marks_done(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}],
            "current": 1,  # past end
            "status": "running", "results": [],
        }
        nerve._dispatch_chain_step("c1")
        assert nerve._chains["c1"]["status"] == "done"

    def test_dispatch_nonexistent_chain_noop(self):
        nerve = _make_nerve()
        nerve._dispatch_chain_step("nonexistent")  # should not crash


# ═══════════════════════════════════════════════════════════════════
# 13. _touch_activity
# ═══════════════════════════════════════════════════════════════════

class TestTouchActivity:
    """Activity tracking for watchdog."""

    def test_touch_updates_timestamp(self):
        nerve = _make_nerve()
        nerve._last_activity_at = 0
        nerve._touch_activity()
        assert nerve._last_activity_at > 0

    def test_touch_no_attr_noop(self):
        nerve = _make_nerve()
        # Remove the attribute if present
        if hasattr(nerve, "_last_activity_at"):
            delattr(nerve, "_last_activity_at")
        nerve._touch_activity()  # should not crash


# ═══════════════════════════════════════════════════════════════════
# 14. Callbacks (_on_error, _on_disconnect)
# ═══════════════════════════════════════════════════════════════════

class TestCallbacks:
    """NATS connection callbacks."""

    @pytest.mark.asyncio
    async def test_on_error_increments_stats(self):
        nerve = _make_nerve()
        initial = nerve._stats["errors"]
        await nerve._on_error(RuntimeError("test error"))
        assert nerve._stats["errors"] == initial + 1

    @pytest.mark.asyncio
    async def test_on_disconnect_no_crash(self):
        nerve = _make_nerve()
        await nerve._on_disconnect()  # should not raise

    @pytest.mark.asyncio
    async def test_on_reconnect_resubscribes(self):
        nerve = _make_nerve()
        nerve._subscribe_all = AsyncMock()
        await nerve._on_reconnect()
        nerve._subscribe_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_reconnect_failure_increments_errors(self):
        nerve = _make_nerve()
        nerve._subscribe_all = AsyncMock(side_effect=RuntimeError("sub failed"))
        initial = nerve._stats["errors"]
        await nerve._on_reconnect()
        assert nerve._stats["errors"] == initial + 1


# ═══════════════════════════════════════════════════════════════════
# 15. P5 SSH -- tunnel fallback
# ═══════════════════════════════════════════════════════════════════

class TestP5SSHExtended:
    """SSH channel tunnel port fallback."""

    @pytest.mark.asyncio
    async def test_ssh_direct_success(self):
        nerve = _make_nerve()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await nerve._send_p5_ssh("mac2", {"id": "1"})
            assert result["delivered"] is True
            assert result["method"] == "ssh_direct"

    @pytest.mark.asyncio
    async def test_ssh_tunnel_fallback(self):
        nerve = _make_nerve()
        with patch("subprocess.run", side_effect=Exception("ssh fail")), \
             patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock(status=200)
            result = await nerve._send_p5_ssh("mac2", {"id": "1"})
            assert result["delivered"] is True
            assert result["method"] == "ssh_tunnel"

    @pytest.mark.asyncio
    async def test_ssh_both_fail(self):
        nerve = _make_nerve()
        with patch("subprocess.run", side_effect=Exception("ssh fail")):
            # mac3 has no tunnel_port, so only SSH is tried
            result = await nerve._send_p5_ssh("mac3", {"id": "1"})
            assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 16. P2t Tailscale
# ═══════════════════════════════════════════════════════════════════

class TestP2tTailscale:
    """Tailscale overlay network channel."""

    @pytest.mark.asyncio
    async def test_tailscale_no_ip(self):
        nerve = _make_nerve()
        result = await nerve._send_p2t_tailscale("mac2", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_tailscale_success(self):
        nerve = _make_nerve()
        nerve._peers_config["mac2"]["tailscale_ip"] = "100.64.0.2"
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value = MagicMock(status=200)
            result = await nerve._send_p2t_tailscale("mac2", {"id": "1"})
            assert result["delivered"] is True
            assert "100.64.0.2" in result["url"]

    @pytest.mark.asyncio
    async def test_tailscale_connection_error(self):
        nerve = _make_nerve()
        nerve._peers_config["mac2"]["tailscale_ip"] = "100.64.0.2"
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = await nerve._send_p2t_tailscale("mac2", {"id": "1"})
            assert result["delivered"] is False
