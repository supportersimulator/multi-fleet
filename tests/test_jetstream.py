"""
Tests for multifleet/jetstream.py — durable persistence for fleet-open blocker #2.

All JetStream client calls are mocked. Tests verify:
  - config gating (enabled flag, env override, defaults)
  - idempotent stream creation (exists vs missing vs concurrent race)
  - graceful degradation when server has JS disabled
  - durable consumer creation + naming
  - best-effort publish (timeout, failure non-fatal)
  - replay loop drains pending msgs, acks on success, naks on handler error
  - FleetNerveNATS integration: connect() wires streams + durables only when gated on

Network is never touched.
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════
# 1. Config gating
# ═══════════════════════════════════════════════════════════════════

class TestConfig:
    def test_defaults_when_no_file(self, tmp_path, monkeypatch):
        from multifleet import jetstream as js
        # Clear env override; pass empty config
        monkeypatch.delenv("MULTIFLEET_JETSTREAM_ENABLED", raising=False)
        cfg = js.get_jetstream_config(config={})
        assert cfg["enabled"] is False
        assert cfg["max_age_s"] == js.DEFAULT_MAX_AGE_DAYS * 24 * 3600
        assert cfg["max_bytes"] == js.DEFAULT_MAX_BYTES_MB * 1024 * 1024
        assert cfg["replay_max_messages"] == js.DEFAULT_REPLAY_MAX_MESSAGES

    def test_enabled_from_config_block(self, monkeypatch):
        from multifleet import jetstream as js
        monkeypatch.delenv("MULTIFLEET_JETSTREAM_ENABLED", raising=False)
        cfg = js.get_jetstream_config(config={
            "jetstream": {"enabled": True, "max_age_days": 3, "max_bytes_mb": 50}
        })
        assert cfg["enabled"] is True
        assert cfg["max_age_s"] == 3 * 24 * 3600
        assert cfg["max_bytes"] == 50 * 1024 * 1024

    def test_env_overrides_config(self, monkeypatch):
        from multifleet import jetstream as js
        monkeypatch.setenv("MULTIFLEET_JETSTREAM_ENABLED", "1")
        cfg = js.get_jetstream_config(config={"jetstream": {"enabled": False}})
        assert cfg["enabled"] is True
        monkeypatch.setenv("MULTIFLEET_JETSTREAM_ENABLED", "false")
        cfg = js.get_jetstream_config(config={"jetstream": {"enabled": True}})
        assert cfg["enabled"] is False

    def test_is_jetstream_enabled_helper(self, monkeypatch):
        from multifleet import jetstream as js
        monkeypatch.delenv("MULTIFLEET_JETSTREAM_ENABLED", raising=False)
        assert js.is_jetstream_enabled({}) is False
        assert js.is_jetstream_enabled({"jetstream": {"enabled": True}}) is True


# ═══════════════════════════════════════════════════════════════════
# 2. ensure_streams
# ═══════════════════════════════════════════════════════════════════

def _make_nc_with_js(js_mock):
    """Build a mock NATS client whose .jetstream() returns js_mock."""
    nc = MagicMock()
    nc.jetstream = MagicMock(return_value=js_mock)
    return nc


class TestEnsureStreams:
    @pytest.mark.asyncio
    async def test_creates_both_streams_when_missing(self, monkeypatch):
        from multifleet import jetstream as js
        from nats.js.errors import NotFoundError

        js_mock = MagicMock()
        # stream_info raises NotFoundError for both streams
        js_mock.stream_info = AsyncMock(side_effect=NotFoundError())
        js_mock.add_stream = AsyncMock(return_value=MagicMock())
        nc = _make_nc_with_js(js_mock)

        result = await js.ensure_streams(nc, config={"jetstream": {"enabled": True}})
        assert result is js_mock
        assert js_mock.add_stream.await_count == 2
        names = {call.kwargs["name"] for call in js_mock.add_stream.await_args_list}
        assert names == {js.STREAM_MESSAGES, js.STREAM_EVENTS}

    @pytest.mark.asyncio
    async def test_skips_creation_when_streams_exist(self):
        from multifleet import jetstream as js

        js_mock = MagicMock()
        js_mock.stream_info = AsyncMock(return_value=MagicMock())  # exists
        js_mock.add_stream = AsyncMock()
        nc = _make_nc_with_js(js_mock)

        result = await js.ensure_streams(nc, config={"jetstream": {"enabled": True}})
        assert result is js_mock
        assert js_mock.add_stream.await_count == 0

    @pytest.mark.asyncio
    async def test_graceful_when_jetstream_unavailable(self):
        """Server has JetStream disabled — nc.jetstream() raises."""
        from multifleet import jetstream as js

        nc = MagicMock()
        nc.jetstream = MagicMock(side_effect=RuntimeError("JetStream not enabled"))

        result = await js.ensure_streams(nc, config={"jetstream": {"enabled": True}})
        assert result is None

    @pytest.mark.asyncio
    async def test_tolerates_concurrent_creation_race(self):
        """Another node created the stream between our info check and add_stream."""
        from multifleet import jetstream as js
        from nats.js.errors import NotFoundError

        js_mock = MagicMock()
        js_mock.stream_info = AsyncMock(side_effect=NotFoundError())
        # First add_stream succeeds, second raises "stream name already in use"
        js_mock.add_stream = AsyncMock(
            side_effect=[MagicMock(), RuntimeError("stream name already in use")]
        )
        nc = _make_nc_with_js(js_mock)

        # Should NOT raise; returns context with partial success
        result = await js.ensure_streams(nc, config={"jetstream": {"enabled": True}})
        assert result is js_mock

    @pytest.mark.asyncio
    async def test_passes_retention_limits(self):
        from multifleet import jetstream as js
        from nats.js.errors import NotFoundError

        js_mock = MagicMock()
        js_mock.stream_info = AsyncMock(side_effect=NotFoundError())
        js_mock.add_stream = AsyncMock(return_value=MagicMock())
        nc = _make_nc_with_js(js_mock)

        await js.ensure_streams(nc, config={
            "jetstream": {"enabled": True, "max_age_days": 7, "max_bytes_mb": 100}
        })
        # Check first call (FLEET_MESSAGES) has correct retention
        first_call = js_mock.add_stream.await_args_list[0]
        kwargs = first_call.kwargs
        assert kwargs["max_age"] == 7 * 24 * 3600
        assert kwargs["max_bytes"] == 100 * 1024 * 1024
        assert kwargs["retention"] == "limits"
        assert kwargs["storage"] == "file"

    @pytest.mark.asyncio
    async def test_returns_none_when_nc_is_none(self):
        from multifleet import jetstream as js
        result = await js.ensure_streams(None, config={"jetstream": {"enabled": True}})
        assert result is None


# ═══════════════════════════════════════════════════════════════════
# 3. Durable consumers
# ═══════════════════════════════════════════════════════════════════

class TestDurableConsumers:
    def test_durable_name_is_stable_and_sanitized(self):
        from multifleet import jetstream as js
        assert js.durable_consumer_name("FLEET_MESSAGES", "mac1") == "FLEET_MESSAGES_mac1"
        assert js.durable_consumer_name("FLEET_MESSAGES", "mac-mini-1") == "FLEET_MESSAGES_mac-mini-1"
        # A dot in a node_id must not leak through into the durable name
        assert "." not in js.durable_consumer_name("FLEET_MESSAGES", "bad.id")

    @pytest.mark.asyncio
    async def test_create_durable_consumer_happy_path(self):
        from multifleet import jetstream as js
        js_ctx = MagicMock()
        fake_sub = MagicMock()
        js_ctx.pull_subscribe = AsyncMock(return_value=fake_sub)

        result = await js.create_durable_consumer(
            js_ctx, js.STREAM_MESSAGES, "FLEET_MESSAGES_mac1", "fleet.mac1.>",
        )
        assert result is fake_sub
        call = js_ctx.pull_subscribe.await_args
        assert call.kwargs["durable"] == "FLEET_MESSAGES_mac1"
        assert call.kwargs["stream"] == js.STREAM_MESSAGES

    @pytest.mark.asyncio
    async def test_create_durable_consumer_returns_none_on_error(self):
        from multifleet import jetstream as js
        js_ctx = MagicMock()
        js_ctx.pull_subscribe = AsyncMock(side_effect=RuntimeError("boom"))
        result = await js.create_durable_consumer(
            js_ctx, js.STREAM_MESSAGES, "FLEET_MESSAGES_mac1", "fleet.mac1.>",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_create_durable_consumer_returns_none_when_js_is_none(self):
        from multifleet import jetstream as js
        assert await js.create_durable_consumer(None, "X", "dur", "sub") is None


# ═══════════════════════════════════════════════════════════════════
# 4. publish_to_stream
# ═══════════════════════════════════════════════════════════════════

class TestPublishToStream:
    @pytest.mark.asyncio
    async def test_publish_success_returns_true(self):
        from multifleet import jetstream as js
        js_ctx = MagicMock()
        js_ctx.publish = AsyncMock(return_value=MagicMock(stream="FLEET_MESSAGES", seq=42))
        ok = await js.publish_to_stream(js_ctx, "fleet.mac2.message", b'{"hi":1}')
        assert ok is True
        js_ctx.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publish_failure_returns_false(self):
        from multifleet import jetstream as js
        js_ctx = MagicMock()
        js_ctx.publish = AsyncMock(side_effect=RuntimeError("no stream"))
        ok = await js.publish_to_stream(js_ctx, "fleet.mac2.message", b'{}')
        assert ok is False

    @pytest.mark.asyncio
    async def test_publish_timeout_returns_false(self):
        from multifleet import jetstream as js
        js_ctx = MagicMock()

        async def _slow(*a, **kw):
            await asyncio.sleep(5)

        js_ctx.publish = _slow
        ok = await js.publish_to_stream(js_ctx, "fleet.x.message", b'{}', timeout_s=0.01)
        assert ok is False

    @pytest.mark.asyncio
    async def test_publish_returns_false_when_js_is_none(self):
        from multifleet import jetstream as js
        assert await js.publish_to_stream(None, "fleet.x", b"{}") is False


# ═══════════════════════════════════════════════════════════════════
# 5. replay_missed
# ═══════════════════════════════════════════════════════════════════

def _fake_msg(data: bytes):
    m = MagicMock()
    m.data = data
    m.subject = "fleet.mac1.message"
    m.ack = AsyncMock()
    m.nak = AsyncMock()
    return m


class TestReplayMissed:
    @pytest.mark.asyncio
    async def test_replays_and_acks_all_messages(self):
        from multifleet import jetstream as js

        msgs = [_fake_msg(b'{"i":1}'), _fake_msg(b'{"i":2}')]
        sub = MagicMock()
        # First fetch returns the batch, second returns empty → loop ends
        sub.fetch = AsyncMock(side_effect=[msgs, []])

        seen = []

        async def handler(msg):
            seen.append(msg.data)

        count = await js.replay_missed(sub, handler, batch_size=10, fetch_timeout_s=0.1)
        assert count == 2
        assert seen == [b'{"i":1}', b'{"i":2}']
        assert all(m.ack.await_count == 1 for m in msgs)

    @pytest.mark.asyncio
    async def test_nacks_on_handler_error(self):
        from multifleet import jetstream as js

        bad = _fake_msg(b'{"bad":true}')
        sub = MagicMock()
        sub.fetch = AsyncMock(side_effect=[[bad], []])

        async def bad_handler(msg):
            raise RuntimeError("boom")

        count = await js.replay_missed(sub, bad_handler, batch_size=10, fetch_timeout_s=0.1)
        assert count == 0  # not counted as delivered
        bad.ack.assert_not_awaited()
        bad.nak.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_fetch_ends_loop(self):
        from multifleet import jetstream as js
        sub = MagicMock()
        sub.fetch = AsyncMock(return_value=[])
        seen = []

        async def handler(msg):
            seen.append(msg)

        count = await js.replay_missed(sub, handler, batch_size=10, fetch_timeout_s=0.1)
        assert count == 0
        assert seen == []

    @pytest.mark.asyncio
    async def test_fetch_timeout_ends_loop(self):
        from multifleet import jetstream as js
        sub = MagicMock()
        sub.fetch = AsyncMock(side_effect=asyncio.TimeoutError())
        count = await js.replay_missed(sub, AsyncMock(), batch_size=5, fetch_timeout_s=0.01)
        assert count == 0

    @pytest.mark.asyncio
    async def test_respects_max_messages_cap(self):
        from multifleet import jetstream as js

        # Server keeps returning 5 per batch — cap at 7 means 2 rounds of 5 and 2
        def make_batch(n):
            return [_fake_msg(b'{"x":1}') for _ in range(n)]

        sub = MagicMock()
        sub.fetch = AsyncMock(side_effect=[make_batch(5), make_batch(5), make_batch(5)])
        delivered = []

        async def handler(msg):
            delivered.append(msg)

        count = await js.replay_missed(sub, handler, max_messages=7, batch_size=5, fetch_timeout_s=0.1)
        assert count == 7
        # Two fetch calls: first with 5, second with 2 (min(batch, remaining))
        assert sub.fetch.await_count == 2
        assert sub.fetch.await_args_list[0].args[0] == 5
        assert sub.fetch.await_args_list[1].args[0] == 2


# ═══════════════════════════════════════════════════════════════════
# 6. FleetNerveNATS integration (connect wiring)
# ═══════════════════════════════════════════════════════════════════

def _make_nerve_for_connect_tests(node_id="mac1"):
    """Build a FleetNerveNATS without real infra — mirrors test_nats_client helper."""
    with patch.dict(os.environ, {
        "MULTIFLEET_NODE_ID": node_id,
        "NATS_URL": "nats://localhost:4222",
    }):
        with patch("multifleet.nats_client.FleetDiscoveryManager"), \
             patch("multifleet.nats_client.SelfHealManager"), \
             patch("multifleet.nats_client.CommunicationProtocol"), \
             patch("multifleet.nats_client.SessionManager"):
            from multifleet.nats_client import FleetNerveNATS
            nerve = FleetNerveNATS(node_id=node_id, nats_url="nats://localhost:4222")
            return nerve


class TestFleetNerveJetStreamWiring:
    @pytest.mark.asyncio
    async def test_publish_mirrors_to_jetstream_when_enabled(self):
        """send() publishes to core NATS AND to JetStream when _js_enabled."""
        nerve = _make_nerve_for_connect_tests()
        nerve.nc = AsyncMock()
        nerve.nc.is_connected = True
        nerve.nc.publish = AsyncMock()
        nerve.js = MagicMock()
        nerve.js.publish = AsyncMock(return_value=MagicMock(seq=1))
        nerve._js_enabled = True

        await nerve.send("mac2", "context", "subj", "body")
        # Core NATS publish called
        assert nerve.nc.publish.await_count == 1
        # JetStream mirror called via module-level helper — js.publish awaited
        assert nerve.js.publish.await_count == 1

    @pytest.mark.asyncio
    async def test_publish_skips_jetstream_when_disabled(self):
        nerve = _make_nerve_for_connect_tests()
        nerve.nc = AsyncMock()
        nerve.nc.is_connected = True
        nerve.nc.publish = AsyncMock()
        nerve.js = MagicMock()
        nerve.js.publish = AsyncMock()
        nerve._js_enabled = False  # gate off

        await nerve.send("mac2", "context", "subj", "body")
        nerve.nc.publish.assert_awaited_once()
        nerve.js.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publish_mirror_failure_does_not_break_core_send(self):
        """A JetStream publish error must not propagate — core send must still succeed."""
        nerve = _make_nerve_for_connect_tests()
        nerve.nc = AsyncMock()
        nerve.nc.is_connected = True
        nerve.nc.publish = AsyncMock()
        nerve.js = MagicMock()
        nerve.js.publish = AsyncMock(side_effect=RuntimeError("stream gone"))
        nerve._js_enabled = True

        # Must not raise
        await nerve.send("mac2", "context", "subj", "body")
        assert nerve.nc.publish.await_count == 1
        assert nerve._stats["sent"] == 1
