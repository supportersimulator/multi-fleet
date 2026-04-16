"""
Tests for multifleet.channels — ChannelState tracking, CommunicationProtocol cascade,
is_known_broken logic, and self-heal scheduling.

All network calls mocked. Tests verify channel state machine logic.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.channels import (
    ChannelState,
    CommunicationProtocol,
    CHANNEL_P0_DISCORD,
    CHANNEL_P1_NATS,
    CHANNEL_P2_HTTP,
    CHANNEL_P3_CHIEF,
    CHANNEL_P4_SEED,
    CHANNEL_P5_SSH,
    CHANNEL_P7_GIT,
    CHANNEL_P8_KEYSTROKE,
    CHANNEL_ORDER,
    ALL_CHANNELS,
    REPAIR_COOLDOWN_S,
    HEAL_SCORE_THRESHOLD,
    _dynamic_probe_count,
)


# ══════════════════════════════════════════════════════════════
# ChannelState
# ══════════════════════════════════════════════════════════════


class TestChannelStateRecordSuccess:
    """record_success updates status, latency, and resets fail_count."""

    def test_new_peer_channel_created(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 15)
        assert "mac2" in cs._state
        assert CHANNEL_P1_NATS in cs._state["mac2"]

    def test_status_set_to_ok(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 15)
        assert cs._state["mac2"][CHANNEL_P1_NATS]["status"] == "ok"

    def test_latency_recorded(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P2_HTTP, 42)
        assert cs._state["mac2"][CHANNEL_P2_HTTP]["latency_ms"] == 42

    def test_fail_count_reset_on_success(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P1_NATS, "timeout")
        cs.record_failure("mac2", CHANNEL_P1_NATS, "timeout")
        assert cs._state["mac2"][CHANNEL_P1_NATS]["fail_count"] == 2
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        assert cs._state["mac2"][CHANNEL_P1_NATS]["fail_count"] == 0

    def test_last_success_timestamp_updated(self):
        cs = ChannelState()
        before = time.time()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        after = time.time()
        ts = cs._state["mac2"][CHANNEL_P1_NATS]["last_success"]
        assert before <= ts <= after


class TestChannelStateRecordFailure:
    """record_failure sets broken status and increments fail_count."""

    def test_status_set_to_broken(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        assert cs._state["mac2"][CHANNEL_P2_HTTP]["status"] == "broken"

    def test_fail_count_increments(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "err1")
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "err2")
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "err3")
        assert cs._state["mac2"][CHANNEL_P2_HTTP]["fail_count"] == 3

    def test_last_failure_timestamp_updated(self):
        cs = ChannelState()
        before = time.time()
        cs.record_failure("mac2", CHANNEL_P1_NATS, "down")
        after = time.time()
        ts = cs._state["mac2"][CHANNEL_P1_NATS]["last_failure"]
        assert before <= ts <= after


class TestIsKnownBroken:
    """is_known_broken returns True only for recently-broken channels."""

    def test_unknown_peer_returns_false(self):
        cs = ChannelState()
        assert cs.is_known_broken("unknown-peer", CHANNEL_P1_NATS) is False

    def test_unknown_channel_returns_false(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        assert cs.is_known_broken("mac2", CHANNEL_P2_HTTP) is False

    def test_ok_channel_returns_false(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        assert cs.is_known_broken("mac2", CHANNEL_P1_NATS) is False

    def test_recently_broken_returns_true(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        assert cs.is_known_broken("mac2", CHANNEL_P2_HTTP) is True

    def test_stale_broken_returns_false(self):
        """Broken state older than max_age_s should be retried (return False)."""
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        # Backdate the failure
        cs._state["mac2"][CHANNEL_P2_HTTP]["last_failure"] = time.time() - 200
        assert cs.is_known_broken("mac2", CHANNEL_P2_HTTP, max_age_s=120) is False

    def test_custom_max_age(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        # Just failed — should be broken with any reasonable max_age
        assert cs.is_known_broken("mac2", CHANNEL_P2_HTTP, max_age_s=10) is True
        # Backdate by 15s — should NOT be broken with 10s max_age
        cs._state["mac2"][CHANNEL_P2_HTTP]["last_failure"] = time.time() - 15
        assert cs.is_known_broken("mac2", CHANNEL_P2_HTTP, max_age_s=10) is False


class TestGetBrokenChannels:
    """get_broken_channels returns list of broken channel names."""

    def test_no_state_returns_empty(self):
        cs = ChannelState()
        assert cs.get_broken_channels("mac2") == []

    def test_returns_only_broken(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        cs.record_failure("mac2", CHANNEL_P5_SSH, "auth failed")
        broken = cs.get_broken_channels("mac2")
        assert CHANNEL_P2_HTTP in broken
        assert CHANNEL_P5_SSH in broken
        assert CHANNEL_P1_NATS not in broken

    def test_success_clears_from_broken(self):
        cs = ChannelState()
        cs.record_failure("mac2", CHANNEL_P2_HTTP, "refused")
        assert CHANNEL_P2_HTTP in cs.get_broken_channels("mac2")
        cs.record_success("mac2", CHANNEL_P2_HTTP, 20)
        assert CHANNEL_P2_HTTP not in cs.get_broken_channels("mac2")


class TestGetAll:
    """get_all returns serializable state without internal keys."""

    def test_omits_internal_keys(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        cs.record_failure("mac2", CHANNEL_P1_NATS, "down")
        cs.record_success("mac2", CHANNEL_P1_NATS, 5)
        all_state = cs.get_all()
        entry = all_state["mac2"][CHANNEL_P1_NATS]
        for key in entry:
            assert not key.startswith("_"), f"Internal key leaked: {key}"

    def test_structure_is_json_serializable(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 10)
        cs.record_failure("mac3", CHANNEL_P2_HTTP, "err")
        import json
        json.dumps(cs.get_all())  # Should not raise


class TestGetPeerSummary:
    """get_peer_summary returns status+latency per channel."""

    def test_unknown_peer_returns_empty(self):
        cs = ChannelState()
        assert cs.get_peer_summary("nonexistent") == {}

    def test_returns_status_and_latency(self):
        cs = ChannelState()
        cs.record_success("mac2", CHANNEL_P1_NATS, 15)
        summary = cs.get_peer_summary("mac2")
        assert summary[CHANNEL_P1_NATS]["status"] == "ok"
        assert summary[CHANNEL_P1_NATS]["latency_ms"] == 15


# ══════════════════════════════════════════════════════════════
# CommunicationProtocol
# ══════════════════════════════════════════════════════════════


def _make_mock_nerve():
    """Create a mock FleetNerveNATS with all channel senders."""
    nerve = MagicMock()
    nerve.node_id = "mac1"
    nerve._peers_config = {
        "mac1": {"ip": "127.0.0.1"},
        "mac2": {"ip": "10.0.0.2"},
        "mac3": {"ip": "10.0.0.3"},
    }
    nerve._peers = {}
    nerve._resolve_peer = MagicMock(side_effect=lambda p: nerve._peers_config.get(p, {}))
    nerve.repair_with_escalation = AsyncMock()
    nerve._keystroke_trigger = MagicMock()

    # Default: all senders return not-delivered
    for method in ["_send_p0_discord", "_send_p0_cloud", "_send_p1_nats",
                   "_send_p2_http", "_send_p2t_tailscale", "_send_p3_chief",
                   "_send_p4_seed", "_send_p5_ssh", "_send_p6_wol", "_send_p7_git"]:
        setattr(nerve, method, AsyncMock(return_value={"delivered": False, "error": "mock"}))
    return nerve


def _run(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


class TestCommunicationProtocolSend:
    """send() cascades through channels, stops on first success."""

    def test_first_success_wins(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        assert result["delivered"] is True
        assert result["channel"] == CHANNEL_P0_DISCORD
        nerve._send_p1_nats.assert_not_called()

    def test_cascade_to_second_channel(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": False, "error": "no webhook"})
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        assert result["delivered"] is True
        assert result["channel"] == CHANNEL_P1_NATS

    def test_skips_known_broken_channels(self):
        nerve = _make_mock_nerve()
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)
        proto.channel_state.record_failure("mac2", CHANNEL_P0_DISCORD, "webhook down")

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        assert result["delivered"] is True
        assert result["channel"] == CHANNEL_P1_NATS
        nerve._send_p0_discord.assert_not_called()

    def test_message_envelope_populated(self):
        """send() adds id, from, timestamp if missing."""
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        msg = {"type": "test", "payload": {}}
        _run(proto.send("mac2", msg))
        assert "id" in msg
        assert msg["from"] == "mac1"
        assert "timestamp" in msg

    def test_all_channels_fail_returns_not_delivered(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False, "error": "cloud down"})
        proto = CommunicationProtocol(nerve)
        proto._send_p8_keystroke = AsyncMock(return_value={"delivered": False, "error": "not local"})

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        assert result["delivered"] is False
        assert "errors" in result

    def test_records_success_in_channel_state(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        _run(proto.send("mac2", {"type": "test", "payload": {}}))
        state = proto.channel_state.get_peer_summary("mac2")
        assert state[CHANNEL_P0_DISCORD]["status"] == "ok"

    def test_records_failure_in_channel_state(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        _run(proto.send("mac2", {"type": "test", "payload": {}}))
        broken = proto.channel_state.get_broken_channels("mac2")
        assert CHANNEL_P0_DISCORD in broken

    def test_timeout_records_failure(self):
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(side_effect=asyncio.TimeoutError())
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        assert result["delivered"] is True
        assert result["channel"] == CHANNEL_P1_NATS

    def test_wol_sent_does_not_record_failure(self):
        """WoL fire-and-forget should not poison channel state."""
        nerve = _make_mock_nerve()
        nerve._send_p0_discord = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p1_nats = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p2t_tailscale = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p3_chief = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p4_seed = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False, "error": "nope"})
        nerve._send_p6_wol = AsyncMock(return_value={"wol_sent": True})
        nerve._send_p7_git = AsyncMock(return_value={"delivered": True})
        proto = CommunicationProtocol(nerve)

        result = _run(proto.send("mac2", {"type": "test", "payload": {}}))
        broken = proto.channel_state.get_broken_channels("mac2")
        assert "P6_wol" not in broken


class TestP8Keystroke:
    """P8 keystroke — last resort, rate-limited, local-only."""

    def test_p8_rejects_remote_target(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)
        CommunicationProtocol._last_p8_time = 0.0

        result = _run(proto._send_p8_keystroke("mac2", {"from": "mac1", "payload": {"subject": "test"}}))
        assert result["delivered"] is False
        assert "local" in result["error"].lower()

    def test_p8_rate_limited(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)
        CommunicationProtocol._last_p8_time = time.time()

        result = _run(proto._send_p8_keystroke("mac1", {"from": "mac1", "payload": {"subject": "test"}}))
        assert result["delivered"] is False
        assert "rate" in result["error"].lower()

    def test_p8_succeeds_for_local_node(self, tmp_path):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)
        CommunicationProtocol._last_p8_time = 0.0

        with patch("multifleet.channels.SEED_DIR", str(tmp_path)):
            result = _run(proto._send_p8_keystroke(
                "mac1",
                {"from": "mac1", "payload": {"subject": "urgent"}}
            ))
        assert result["delivered"] is True
        assert result["method"] == "p8_keystroke"
        nerve._keystroke_trigger.assert_called_once()


class TestSelfHealTrigger:
    """_trigger_heal respects cooldowns and delegates to repair_with_escalation."""

    def test_trigger_heal_calls_repair(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)

        _run(proto._trigger_heal("mac2", CHANNEL_P4_SEED, [CHANNEL_P1_NATS]))
        nerve.repair_with_escalation.assert_called_once()

    def test_trigger_heal_respects_cooldown(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)

        _run(proto._trigger_heal("mac2", CHANNEL_P4_SEED, [CHANNEL_P1_NATS]))
        nerve.repair_with_escalation.reset_mock()
        _run(proto._trigger_heal("mac2", CHANNEL_P4_SEED, [CHANNEL_P1_NATS]))
        nerve.repair_with_escalation.assert_not_called()

    def test_trigger_heal_empty_broken_noop(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)

        _run(proto._trigger_heal("mac2", CHANNEL_P4_SEED, []))
        nerve.repair_with_escalation.assert_not_called()


class TestStartStop:
    """start/stop control the self-heal background loop."""

    def test_start_creates_task(self):
        async def _test():
            nerve = _make_mock_nerve()
            proto = CommunicationProtocol(nerve)
            with patch.object(proto, "_self_heal_loop", new_callable=AsyncMock):
                proto.start()
                assert proto._self_heal_task is not None
                proto.stop()
        asyncio.run(_test())

    def test_stop_without_start_is_safe(self):
        nerve = _make_mock_nerve()
        proto = CommunicationProtocol(nerve)
        proto.stop()  # Should not raise


# ── Dynamic probe count ──


class TestDynamicProbeCount:
    """_dynamic_probe_count scales probe budget with fleet size."""

    def test_small_fleet_minimum_10(self):
        assert _dynamic_probe_count(3) == 10

    def test_medium_fleet_minimum_10(self):
        assert _dynamic_probe_count(50) == 10

    def test_large_fleet_sqrt_scaling(self):
        result = _dynamic_probe_count(200)
        assert result >= 14  # sqrt(200) ~ 14.1

    def test_zero_peers(self):
        assert _dynamic_probe_count(0) == 10


# ── Channel constants ──


class TestChannelConstants:
    """Channel constants and ordering are consistent."""

    def test_channel_order_has_all_expected(self):
        assert CHANNEL_P0_DISCORD in CHANNEL_ORDER
        assert CHANNEL_P1_NATS in CHANNEL_ORDER
        assert CHANNEL_P2_HTTP in CHANNEL_ORDER
        assert CHANNEL_P7_GIT in CHANNEL_ORDER
        assert CHANNEL_P8_KEYSTROKE in CHANNEL_ORDER

    def test_p0_before_p1(self):
        assert CHANNEL_ORDER.index(CHANNEL_P0_DISCORD) < CHANNEL_ORDER.index(CHANNEL_P1_NATS)

    def test_p1_before_p7(self):
        assert CHANNEL_ORDER.index(CHANNEL_P1_NATS) < CHANNEL_ORDER.index(CHANNEL_P7_GIT)
