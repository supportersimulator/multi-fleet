"""
Chaos testing foundation — code-level resilience tests (no live network).

Tests crash recovery, concurrent writes, network partition simulation,
message loss recovery, and resource exhaustion using mocks and tmp_path.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. Daemon crash recovery ────────────────────────────────────────────────


class TestCrashRecovery:
    """TaskStateMachine recovers from daemon crashes (stale tasks, expired cleanup)."""

    def test_resume_stale_reassigns_expired_lease(self, tmp_path):
        """Stale in_progress tasks get reassigned on startup."""
        from multifleet.task_state import TaskStateMachine, TaskState

        tsm = TaskStateMachine(db_path=tmp_path / "tasks.db")
        tid = tsm.create_task("mac2", "mac1", {"cmd": "fix"}, lease_s=1)
        tsm.transition(tid, TaskState.ASSIGNED, "dispatched")
        tsm.transition(tid, TaskState.IN_PROGRESS, "started")
        # Backdate the lease so it looks expired
        conn = tsm._conn()
        conn.execute("UPDATE task_states SET leased_at = ? WHERE id = ?", (time.time() - 100, tid))
        conn.commit()
        conn.close()

        reassigned = tsm.resume_stale()
        assert tid in reassigned
        task = tsm.get(tid)
        assert task["state"] == "assigned"
        assert task["retries"] == 1

    def test_expire_old_created_cleans_up(self, tmp_path):
        """Old created/assigned tasks get expired on startup."""
        from multifleet.task_state import TaskStateMachine

        tsm = TaskStateMachine(db_path=tmp_path / "tasks.db")
        tid = tsm.create_task("mac2", "mac1", {"cmd": "stale"})
        # Backdate creation
        conn = tsm._conn()
        conn.execute("UPDATE task_states SET created_at = ? WHERE id = ?", (time.time() - 7200, tid))
        conn.commit()
        conn.close()

        count = tsm.expire_old_created(max_age_s=3600)
        assert count == 1
        assert tsm.get(tid)["state"] == "expired"

    def test_maintenance_handles_store_errors(self, tmp_path):
        """resume_stale doesn't crash if the DB has unexpected state."""
        from multifleet.task_state import TaskStateMachine, TaskState

        tsm = TaskStateMachine(db_path=tmp_path / "tasks.db")
        # No in-progress tasks — resume_stale should return empty, not crash
        result = tsm.resume_stale()
        assert result == []
        # Stats should also work on empty DB
        assert tsm.stats() == {}


# ── 2. Concurrent write safety ──────────────────────────────────────────────


class TestConcurrentWrites:
    """SQLite stores handle concurrent access without corruption."""

    def test_store_concurrent_messages(self, tmp_path):
        """Two threads writing messages simultaneously don't corrupt the store.

        Each thread gets its own FleetNerveStore instance (own connection),
        matching real daemon usage where multiple coroutines/threads access
        the same SQLite file via WAL mode.
        """
        db_path = tmp_path / "msg.db"
        # Init schema once
        from multifleet.store import FleetNerveStore
        FleetNerveStore(db_path=db_path)
        errors = []

        def writer(prefix, count):
            try:
                store = FleetNerveStore(db_path=db_path)
                for i in range(count):
                    store.store_message({
                        "id": f"{prefix}-{i}",
                        "type": "context",
                        "from": prefix,
                        "to": "mac2",
                        "payload": {"subject": f"msg-{i}", "body": "x"},
                    })
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer, args=("mac1", 50))
        t2 = threading.Thread(target=writer, args=("mac3", 50))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors, f"Concurrent writes failed: {errors}"
        # Both writers' messages should be stored
        reader = FleetNerveStore(db_path=db_path)
        pending = reader.pending_for_node("mac2")
        assert len(pending) == 100

    def test_packet_registry_concurrent_register(self, tmp_path):
        """Concurrent register calls don't corrupt the registry."""
        from multifleet.packet_registry import PacketRegistry

        reg = PacketRegistry()
        errors = []

        def register_types(prefix, count):
            try:
                for i in range(count):
                    reg.register(f"{prefix}-type-{i}", owner=prefix, description=f"test {i}")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=register_types, args=("owner1", 50))
        t2 = threading.Thread(target=register_types, args=("owner2", 50))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors
        # All types from both threads should be registered
        for prefix in ("owner1", "owner2"):
            for i in range(50):
                assert reg.get(f"{prefix}-type-{i}") is not None

    def test_dedup_tracker_concurrent_check_and_mark(self):
        """Concurrent check_and_mark calls are safe (no crashes, no lost marks)."""
        from multifleet.packets import DedupTracker

        tracker = DedupTracker(max_size=5000)
        errors = []

        def mark_range(start, count):
            try:
                for i in range(start, start + count):
                    tracker.check_and_mark(f"pkt-{i}")
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=mark_range, args=(0, 500))
        t2 = threading.Thread(target=mark_range, args=(500, 500))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors
        # All 1000 should be tracked (within max_size)
        assert len(tracker) == 1000


