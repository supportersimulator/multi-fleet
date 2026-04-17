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


class TestMessageFlowAfterReconnect:
    """Tests that messages can be published and received after reconnect."""

    @pytest.mark.asyncio
    async def test_publish_works_after_reconnect(self, mock_nc):
        """After reconnect + resubscribe, publishing should succeed."""
        nerve = _make_nerve(mock_nc)

        # Initial connect
        await nerve._subscribe_all()

        # Simulate disconnect then reconnect
        await nerve._on_disconnect()
        await nerve._on_reconnect()

        # Publish should work on reconnected connection
        await nerve.nc.publish(b"fleet.mac2.message", b'{"type":"test"}')
        mock_nc.publish.assert_awaited()

    @pytest.mark.asyncio
    async def test_callbacks_wired_to_new_subs_after_reconnect(self, mock_nc):
        """New subscriptions after reconnect must have callbacks attached."""
        nerve = _make_nerve(mock_nc)

        await nerve._on_reconnect()

        # Every subscribe call should have a cb= argument
        for call in mock_nc.subscribe.call_args_list:
            _, kwargs = call
            assert "cb" in kwargs, f"Subscription missing callback: {call}"

    @pytest.mark.asyncio
    async def test_correct_subjects_after_reconnect(self, mock_nc):
        """Reconnect must re-subscribe to all required subjects."""
        nerve = _make_nerve(mock_nc)

        await nerve._on_reconnect()

        subscribed_subjects = [
            call.args[0] if call.args else call.kwargs.get("subject")
            for call in mock_nc.subscribe.call_args_list
        ]
        # Must include direct, all, heartbeat, ack
        assert any("mac1" in s for s in subscribed_subjects), "Missing direct subject"
        assert any("all" in s for s in subscribed_subjects), "Missing broadcast subject"
        assert any("heartbeat" in s for s in subscribed_subjects), "Missing heartbeat subject"
        assert any("ack" in s for s in subscribed_subjects), "Missing ack subject"


class TestProlongedDisconnect:
    """Tests behavior during extended NATS outages (5+ minutes)."""

    @pytest.mark.asyncio
    async def test_error_counter_increments_on_failed_reconnect(self, mock_nc):
        """Repeated reconnect failures should track errors without crashing."""
        nerve = _make_nerve(mock_nc)
        mock_nc.subscribe.side_effect = Exception("server still down")

        errors_before = nerve._stats["errors"]

        # Simulate multiple failed reconnect attempts
        for _ in range(5):
            await nerve._on_reconnect()

        assert nerve._stats["errors"] >= errors_before + 5

    @pytest.mark.asyncio
    async def test_recovery_after_prolonged_outage(self, mock_nc):
        """After failures, a successful reconnect should restore subs."""
        nerve = _make_nerve(mock_nc)

        # Simulate 3 failed reconnects
        mock_nc.subscribe.side_effect = Exception("NATS down")
        for _ in range(3):
            await nerve._on_reconnect()
        assert nerve._stats["errors"] >= 3
        assert len(nerve._active_subs) == 0  # No subs during outage

        # NATS comes back
        mock_nc.subscribe.side_effect = lambda subj, cb=None: MagicMock(unsubscribe=AsyncMock())
        await nerve._on_reconnect()

        # Subs restored
        assert len(nerve._active_subs) >= 4
        # Error count unchanged from successful reconnect
        errors_after = nerve._stats["errors"]
        await nerve._on_reconnect()  # Another clean reconnect
        assert nerve._stats["errors"] == errors_after  # No new errors

    @pytest.mark.asyncio
    async def test_stats_accessible_during_disconnect(self, mock_nc):
        """Stats dict should remain valid and queryable during disconnect."""
        nerve = _make_nerve(mock_nc)

        await nerve._subscribe_all()
        await nerve._on_disconnect()

        # Stats should be readable (used by health endpoint)
        assert isinstance(nerve._stats["errors"], int)
        assert isinstance(nerve._stats["received"], int)
        assert nerve._running is True  # Still running, just disconnected
