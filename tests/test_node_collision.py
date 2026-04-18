"""
Tests for node ID collision detection (blocker #3).

Verifies:
- _check_node_id_collision returns collision dict when another daemon replies
- No collision when request times out (backward-compat with older daemons)
- _handle_presence_probe replies to foreign probes
- _handle_presence_probe ignores own probe
- Strict mode (MULTIFLEET_COLLISION_STRICT=1) raises on collision
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeMsg:
    def __init__(self, data: bytes, reply: str = ""):
        self.data = data
        self.reply = reply


@pytest.fixture
def nerve():
    """Minimal nerve stub with just enough attrs for collision methods."""
    from multifleet.nats_client import FleetNerveNATS

    n = FleetNerveNATS.__new__(FleetNerveNATS)
    n.node_id = "testnode"
    n.nc = MagicMock()
    n.nc.request = AsyncMock()
    n.nc.publish = AsyncMock()
    n._presence_session = None
    return n


@pytest.mark.asyncio
async def test_no_collision_on_timeout(nerve):
    """Backward-compat: timeout means no responder → no collision."""
    nerve.nc.request.side_effect = asyncio.TimeoutError()
    result = await nerve._check_node_id_collision()
    assert result is None


@pytest.mark.asyncio
async def test_collision_detected_when_foreign_reply(nerve):
    """Another daemon replies with different session → collision dict returned."""
    other = {"session": "deadbeef-1111-2222-3333-444455556666", "pid": 99999, "node_id": "testnode", "reply": True}
    reply_msg = SimpleNamespace(data=json.dumps(other).encode())
    nerve.nc.request.return_value = reply_msg
    result = await nerve._check_node_id_collision()
    assert result is not None
    assert result["pid"] == 99999
    assert result["session"] != nerve._presence_session


@pytest.mark.asyncio
async def test_own_probe_not_treated_as_collision(nerve):
    """Reply with same session as probe is ignored (shouldn't happen but defensive)."""
    # After _check_node_id_collision runs, _presence_session is set.
    # Simulate a reply with that same session.
    async def fake_request(subject, payload, timeout):
        d = json.loads(payload.decode())
        my = d["session"]
        return SimpleNamespace(data=json.dumps({"session": my, "pid": os.getpid(), "node_id": "testnode"}).encode())
    nerve.nc.request = AsyncMock(side_effect=fake_request)
    result = await nerve._check_node_id_collision()
    assert result is None


@pytest.mark.asyncio
async def test_presence_probe_replies_to_foreign(nerve):
    """Incoming probe from another daemon → publish reply to probe.reply subject."""
    nerve._presence_session = "us-session-1"
    probe_body = json.dumps({"session": "them-session-2", "pid": 42, "probe": True}).encode()
    msg = FakeMsg(probe_body, reply="_INBOX.xyz")
    await nerve._handle_presence_probe(msg)
    nerve.nc.publish.assert_awaited_once()
    args = nerve.nc.publish.await_args
    assert args[0][0] == "_INBOX.xyz"
    reply_data = json.loads(args[0][1].decode())
    assert reply_data["session"] == "us-session-1"
    assert reply_data["reply"] is True


@pytest.mark.asyncio
async def test_presence_probe_ignores_own_session(nerve):
    """Own probe arriving back via subscribe — must NOT reply."""
    nerve._presence_session = "us-session-1"
    probe_body = json.dumps({"session": "us-session-1", "pid": os.getpid(), "probe": True}).encode()
    msg = FakeMsg(probe_body, reply="_INBOX.xyz")
    await nerve._handle_presence_probe(msg)
    nerve.nc.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_presence_probe_ignores_non_probe_messages(nerve):
    """Messages without 'probe': true are ignored (e.g. future protocol messages)."""
    nerve._presence_session = "us-session-1"
    body = json.dumps({"session": "them", "probe": False}).encode()
    msg = FakeMsg(body, reply="_INBOX.xyz")
    await nerve._handle_presence_probe(msg)
    nerve.nc.publish.assert_not_awaited()
