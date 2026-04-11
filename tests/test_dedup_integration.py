"""Tests for deduplication at the store level — FleetNerveStore message handling.

Covers: duplicate message insert behavior (INSERT OR REPLACE), WAL dedup,
peer upsert idempotency, outbox queuing, ACK lifecycle, relay dedup,
task run tracking, work item coordination, and stats persistence.
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.store import FleetNerveStore


@pytest.fixture
def store(tmp_path):
    """Create a FleetNerveStore backed by a temp SQLite DB."""
    return FleetNerveStore(db_path=tmp_path / "test.db")


def _msg(msg_id="msg-001", type_="context", from_node="mac1", to_node="mac2",
         subject="test", body="hello"):
    return {
        "id": msg_id,
        "type": type_,
        "from": from_node,
        "to": to_node,
        "payload": {"subject": subject, "body": body},
    }


# ── Message dedup via INSERT OR REPLACE ──────────────────────────────────────


class TestMessageDedup:
    """store_message uses INSERT OR REPLACE — same ID overwrites, not duplicates."""

    def test_insert_single_message(self, store):
        store.store_message(_msg("msg-001"))
        result = store.get_message("msg-001")
        assert result is not None
        assert result["id"] == "msg-001"
        assert result["type"] == "context"

    def test_same_id_overwrites_not_duplicates(self, store):
        store.store_message(_msg("msg-001", subject="first"))
        store.store_message(_msg("msg-001", subject="second"))
        result = store.get_message("msg-001")
        assert result is not None
        # body is JSON-encoded payload
        payload = json.loads(result["body"])
        assert payload["subject"] == "second"

    def test_different_ids_both_stored(self, store):
        store.store_message(_msg("msg-001"))
        store.store_message(_msg("msg-002"))
        assert store.get_message("msg-001") is not None
        assert store.get_message("msg-002") is not None

    def test_pending_for_node_returns_only_target(self, store):
        store.store_message(_msg("msg-001", to_node="mac2"))
        store.store_message(_msg("msg-002", to_node="mac3"))
        pending = store.pending_for_node("mac2")
        assert len(pending) == 1
        assert pending[0]["id"] == "msg-001"

    def test_mark_delivered_removes_from_pending(self, store):
        store.store_message(_msg("msg-001", to_node="mac2"))
        store.mark_delivered("msg-001")
        pending = store.pending_for_node("mac2")
        assert len(pending) == 0
        msg = store.get_message("msg-001")
        assert msg["status"] == "delivered"

    def test_get_nonexistent_returns_none(self, store):
        assert store.get_message("does-not-exist") is None


# ── WAL dedup ────────────────────────────────────────────────────────────────


class TestWalDedup:
    """WAL allows duplicate msg_ids (crash-replay design) — no UNIQUE constraint on msg_id."""

    def test_wal_write_and_retrieve(self, store):
        wal_id = store.wal_write("msg-001", '{"type":"heartbeat"}')
        assert wal_id > 0
        unprocessed = store.wal_unprocessed()
        assert len(unprocessed) == 1
        assert unprocessed[0]["msg_id"] == "msg-001"

    def test_wal_same_msg_id_creates_two_entries(self, store):
        """WAL intentionally allows dups — crash recovery replays all unprocessed."""
        store.wal_write("msg-001", '{"attempt":1}')
        store.wal_write("msg-001", '{"attempt":2}')
        unprocessed = store.wal_unprocessed()
        assert len(unprocessed) == 2

    def test_wal_mark_processed_removes_from_unprocessed(self, store):
        wal_id = store.wal_write("msg-001", '{}')
        store.wal_mark_processed(wal_id)
        assert len(store.wal_unprocessed()) == 0

    def test_wal_prune_removes_old_processed(self, store):
        wal_id = store.wal_write("msg-001", '{}')
        store.wal_mark_processed(wal_id)
        # Force old timestamp
        store._conn().execute(
            "UPDATE wal SET processed_at = ? WHERE id = ?",
            (time.time() - 200000, wal_id),
        )
        store._conn().commit()
        store.wal_prune(max_age_hours=1)
        # Processed + old = pruned
        # Verify by trying to read it directly
        row = store._conn().execute("SELECT * FROM wal WHERE id = ?", (wal_id,)).fetchone()
        assert row is None


# ── ACK lifecycle ────────────────────────────────────────────────────────────


class TestAckLifecycle:
    def test_ack_status_default_none(self, store):
        store.store_message(_msg("msg-001"))
        ack = store.get_ack_status("msg-001")
        assert ack["ack_status"] == "none"

    def test_update_ack_status(self, store):
        store.store_message(_msg("msg-001"))
        store.update_ack_status("msg-001", "awaiting")
        assert store.get_ack_status("msg-001")["ack_status"] == "awaiting"

    def test_bump_retry_increments(self, store):
        store.store_message(_msg("msg-001"))
        store.update_ack_status("msg-001", "awaiting")
        store.bump_retry("msg-001")
        store.bump_retry("msg-001")
        ack = store.get_ack_status("msg-001")
        assert ack["retry_count"] == 2

    def test_mark_ack_failed(self, store):
        store.store_message(_msg("msg-001"))
        store.mark_ack_failed("msg-001")
        ack = store.get_ack_status("msg-001")
        assert ack["ack_status"] == "failed"

    def test_get_ack_status_nonexistent(self, store):
        assert store.get_ack_status("nope") is None


# ── Peer upsert idempotency ─────────────────────────────────────────────────


class TestPeerUpsert:
    def test_upsert_creates_peer(self, store):
        store.upsert_peer("mac1", "192.168.1.10", 8855)
        peer = store.get_peer("mac1")
        assert peer is not None
        assert peer["ip"] == "192.168.1.10"
        assert peer["status"] == "online"

    def test_upsert_updates_existing(self, store):
        store.upsert_peer("mac1", "192.168.1.10")
        store.upsert_peer("mac1", "192.168.1.20")
        peer = store.get_peer("mac1")
        assert peer["ip"] == "192.168.1.20"

    def test_get_peers_returns_all(self, store):
        store.upsert_peer("mac1", "10.0.0.1")
        store.upsert_peer("mac2", "10.0.0.2")
        peers = store.get_peers()
        assert len(peers) == 2

    def test_update_heartbeat(self, store):
        store.upsert_peer("mac1", "10.0.0.1")
        store.update_heartbeat("mac1", seq=5)
        peer = store.get_peer("mac1")
        assert peer["last_seq"] == 5

    def test_get_nonexistent_peer(self, store):
        assert store.get_peer("ghost") is None


# ── Outbox ───────────────────────────────────────────────────────────────────


class TestOutbox:
    def test_queue_and_retrieve(self, store):
        store.store_message(_msg("msg-001"))
        store.queue_outbox("msg-001", "mac2")
        pending = store.pending_outbox()
        assert len(pending) >= 1
        assert pending[0]["target_node"] == "mac2"

    def test_remove_outbox(self, store):
        store.store_message(_msg("msg-001"))
        store.queue_outbox("msg-001", "mac2")
        pending = store.pending_outbox()
        store.remove_outbox(pending[0]["id"])
        assert len(store.pending_outbox()) == 0

    def test_bump_outbox_retry_delays_next(self, store):
        store.store_message(_msg("msg-001"))
        store.queue_outbox("msg-001", "mac2")
        pending = store.pending_outbox()
        outbox_id = pending[0]["id"]
        store.bump_outbox_retry(outbox_id)
        # After bump, next_retry is in the future so pending_outbox may not return it
        row = store._conn().execute("SELECT attempts FROM outbox WHERE id = ?", (outbox_id,)).fetchone()
        assert row[0] == 1


# ── Relay dedup (P3 chief relay) ─────────────────────────────────────────────


class TestRelayDedup:
    def test_relay_queue_creates_pending(self, store):
        msg_id = store.relay_queue(_msg("relay-001", to_node="mac3"))
        assert msg_id == "relay-001"
        pending = store.relay_pending("mac3")
        assert len(pending) == 1

    def test_relay_queue_dedup_skips_existing_pending(self, store):
        store.relay_queue(_msg("relay-001", to_node="mac3"))
        store.relay_queue(_msg("relay-001", to_node="mac3", subject="updated"))
        pending = store.relay_pending("mac3")
        assert len(pending) == 1
        # Subject should be original (dedup skipped the second)
        assert pending[0]["payload"]["subject"] == "test"

    def test_relay_ack_marks_delivered(self, store):
        store.relay_queue(_msg("relay-001", to_node="mac3"))
        assert store.relay_ack("relay-001") is True
        pending = store.relay_pending("mac3")
        assert len(pending) == 0

    def test_relay_ack_nonexistent_returns_false(self, store):
        assert store.relay_ack("nope") is False


# ── Task run tracking ────────────────────────────────────────────────────────


class TestTaskRuns:
    def test_start_and_complete(self, store):
        store.task_run_start("t-1", "msg-001", "mac1", "mac2", "deploy", pid=1234)
        active = store.task_runs_active("mac1")
        assert len(active) == 1
        assert active[0]["status"] == "running"

        store.task_run_complete("t-1", exit_code=0, result_summary="ok")
        active = store.task_runs_active("mac1")
        assert len(active) == 0

    def test_task_run_timeout(self, store):
        store.task_run_start("t-1", "msg-001", "mac1", "mac2", "deploy")
        store.task_run_timeout("t-1")
        recent = store.task_runs_recent(hours=1)
        assert recent[0]["status"] == "timeout"

    def test_task_runs_check_timeouts(self, store):
        store.task_run_start("t-1", "msg-001", "mac1", "mac2", "deploy")
        # Force old spawn time
        store._conn().execute(
            "UPDATE task_runs SET spawned_at = ? WHERE id = ?",
            (time.time() - 1000, "t-1"),
        )
        store._conn().commit()
        store.task_runs_check_timeouts(timeout_s=600)
        recent = store.task_runs_recent(hours=1)
        assert recent[0]["status"] == "timeout"


# ── Work item coordination ───────────────────────────────────────────────────


class TestWorkItems:
    def test_work_start_and_active(self, store):
        store.work_start("w-1", "mac1", "refactor auth", priority=1)
        active = store.work_active("mac1")
        assert len(active) == 1
        assert active[0]["title"] == "refactor auth"

    def test_work_update_to_completed(self, store):
        store.work_start("w-1", "mac1", "task")
        store.work_update("w-1", status="completed", result_summary="done")
        active = store.work_active("mac1")
        assert len(active) == 0

    def test_work_is_claimed(self, store):
        store.work_start("w-1", "mac1", "deploy v2")
        assert store.work_is_claimed("deploy v2") is True
        assert store.work_is_claimed("other task") is False

    def test_work_add_pending_and_next_unclaimed(self, store):
        store.work_add_pending("w-1", "task A", priority=2)
        store.work_add_pending("w-2", "task B", priority=1)
        unclaimed = store.work_next_unclaimed("mac1")
        assert unclaimed is not None
        assert unclaimed["title"] == "task B"  # higher priority (lower number)

    def test_work_next_unclaimed_skips_active(self, store):
        store.work_add_pending("w-1", "task A", priority=1)
        store.work_start("w-2", "mac2", "task A", priority=1)  # claimed by mac2
        unclaimed = store.work_next_unclaimed("mac1")
        # task A is in_progress on mac2, so w-1 pending with same title gets skipped
        assert unclaimed is None


# ── Stats persistence ────────────────────────────────────────────────────────


class TestStatsPersistence:
    def test_save_and_load_stats(self, store):
        stats = {"messages_sent": 42, "uptime_s": 3600}
        store.save_stats("mac1", stats)
        loaded = store.load_stats("mac1")
        assert loaded["messages_sent"] == 42

    def test_load_stats_nonexistent_returns_empty(self, store):
        assert store.load_stats("ghost") == {}

    def test_save_stats_overwrites(self, store):
        store.save_stats("mac1", {"v": 1})
        store.save_stats("mac1", {"v": 2})
        assert store.load_stats("mac1")["v"] == 2


# ── WAL replication ──────────────────────────────────────────────────────────


class TestWalReplication:
    def test_replica_write_and_retrieve(self, store):
        rid = store.wal_replica_write("msg-001", '{"data":1}', "mac1", "mac2")
        assert rid > 0
        replicas = store.wal_replicas_for_node("mac2")
        assert len(replicas) == 1
        assert replicas[0]["source_node"] == "mac1"

    def test_replica_mark_delivered(self, store):
        rid = store.wal_replica_write("msg-001", '{}', "mac1", "mac2")
        store.wal_replica_mark_delivered(rid)
        replicas = store.wal_replicas_for_node("mac2")
        assert len(replicas) == 0  # delivered = excluded

    def test_replica_prune_old(self, store):
        rid = store.wal_replica_write("msg-001", '{}', "mac1", "mac2")
        store.wal_replica_mark_delivered(rid)
        # Force old delivered_at
        store._conn().execute(
            "UPDATE wal_replicas SET delivered_at = ? WHERE id = ?",
            (time.time() - 200000, rid),
        )
        store._conn().commit()
        store.wal_replica_prune(max_age_hours=1)
        row = store._conn().execute("SELECT * FROM wal_replicas WHERE id = ?", (rid,)).fetchone()
        assert row is None


# ── Expiry ───────────────────────────────────────────────────────────────────


class TestExpiry:
    def test_expire_old_marks_expired(self, store):
        store.store_message({**_msg("msg-001"), "ttl_hours": 0})
        # Force old created_at
        store._conn().execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (time.time() - 7200, "msg-001"),
        )
        store._conn().commit()
        store.expire_old()
        msg = store.get_message("msg-001")
        assert msg["status"] == "expired"

    def test_expire_old_leaves_fresh_messages(self, store):
        store.store_message(_msg("msg-001"))
        store.expire_old()
        msg = store.get_message("msg-001")
        assert msg["status"] == "pending"
