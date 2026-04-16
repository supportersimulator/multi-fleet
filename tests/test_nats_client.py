"""
Tests for multifleet/nats_client.py — the P1 NATS communication channel.

Covers: subject routing, message serialization, channel cascade logic,
P7 rate limiting, chain orchestration, memory cleanup, health building,
ACK tracking, WoL magic packet, peer resolution, send_with_fallback,
broadcast routing, send_smart session routing, and repair handling.

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
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest


# ── Helpers to build a minimal FleetNerveNATS without real infra ──

def _make_nerve(node_id="mac1", nats_url="nats://localhost:4222"):
    """Build a FleetNerveNATS with all external deps mocked."""
    with patch.dict(os.environ, {
        "MULTIFLEET_NODE_ID": node_id,
        "NATS_URL": nats_url,
    }):
        # Mock heavy optional modules that fail without real infra
        with patch("multifleet.nats_client.FleetDiscoveryManager"), \
             patch("multifleet.nats_client.SelfHealManager") as mock_heal_cls, \
             patch("multifleet.nats_client.CommunicationProtocol"), \
             patch("multifleet.nats_client.SessionManager"):
            from multifleet.nats_client import FleetNerveNATS
            nerve = FleetNerveNATS(node_id=node_id, nats_url=nats_url)
            # Wire a mock NATS client
            nc = AsyncMock()
            nc.is_connected = True
            nc.publish = AsyncMock()
            nerve.nc = nc
            nerve._peers_config = {
                "mac2": {"user": "bob", "ip": "10.0.0.2", "port": 8855, "mac_address": "AA:BB:CC:DD:EE:02"},
                "mac3": {"user": "carol", "ip": "10.0.0.3", "port": 8855, "mac_address": "AA:BB:CC:DD:EE:03"},
            }
            nerve._chief = "mac1"
            nerve._store = None
            return nerve


# ═══════════════════════════════════════════════════════════════════
# 1. Subject routing helpers
# ═══════════════════════════════════════════════════════════════════

class TestSubjectRouting:
    """NATS subject string generation."""

    def test_sub_direct(self):
        from multifleet.nats_client import _sub_direct
        assert _sub_direct("mac2") == "fleet.mac2.>"

    def test_sub_all(self):
        from multifleet.nats_client import _sub_all
        assert _sub_all() == "fleet.all.>"

    def test_sub_heartbeat(self):
        from multifleet.nats_client import _sub_heartbeat
        assert _sub_heartbeat() == "fleet.heartbeat"

    def test_sub_ack(self):
        from multifleet.nats_client import _sub_ack
        assert _sub_ack("mac3") == "fleet.mac3.ack"

    def test_sub_direct_special_chars(self):
        """Node IDs with hyphens/underscores are valid."""
        from multifleet.nats_client import _sub_direct
        assert _sub_direct("mac-mini-1") == "fleet.mac-mini-1.>"
        assert _sub_direct("node_3") == "fleet.node_3.>"


# ═══════════════════════════════════════════════════════════════════
# 2. Message format and send()
# ═══════════════════════════════════════════════════════════════════

class TestMessageFormat:
    """Message envelope structure produced by send()."""

    @pytest.mark.asyncio
    async def test_send_unicast_format(self):
        nerve = _make_nerve()
        await nerve.send("mac2", "context", "hello", "world")
        call_args = nerve.nc.publish.call_args
        subject = call_args[0][0]
        payload = json.loads(call_args[0][1].decode())

        assert subject == "fleet.mac2.message"
        assert payload["type"] == "context"
        assert payload["from"] == "mac1"
        assert payload["to"] == "mac2"
        assert payload["payload"]["subject"] == "hello"
        assert payload["payload"]["body"] == "world"
        assert "id" in payload
        assert "timestamp" in payload

    @pytest.mark.asyncio
    async def test_send_broadcast_format(self):
        nerve = _make_nerve()
        await nerve.send("all", "alert", "fire", "evacuate")
        call_args = nerve.nc.publish.call_args
        subject = call_args[0][0]
        assert subject == "fleet.all.message"

    @pytest.mark.asyncio
    async def test_send_increments_stats(self):
        nerve = _make_nerve()
        assert nerve._stats["sent"] == 0
        await nerve.send("mac2", "context", "test", "body")
        assert nerve._stats["sent"] == 1

    @pytest.mark.asyncio
    async def test_send_broadcast_increments_broadcast_stat(self):
        nerve = _make_nerve()
        assert nerve._stats["broadcasts"] == 0
        await nerve.send("all", "context", "test", "body")
        assert nerve._stats["broadcasts"] == 1

    @pytest.mark.asyncio
    async def test_send_message_has_uuid_id(self):
        nerve = _make_nerve()
        await nerve.send("mac2", "context", "test", "body")
        payload = json.loads(nerve.nc.publish.call_args[0][1].decode())
        # Should be a valid UUID4
        parsed = uuid.UUID(payload["id"])
        assert parsed.version == 4

    @pytest.mark.asyncio
    async def test_send_message_has_utc_timestamp(self):
        nerve = _make_nerve()
        await nerve.send("mac2", "context", "test", "body")
        payload = json.loads(nerve.nc.publish.call_args[0][1].decode())
        ts = payload["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts


# ═══════════════════════════════════════════════════════════════════
# 3. P7 Git rate limiting
# ═══════════════════════════════════════════════════════════════════

class TestP7RateLimiting:
    """P7 Git channel rate limiting prevents commit floods."""

    def test_first_send_allowed(self):
        nerve = _make_nerve()
        assert nerve._p7_rate_check("mac2") is None

    def test_per_target_cooldown(self):
        nerve = _make_nerve()
        nerve._p7_last_sent["mac2"] = time.time()
        result = nerve._p7_rate_check("mac2")
        assert result is not None
        assert "per-target cooldown" in result

    def test_per_target_cooldown_expired(self):
        nerve = _make_nerve()
        nerve._p7_last_sent["mac2"] = time.time() - 700  # 600s cooldown
        assert nerve._p7_rate_check("mac2") is None

    def test_broadcast_cooldown(self):
        nerve = _make_nerve()
        nerve._p7_broadcast_last = time.time()
        result = nerve._p7_rate_check("all")
        assert result is not None
        assert "broadcast cooldown" in result

    def test_broadcast_cooldown_expired(self):
        nerve = _make_nerve()
        nerve._p7_broadcast_last = time.time() - 3700  # 3600s cooldown
        assert nerve._p7_rate_check("all") is None

    def test_hourly_cap(self):
        nerve = _make_nerve()
        now = time.time()
        nerve._p7_hourly_window = [now - i for i in range(6)]  # 6 in window
        result = nerve._p7_rate_check("mac2")
        assert result is not None
        assert "hourly cap" in result

    def test_hourly_cap_prunes_old(self):
        nerve = _make_nerve()
        # 6 entries but all >1 hour old
        nerve._p7_hourly_window = [time.time() - 4000 for _ in range(6)]
        assert nerve._p7_rate_check("mac2") is None
        assert len(nerve._p7_hourly_window) == 0  # pruned

    def test_record_sent_tracks_target(self):
        nerve = _make_nerve()
        nerve._p7_record_sent("mac2")
        assert "mac2" in nerve._p7_last_sent
        assert len(nerve._p7_hourly_window) == 1

    def test_record_sent_broadcast_tracks_broadcast_last(self):
        nerve = _make_nerve()
        nerve._p7_record_sent("all")
        assert nerve._p7_broadcast_last > 0
        assert "all" in nerve._p7_last_sent

    def test_different_targets_independent(self):
        nerve = _make_nerve()
        nerve._p7_last_sent["mac2"] = time.time()
        # mac3 should still be allowed
        assert nerve._p7_rate_check("mac3") is None


# ═══════════════════════════════════════════════════════════════════
# 4. Chain orchestration
# ═══════════════════════════════════════════════════════════════════

class TestChainOrchestration:
    """Multi-step chain task dispatch and tracking."""

    def test_handle_chain_registers(self):
        nerve = _make_nerve()
        nerve._dispatch_chain_step = MagicMock()
        data = {
            "chain_id": "test-chain-1",
            "steps": [
                {"node": "mac1", "task": "step 1"},
                {"node": "mac2", "task": "step 2"},
            ],
        }
        nerve._handle_chain(data)
        assert "test-chain-1" in nerve._chains
        chain = nerve._chains["test-chain-1"]
        assert chain["status"] == "running"
        assert chain["current"] == 0
        assert len(chain["steps"]) == 2
        nerve._dispatch_chain_step.assert_called_once_with("test-chain-1")

    def test_handle_chain_no_steps(self):
        nerve = _make_nerve()
        nerve._handle_chain({"chain_id": "empty", "steps": []})
        assert "empty" not in nerve._chains

    def test_handle_chain_auto_id(self):
        nerve = _make_nerve()
        nerve._dispatch_chain_step = MagicMock()
        nerve._handle_chain({"steps": [{"node": "mac1", "task": "x"}]})
        assert len(nerve._chains) == 1

    def test_advance_chain_success(self):
        nerve = _make_nerve()
        nerve._dispatch_chain_step = MagicMock()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}, {"task": "b"}],
            "current": 0, "status": "running", "results": [],
        }
        nerve._advance_chain("c1", True, "ok")
        assert nerve._chains["c1"]["current"] == 1
        assert nerve._chains["c1"]["status"] == "running"
        assert len(nerve._chains["c1"]["results"]) == 1

    def test_advance_chain_failure(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}, {"task": "b"}],
            "current": 0, "status": "running", "results": [],
        }
        nerve._advance_chain("c1", False, "error")
        assert nerve._chains["c1"]["status"] == "failed"

    def test_advance_chain_completion(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}],
            "current": 0, "status": "running", "results": [],
        }
        nerve._advance_chain("c1", True, "done")
        assert nerve._chains["c1"]["status"] == "done"
        assert nerve._chains["c1"]["current"] == 1

    def test_get_chain_status_found(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}], "current": 0,
            "status": "running", "results": [], "created_at": 1000,
        }
        status = nerve._get_chain_status("c1")
        assert status["chain_id"] == "c1"
        assert status["status"] == "running"
        assert status["total"] == 1

    def test_get_chain_status_not_found(self):
        nerve = _make_nerve()
        status = nerve._get_chain_status("nonexistent")
        assert "error" in status

    def test_advance_truncates_result(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {
            "steps": [{"task": "a"}, {"task": "b"}],
            "current": 0, "status": "running", "results": [],
        }
        long_result = "x" * 5000
        nerve._advance_chain("c1", True, long_result)
        assert len(nerve._chains["c1"]["results"][0]["result"]) == 2000


# ═══════════════════════════════════════════════════════════════════
# 5. Memory cleanup
# ═══════════════════════════════════════════════════════════════════

class TestMemoryCleanup:
    """Cleanup of terminal chains and dispatches prevents memory leaks."""

    def test_cleanup_evicts_old_done_chains(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {"status": "done", "created_at": time.time() - 100000}
        nerve._chains["c2"] = {"status": "running", "created_at": time.time() - 100000}
        result = nerve._cleanup_completed(max_age_s=86400)
        assert "c1" not in nerve._chains
        assert "c2" in nerve._chains  # running chains kept
        assert result["chains_evicted"] == 1

    def test_cleanup_evicts_old_failed_chains(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {"status": "failed", "created_at": time.time() - 100000}
        result = nerve._cleanup_completed(max_age_s=86400)
        assert result["chains_evicted"] == 1

    def test_cleanup_keeps_recent_terminal(self):
        nerve = _make_nerve()
        nerve._chains["c1"] = {"status": "done", "created_at": time.time()}
        result = nerve._cleanup_completed(max_age_s=86400)
        assert result["chains_evicted"] == 0
        assert "c1" in nerve._chains

    def test_cleanup_evicts_old_dispatches(self):
        nerve = _make_nerve()
        nerve._dispatches["d1"] = {"status": "completed", "completed_at": time.time() - 100000}
        nerve._dispatches["d2"] = {"status": "sent", "sent_at": time.time() - 100000}
        result = nerve._cleanup_completed(max_age_s=86400)
        assert "d1" not in nerve._dispatches
        assert "d2" in nerve._dispatches  # non-terminal kept
        assert result["dispatches_evicted"] == 1


# ═══════════════════════════════════════════════════════════════════
# 6. Peer resolution
# ═══════════════════════════════════════════════════════════════════

class TestPeerResolution:
    """Peer info resolution from heartbeat cache and config."""

    def test_resolve_from_config(self):
        nerve = _make_nerve()
        peer = nerve._resolve_peer("mac2")
        assert peer["ip"] == "10.0.0.2"
        assert peer["user"] == "bob"

    def test_resolve_from_heartbeat_cache(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {"ip": "192.168.1.50", "user": "bob2", "mac": "FF:FF:FF:FF:FF:FF"}
        peer = nerve._resolve_peer("mac2")
        # Heartbeat (dynamic) takes priority
        assert peer["ip"] == "192.168.1.50"
        assert peer["user"] == "bob2"

    def test_resolve_unknown_peer_empty(self):
        nerve = _make_nerve()
        peer = nerve._resolve_peer("mac99")
        assert peer == {}


# ═══════════════════════════════════════════════════════════════════
# 7. ACK tracking
# ═══════════════════════════════════════════════════════════════════

class TestAckTracking:
    """ACK message handling and tracking."""

    @pytest.mark.asyncio
    async def test_handle_ack_marks_pending(self):
        nerve = _make_nerve()
        nerve._pending_acks["msg-123"] = {"sent_at": time.time(), "target": "mac2", "retries": 0, "acked": False}
        msg = MagicMock()
        msg.data = json.dumps({"ref": "msg-123", "node": "mac2", "channel": "P1_nats"}).encode()
        await nerve._handle_ack(msg)
        assert nerve._pending_acks["msg-123"]["acked"] is True

    @pytest.mark.asyncio
    async def test_handle_ack_unknown_ref(self):
        nerve = _make_nerve()
        msg = MagicMock()
        msg.data = json.dumps({"ref": "unknown", "node": "mac2"}).encode()
        # Should not raise
        await nerve._handle_ack(msg)

    @pytest.mark.asyncio
    async def test_handle_ack_invalid_json(self):
        nerve = _make_nerve()
        msg = MagicMock()
        msg.data = b"not json"
        # Should not raise (swallowed by except)
        await nerve._handle_ack(msg)

    @pytest.mark.asyncio
    async def test_send_ack_publishes(self):
        nerve = _make_nerve()
        await nerve._send_ack("mac2", "msg-1", "delivered", "P1_nats")
        nerve.nc.publish.assert_called_once()
        subject = nerve.nc.publish.call_args[0][0]
        assert subject == "fleet.mac2.ack"
        payload = json.loads(nerve.nc.publish.call_args[0][1].decode())
        assert payload["ref"] == "msg-1"
        assert payload["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_send_ack_skips_self(self):
        nerve = _make_nerve()
        await nerve._send_ack("mac1", "msg-1", "delivered", "P1_nats")
        nerve.nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_ack_skips_disconnected(self):
        nerve = _make_nerve()
        nerve.nc.is_connected = False
        await nerve._send_ack("mac2", "msg-1", "delivered", "P1_nats")
        nerve.nc.publish.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 8. P1 NATS send
# ═══════════════════════════════════════════════════════════════════

class TestP1NatsSend:
    """Direct NATS publish channel."""

    @pytest.mark.asyncio
    async def test_p1_nats_unicast(self):
        nerve = _make_nerve()
        msg = {"id": "1", "payload": {"subject": "test"}}
        result = await nerve._send_p1_nats("mac2", msg)
        assert result["delivered"] is True
        subject = nerve.nc.publish.call_args[0][0]
        assert subject == "fleet.mac2.message"

    @pytest.mark.asyncio
    async def test_p1_nats_broadcast(self):
        nerve = _make_nerve()
        result = await nerve._send_p1_nats("all", {"id": "1"})
        assert result["delivered"] is True
        subject = nerve.nc.publish.call_args[0][0]
        assert subject == "fleet.all.message"

    @pytest.mark.asyncio
    async def test_p1_nats_disconnected(self):
        nerve = _make_nerve()
        nerve.nc.is_connected = False
        result = await nerve._send_p1_nats("mac2", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_p1_nats_no_client(self):
        nerve = _make_nerve()
        nerve.nc = None
        result = await nerve._send_p1_nats("mac2", {"id": "1"})
        assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 9. P6 WoL (Wake-on-LAN) magic packet
# ═══════════════════════════════════════════════════════════════════

class TestP6WoL:
    """Wake-on-LAN magic packet construction."""

    @pytest.mark.asyncio
    async def test_wol_no_mac_address(self):
        nerve = _make_nerve()
        nerve._peers_config["mac2"]["mac_address"] = ""
        result = await nerve._send_p6_wol("mac2", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_wol_unknown_peer(self):
        nerve = _make_nerve()
        result = await nerve._send_p6_wol("mac99", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_wol_sends_magic_packet(self):
        nerve = _make_nerve()
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            result = await nerve._send_p6_wol("mac2", {"id": "1"})
            assert result.get("wol_sent") is True
            assert result["delivered"] is False  # WoL doesn't confirm delivery
            # Verify magic packet structure
            sendto_args = mock_sock.sendto.call_args
            magic = sendto_args[0][0]
            assert magic[:6] == b'\xff' * 6  # 6 bytes of 0xFF
            # MAC "AA:BB:CC:DD:EE:02" repeated 16 times
            mac_bytes = bytes.fromhex("AABBCCDDEE02")
            assert magic[6:12] == mac_bytes
            assert len(magic) == 6 + 6 * 16  # 102 bytes total


# ═══════════════════════════════════════════════════════════════════
# 10. P5 SSH input validation
# ═══════════════════════════════════════════════════════════════════

class TestP5SSHValidation:
    """SSH channel validates target names to prevent injection."""

    @pytest.mark.asyncio
    async def test_ssh_rejects_path_traversal(self):
        nerve = _make_nerve()
        nerve._peers_config["../etc"] = {"user": "evil", "ip": "1.2.3.4"}
        result = await nerve._send_p5_ssh("../etc", {"id": "1"})
        assert result["delivered"] is False

    @pytest.mark.asyncio
    async def test_ssh_allows_valid_names(self):
        nerve = _make_nerve()
        # Valid names with hyphens and underscores
        # Won't actually deliver (no real SSH) but should pass validation
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = await nerve._send_p5_ssh("mac-mini_2", {"id": "1"})
            # Reaches subprocess (validation passed), even though SSH fails
            # The key thing: it didn't return early due to invalid name

    @pytest.mark.asyncio
    async def test_ssh_no_user_or_ip(self):
        nerve = _make_nerve()
        result = await nerve._send_p5_ssh("mac99", {"id": "1"})
        assert result["delivered"] is False


# ═══════════════════════════════════════════════════════════════════
# 11. Task result handling
# ═══════════════════════════════════════════════════════════════════

class TestTaskResultHandling:
    """Dispatch tracking from incoming task results."""

    def test_handle_task_result_updates_dispatch(self):
        nerve = _make_nerve()
        nerve._dispatches["d1"] = {"target": "mac2", "status": "sent", "sent_at": time.time()}
        nerve._handle_task_result({
            "payload": {"dispatch_id": "d1", "status": "completed", "response": "42"},
        })
        assert nerve._dispatches["d1"]["status"] == "completed"
        assert nerve._dispatches["d1"]["result"] == "42"
        assert "completed_at" in nerve._dispatches["d1"]

    def test_handle_task_result_unknown_dispatch(self):
        nerve = _make_nerve()
        nerve._inject_context = MagicMock()
        data = {"payload": {"dispatch_id": "unknown"}}
        nerve._handle_task_result(data)
        # Falls through to context injection
        nerve._inject_context.assert_called_once_with(data)


# ═══════════════════════════════════════════════════════════════════
# 12. Channel cascade (send_with_fallback)
# ═══════════════════════════════════════════════════════════════════

class TestSendWithFallback:
    """send_with_fallback tries channels in priority order."""

    @pytest.mark.asyncio
    async def test_p1_success_short_circuits(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "m1", "type": "context", "from": "mac1",
               "payload": {"subject": "test", "body": "hi"}}
        result = await nerve.send_with_fallback("mac2", msg)
        assert result["delivered"] is True
        assert result["channel"] == "P1_nats"
        nerve._send_p2_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_to_p2_on_p1_failure(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        nerve._attempt_self_heal = MagicMock()
        msg = {"id": "m1", "type": "context", "from": "mac1",
               "payload": {"subject": "test", "body": "hi"}}
        result = await nerve.send_with_fallback("mac2", msg)
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_message_gets_auto_id(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        msg = {"type": "context", "payload": {"subject": "test"}}
        await nerve.send_with_fallback("mac2", msg)
        assert "id" in msg
        assert "from" in msg
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_repair_messages_skip_p7_git(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False})
        nerve._send_p3_chief = AsyncMock(return_value={"delivered": False})
        nerve._send_p4_seed = AsyncMock(return_value={"delivered": False})
        nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False})
        nerve._send_p6_wol = AsyncMock(return_value={"delivered": False})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "r1", "type": "repair", "from": "mac1",
               "payload": {"subject": "self-heal-request"}}
        result = await nerve.send_with_fallback("mac2", msg)
        # P7 should be skipped for repair messages
        nerve._send_p7_git.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_routes_to_send_broadcast(self):
        nerve = _make_nerve()
        nerve._send_broadcast = AsyncMock(return_value={"delivered": True, "channel": "P1_nats"})
        msg = {"id": "b1", "type": "context", "from": "mac1"}
        result = await nerve.send_with_fallback("all", msg)
        nerve._send_broadcast.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_budget_enforced(self):
        """30s total budget prevents infinite cascade."""
        nerve = _make_nerve()

        async def slow_channel(target, msg):
            await asyncio.sleep(35)  # Exceeds budget
            return {"delivered": True}

        nerve._send_p1_nats = slow_channel
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        nerve._update_channel_health = MagicMock()
        msg = {"id": "t1", "type": "context", "from": "mac1",
               "payload": {"subject": "test"}}
        # P1 will timeout (2s channel timeout), then continue to P2
        result = await nerve.send_with_fallback("mac2", msg)
        # Should eventually deliver via P2 after P1 times out
        assert result["delivered"] is True


# ═══════════════════════════════════════════════════════════════════
# 13. send_smart session-aware routing
# ═══════════════════════════════════════════════════════════════════

class TestSendSmart:
    """Session-aware routing picks the right session type."""

    @pytest.mark.asyncio
    async def test_no_peer_health_falls_back(self):
        nerve = _make_nerve()
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "context", "payload": {"body": "hi"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "no_peer_health"

    @pytest.mark.asyncio
    async def test_task_routes_to_idle_session(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {
            "_received_at": time.time(),
            "sessions": [
                {"sessionId": "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-1111", "idle_s": 600},
                {"sessionId": "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-2222", "idle_s": 10},
            ],
        }
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "task", "payload": {"body": "do work"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "task_to_idle_session"
        # Should pick the most idle session
        assert msg["_target_session"] == "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-1111"

    @pytest.mark.asyncio
    async def test_task_no_idle_sessions(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {
            "_received_at": time.time(),
            "sessions": [
                {"sessionId": "s1", "idle_s": 10},  # Not idle enough (< 300s)
            ],
        }
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "task", "payload": {"body": "do work"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "task_no_idle"

    @pytest.mark.asyncio
    async def test_context_routes_to_primary(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {
            "_received_at": time.time(),
            "sessions": [
                {"sessionId": "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-1111", "idle_s": 600},
                {"sessionId": "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-2222", "idle_s": 5},
            ],
        }
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "context", "payload": {"body": "info"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "context_to_primary"
        # Primary = least idle
        assert msg["_target_session"] == "aaaa-bbbb-cccc-dddd-eeee-ffff-0000-2222"

    @pytest.mark.asyncio
    async def test_alert_routes_to_all_sessions(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {
            "_received_at": time.time(),
            "sessions": [
                {"sessionId": "s1", "idle_s": 5},
                {"sessionId": "s2", "idle_s": 10},
            ],
        }
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "alert", "payload": {"body": "emergency"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "alert_all_sessions"

    @pytest.mark.asyncio
    async def test_stale_peer_data_falls_back(self):
        nerve = _make_nerve()
        nerve._peers["mac2"] = {
            "_received_at": time.time() - 200,  # > 120s stale
            "sessions": [],
        }
        nerve.send_with_fallback = AsyncMock(return_value={"delivered": True})
        msg = {"type": "context", "payload": {"body": "hi"}}
        result = await nerve.send_smart("mac2", msg)
        assert result["routing"] == "no_peer_health"


# ═══════════════════════════════════════════════════════════════════
# 14. Message handling (_handle_message)
# ═══════════════════════════════════════════════════════════════════

class TestHandleMessage:
    """Incoming message routing by subject and type."""

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self):
        nerve = _make_nerve()
        nerve._inject_context = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.all.message"
        msg.data = json.dumps({"from": "mac1", "type": "context", "payload": {}}).encode()
        msg.reply = None
        await nerve._handle_message(msg)
        nerve._inject_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_ack_subject(self):
        nerve = _make_nerve()
        msg = MagicMock()
        msg.subject = "fleet.mac1.ack"
        msg.data = json.dumps({"from": "mac2", "type": "ack"}).encode()
        msg.reply = None
        stats_before = nerve._stats["received"]
        await nerve._handle_message(msg)
        # ACK subject messages should be returned early
        assert nerve._stats["received"] == stats_before

    @pytest.mark.asyncio
    async def test_routes_context_to_inject(self):
        nerve = _make_nerve()
        nerve._inject_context = MagicMock()
        nerve._touch_activity = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.data = json.dumps({
            "from": "mac2", "type": "context",
            "payload": {"subject": "hello", "body": "world"},
        }).encode()
        msg.reply = None
        await nerve._handle_message(msg)
        nerve._inject_context.assert_called_once()
        assert nerve._stats["received"] == 1

    @pytest.mark.asyncio
    async def test_routes_alert_to_inject_alert(self):
        nerve = _make_nerve()
        nerve._inject_alert = MagicMock()
        nerve._touch_activity = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.data = json.dumps({
            "from": "mac2", "type": "alert",
            "payload": {"subject": "fire", "body": "evacuate"},
        }).encode()
        msg.reply = None
        await nerve._handle_message(msg)
        nerve._inject_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_task_to_inject_task(self):
        nerve = _make_nerve()
        nerve._inject_task = MagicMock()
        nerve._touch_activity = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.data = json.dumps({
            "from": "mac2", "type": "task",
            "payload": {"subject": "work", "body": "do it"},
        }).encode()
        msg.reply = None
        await nerve._handle_message(msg)
        nerve._inject_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_request_reply(self):
        nerve = _make_nerve()
        nerve._build_health = MagicMock(return_value={"status": "ok"})
        nerve._touch_activity = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.health"
        msg.data = json.dumps({"from": "mac2"}).encode()
        msg.reply = "_INBOX.reply123"
        await nerve._handle_message(msg)
        nerve.nc.publish.assert_called_once()
        reply_subject = nerve.nc.publish.call_args[0][0]
        assert reply_subject == "_INBOX.reply123"

    @pytest.mark.asyncio
    async def test_ack_type_updates_store(self):
        nerve = _make_nerve()
        nerve._store = MagicMock()
        nerve._touch_activity = MagicMock()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.data = json.dumps({
            "from": "mac2", "type": "ack", "ref": "msg-xyz", "status": "delivered",
        }).encode()
        msg.reply = None
        await nerve._handle_message(msg)
        nerve._store.update_ack_status.assert_called_once_with("msg-xyz", "delivered")

    @pytest.mark.asyncio
    async def test_bad_json_increments_errors(self):
        nerve = _make_nerve()
        msg = MagicMock()
        msg.subject = "fleet.mac1.message"
        msg.data = b"not valid json"
        await nerve._handle_message(msg)
        assert nerve._stats["errors"] == 1


# ═══════════════════════════════════════════════════════════════════
# 15. ChannelState (from channels.py, used by nats_client)
# ═══════════════════════════════════════════════════════════════════

class TestChannelState:
    """Per-peer per-channel health tracking."""

    def test_record_success(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_success("mac2", "P1_nats", 15)
        entry = cs._state["mac2"]["P1_nats"]
        assert entry["status"] == "ok"
        assert entry["latency_ms"] == 15
        assert entry["fail_count"] == 0

    def test_record_failure(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "timeout")
        entry = cs._state["mac2"]["P1_nats"]
        assert entry["status"] == "broken"
        assert entry["fail_count"] == 1

    def test_is_known_broken_fresh(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "timeout")
        assert cs.is_known_broken("mac2", "P1_nats") is True

    def test_is_known_broken_stale(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "timeout")
        cs._state["mac2"]["P1_nats"]["last_failure"] = time.time() - 200
        assert cs.is_known_broken("mac2", "P1_nats", max_age_s=120) is False

    def test_is_known_broken_unknown_peer(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        assert cs.is_known_broken("mac99", "P1_nats") is False

    def test_get_broken_channels(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "timeout")
        cs.record_success("mac2", "P2_http", 50)
        broken = cs.get_broken_channels("mac2")
        assert "P1_nats" in broken
        assert "P2_http" not in broken

    def test_get_all_omits_internal_keys(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_success("mac2", "P1_nats", 10)
        result = cs.get_all()
        for key in result["mac2"]["P1_nats"]:
            assert not key.startswith("_")

    def test_fail_count_increments(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "err1")
        cs.record_failure("mac2", "P1_nats", "err2")
        assert cs._state["mac2"]["P1_nats"]["fail_count"] == 2

    def test_success_resets_fail_count(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac2", "P1_nats", "err")
        cs.record_failure("mac2", "P1_nats", "err")
        cs.record_success("mac2", "P1_nats", 5)
        assert cs._state["mac2"]["P1_nats"]["fail_count"] == 0


# ═══════════════════════════════════════════════════════════════════
# 16. Channel constants and ordering
# ═══════════════════════════════════════════════════════════════════

class TestChannelConstants:
    """Channel priority ordering is correct."""

    def test_channel_order_starts_with_discord(self):
        from multifleet.channels import CHANNEL_ORDER
        assert CHANNEL_ORDER[0] == "P0_discord"

    def test_channel_order_ends_with_keystroke(self):
        from multifleet.channels import CHANNEL_ORDER
        assert CHANNEL_ORDER[-1] == "P8_keystroke"

    def test_nats_before_http(self):
        from multifleet.channels import CHANNEL_ORDER
        nats_idx = CHANNEL_ORDER.index("P1_nats")
        http_idx = CHANNEL_ORDER.index("P2_http")
        assert nats_idx < http_idx

    def test_all_channels_have_timeouts(self):
        from multifleet.channels import CHANNEL_ORDER, CHANNEL_TIMEOUT_S
        for ch in CHANNEL_ORDER:
            assert ch in CHANNEL_TIMEOUT_S, f"Missing timeout for {ch}"


# ═══════════════════════════════════════════════════════════════════
# 17. Broadcast routing
# ═══════════════════════════════════════════════════════════════════

class TestBroadcast:
    """Broadcast path: NATS primary, HTTP parallel fallback."""

    @pytest.mark.asyncio
    async def test_nats_broadcast_primary(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        result = await nerve._send_broadcast({"id": "b1", "to": "all"})
        assert result["delivered"] is True
        assert result["channel"] == "P1_nats"
        assert result["mode"] == "broadcast_nats"

    @pytest.mark.asyncio
    async def test_http_fallback_on_nats_failure(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        nerve._peers = {"mac2": {"_received_at": time.time()}}
        result = await nerve._send_broadcast({"id": "b1", "to": "all"})
        assert result["delivered"] is True
        assert result["mode"] == "broadcast_http_parallel"

    @pytest.mark.asyncio
    async def test_broadcast_excludes_self(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": True})
        nerve._update_channel_health = MagicMock()
        # Self is in peers list; clear config peers to isolate
        nerve._peers_config = {}
        nerve._peers = {
            "mac1": {"_received_at": time.time()},
            "mac2": {"_received_at": time.time()},
        }
        await nerve._send_broadcast({"id": "b1", "to": "all"})
        # Should only HTTP to mac2, not mac1 (self)
        assert nerve._send_p2_http.call_count == 1
        call_target = nerve._send_p2_http.call_args[0][0]
        assert call_target == "mac2"

    @pytest.mark.asyncio
    async def test_broadcast_no_peers_returns_failure(self):
        nerve = _make_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False})
        nerve._peers = {}
        nerve._peers_config = {}
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False})
        result = await nerve._send_broadcast({"id": "b1", "to": "all"})
        assert result["delivered"] is False
        assert result["mode"] == "broadcast_no_peers"


# ═══════════════════════════════════════════════════════════════════
# 18. Session normalization (from sessions.py)
# ═══════════════════════════════════════════════════════════════════

class TestSessionNormalization:
    """Session ID normalization from truncated to full UUID."""

    def test_full_uuid_unchanged(self):
        from multifleet.sessions import normalize_session_id
        full = "12345678-1234-1234-1234-123456789abc"
        assert normalize_session_id(full) == full

    def test_empty_string_unchanged(self):
        from multifleet.sessions import normalize_session_id
        assert normalize_session_id("") == ""

    def test_truncated_resolves_from_list(self):
        from multifleet.sessions import normalize_session_id
        sessions = [
            {"sessionId": "12345678-1234-1234-1234-123456789abc"},
            {"sessionId": "abcdefgh-abcd-abcd-abcd-abcdefghijkl"},
        ]
        result = normalize_session_id("12345678", active_sessions=sessions)
        assert result == "12345678-1234-1234-1234-123456789abc"

    def test_ambiguous_truncated_returns_original(self):
        from multifleet.sessions import normalize_session_id
        sessions = [
            {"sessionId": "12345678-1234-1234-1234-111111111111"},
            {"sessionId": "12345678-1234-1234-1234-222222222222"},
        ]
        result = normalize_session_id("12345678", active_sessions=sessions)
        assert result == "12345678"  # ambiguous, return original

    def test_no_match_returns_original(self):
        from multifleet.sessions import normalize_session_id
        sessions = [{"sessionId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}]
        result = normalize_session_id("zzzzz", active_sessions=sessions)
        assert result == "zzzzz"