# ── 3. Network partition simulation ─────────────────────────────────────────


class TestNetworkPartition:
    """send_with_fallback cascades correctly under simulated partitions."""

    @pytest.mark.asyncio
    async def test_p1_fail_falls_to_p2(self, mock_nerve, mock_nats_client):
        """P1 NATS down -> falls to P2 HTTP."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "chaos", "body": "partition"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_p1_p2_fail_falls_to_p5_ssh(self, mock_nerve, mock_nats_client):
        """P1+P2 down -> skips P3 (self is chief) + P4 -> falls to P5 SSH."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac1"  # self is chief, P3 skipped
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "refused"})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False, "error": "no fs"})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "chaos", "body": "deep partition"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P5_ssh"

    @pytest.mark.asyncio
    async def test_total_partition_returns_error_not_hang(self, mock_nerve, mock_nats_client):
        """All channels fail -> returns error dict (no hang, no exception)."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p6_wol = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p7_git = AsyncMock(return_value={"delivered": False, "error": "down"})
        mock_nerve._send_p0_cloud = AsyncMock(return_value={"delivered": False, "error": "down"})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "chaos", "body": "total partition"},
        })
        assert result["delivered"] is False
        assert "errors" in result


# ── 4. Message loss recovery ────────────────────────────────────────────────


class TestMessageLossRecovery:
    """ACK timeout triggers retry; max retries marks failed."""

    def test_ack_timeout_surfaces_for_retry(self, tmp_path):
        """Messages awaiting ACK past timeout appear in get_awaiting_ack."""
        from multifleet.store import FleetNerveStore

        store = FleetNerveStore(db_path=tmp_path / "msg.db")
        store.store_message({
            "id": "msg-retry-1", "type": "context", "from": "mac1", "to": "mac2",
            "payload": {"subject": "important", "body": "data"},
        })
        store.update_ack_status("msg-retry-1", "awaiting")
        # Backdate created_at so it exceeds timeout
        with store._conn() as conn:
            conn.execute("UPDATE messages SET created_at = ? WHERE id = ?",
                         (time.time() - 120, "msg-retry-1"))

        pending = store.get_awaiting_ack(max_age_s=60, max_retries=3)
        assert len(pending) == 1
        assert pending[0]["id"] == "msg-retry-1"
        # Bump retry
        store.bump_retry("msg-retry-1")
        status = store.get_ack_status("msg-retry-1")
        assert status["retry_count"] == 1

    def test_max_retries_marks_failed(self, tmp_path):
        """After exhausting retries, message is marked failed."""
        from multifleet.store import FleetNerveStore

        store = FleetNerveStore(db_path=tmp_path / "msg.db")
        store.store_message({
            "id": "msg-fail-1", "type": "context", "from": "mac1", "to": "mac2",
            "payload": {"subject": "lost", "body": "data"},
        })
        store.update_ack_status("msg-fail-1", "awaiting")
        # Simulate 3 retries
        for _ in range(3):
            store.bump_retry("msg-fail-1")
        store.mark_ack_failed("msg-fail-1")

        status = store.get_ack_status("msg-fail-1")
        assert status["ack_status"] == "failed"
        assert status["retry_count"] == 3


# ── 5. Resource exhaustion ──────────────────────────────────────────────────


class TestResourceExhaustion:
    """Bounded data structures evict correctly; large payloads don't crash."""

    def test_dedup_tracker_evicts_at_capacity(self):
        """DedupTracker at max_size evicts oldest entries (LRU)."""
        from multifleet.packets import DedupTracker

        tracker = DedupTracker(max_size=10)
        for i in range(15):
            tracker.check_and_mark(f"pkt-{i}")

        assert len(tracker) == 10
        # Oldest 5 should be evicted
        for i in range(5):
            assert not tracker.is_duplicate(f"pkt-{i}"), f"pkt-{i} should have been evicted"
        # Newest 5 should still be tracked
        for i in range(10, 15):
            assert tracker.is_duplicate(f"pkt-{i}"), f"pkt-{i} should still be tracked"

    def test_store_handles_large_payload(self, tmp_path):
        """Store accepts 1MB+ message payloads without crashing."""
        from multifleet.store import FleetNerveStore

        store = FleetNerveStore(db_path=tmp_path / "msg.db")
        large_body = "x" * (1024 * 1024)  # 1MB
        store.store_message({
            "id": "msg-large-1", "type": "context", "from": "mac1", "to": "mac2",
            "payload": {"subject": "big", "body": large_body},
        })
        msg = store.get_message("msg-large-1")
        assert msg is not None
        assert msg["id"] == "msg-large-1"
