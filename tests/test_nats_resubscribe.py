"""Tests for NATS disconnect/reconnect resubscription logic.

Verifies that after NATS disconnect+reconnect:
- Subscriptions are re-established
- The resubscribe callback fires
- Subscription list is maintained across disconnect cycles
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_nc():
    """Mock NATS connection with subscription tracking."""
    nc = AsyncMock()
    nc.is_connected = True
    nc.subscribe = AsyncMock(side_effect=lambda subj, cb=None: MagicMock(unsubscribe=AsyncMock()))
    nc.publish = AsyncMock()
    nc.close = AsyncMock()
    return nc


def _make_nerve(nc):
    """Create a FleetNerveNATS with mocked connection, bypassing real connect()."""
    import os
    with patch.dict(os.environ, {
        "MULTIFLEET_NODE_ID": "mac1",
        "NATS_URL": "nats://localhost:4222",
    }):
        from multifleet.nats_client import FleetNerveNATS
        nerve = FleetNerveNATS.__new__(FleetNerveNATS)
        nerve.node_id = "mac1"
        nerve.nats_url = "nats://localhost:4222"
        nerve.nc = nc
        nerve.js = None
        nerve._active_subs = []
        nerve._stats = {"received": 0, "sent": 0, "broadcasts": 0, "errors": 0}
        nerve._peers = {}
        nerve._running = True
        nerve._store = None
        nerve._channel_health = {}
        nerve._heal_in_flight = {}
        nerve._pending_acks = {}
        nerve._chains = {}
        nerve._inbox = {}
        nerve._loop = None
        return nerve


class TestSubscribeAll:
    """Tests for _subscribe_all() — the core resubscription method."""

    @pytest.mark.asyncio
    async def test_subscribe_all_creates_subscriptions(self, mock_nc):
        nerve = _make_nerve(mock_nc)

        await nerve._subscribe_all()

        # Should have created at least 4 subscriptions:
        # direct, all, heartbeat, ack
        assert mock_nc.subscribe.call_count >= 4
        assert len(nerve._active_subs) >= 4

    @pytest.mark.asyncio
    async def test_subscribe_all_clears_old_subs_first(self, mock_nc):
        nerve = _make_nerve(mock_nc)

        # Pre-populate with old subs
        old_sub = MagicMock(unsubscribe=AsyncMock())
        nerve._active_subs = [old_sub]

        await nerve._subscribe_all()

        # Old sub should have been unsubscribed
        old_sub.unsubscribe.assert_awaited_once()
        # New subs should replace old ones
        assert old_sub not in nerve._active_subs
        assert len(nerve._active_subs) >= 4

    @pytest.mark.asyncio
    async def test_subscribe_all_handles_old_unsub_failure(self, mock_nc):
        """Old subscription unsub failure should not prevent new subscriptions."""
        nerve = _make_nerve(mock_nc)

        broken_sub = MagicMock(unsubscribe=AsyncMock(side_effect=Exception("already closed")))
        nerve._active_subs = [broken_sub]

        # Should not raise
        await nerve._subscribe_all()
        assert len(nerve._active_subs) >= 4


class TestOnReconnect:
    """Tests for _on_reconnect callback — fired by NATS client after reconnect."""

    @pytest.mark.asyncio
    async def test_on_reconnect_resubscribes(self, mock_nc):
        nerve = _make_nerve(mock_nc)

        await nerve._on_reconnect()

        # _subscribe_all should have been called (creates new subs)
        assert mock_nc.subscribe.call_count >= 4
        assert len(nerve._active_subs) >= 4

    @pytest.mark.asyncio
    async def test_on_reconnect_handles_subscribe_failure(self, mock_nc):
        """If resubscribe fails, error count increments but no crash."""
        nerve = _make_nerve(mock_nc)
        mock_nc.subscribe.side_effect = Exception("NATS unavailable")

        await nerve._on_reconnect()

        assert nerve._stats["errors"] >= 1

    @pytest.mark.asyncio
    async def test_on_reconnect_replaces_stale_subs(self, mock_nc):
        """Reconnect should clean up pre-disconnect subs and create fresh ones."""
        nerve = _make_nerve(mock_nc)

        # First subscribe (simulates initial connect)
        await nerve._subscribe_all()
        first_subs = list(nerve._active_subs)
        assert len(first_subs) >= 4

        # Reset call count to track reconnect calls
        mock_nc.subscribe.reset_mock()

        # Simulate reconnect
        await nerve._on_reconnect()

        # Should have created new subs
        assert mock_nc.subscribe.call_count >= 4
        # Old subs should have been cleaned up (unsubscribed)
        for sub in first_subs:
            sub.unsubscribe.assert_awaited()


class TestSubscriptionListMaintenance:
    """Tests that _active_subs list stays consistent across disconnect cycles."""

    @pytest.mark.asyncio
    async def test_multiple_reconnect_cycles(self, mock_nc):
        """Subscription list stays bounded after multiple reconnect cycles."""
        nerve = _make_nerve(mock_nc)

        await nerve._subscribe_all()
        initial_count = len(nerve._active_subs)

        # Simulate 5 disconnect/reconnect cycles
        for _ in range(5):
            await nerve._on_reconnect()

        # Count should stay the same (old subs cleared each time)
        assert len(nerve._active_subs) == initial_count

    @pytest.mark.asyncio
    async def test_active_subs_empty_before_first_subscribe(self, mock_nc):
        nerve = _make_nerve(mock_nc)
        assert nerve._active_subs == []

    @pytest.mark.asyncio
    async def test_disconnect_does_not_clear_sub_list(self, mock_nc):
        """_on_disconnect only logs — subs stay in list for reconnect cleanup."""
        nerve = _make_nerve(mock_nc)

        await nerve._subscribe_all()
        count_before = len(nerve._active_subs)

        await nerve._on_disconnect()

        # Subs still tracked (reconnect callback will clean them up)
        assert len(nerve._active_subs) == count_before
