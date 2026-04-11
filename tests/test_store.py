"""Tests for multifleet.store — SQLite-backed message persistence, WAL, outbox, peers, stats."""

import json
import time

import pytest

from multifleet.store import FleetNerveStore


@pytest.fixture
def store(tmp_path):
    """Create a FleetNerveStore backed by a temporary SQLite database."""
    db = tmp_path / "test_messages.db"
    return FleetNerveStore(db_path=db)


def _make_msg(msg_id="msg-1", msg_type="context", from_node="mac1", to_node="mac2",
              subject="test", body="hello", ttl_hours=24, priority=2):
    return {
        "id": msg_id,
        "type": msg_type,
        "from": from_node,
        "to": to_node,
        "priority": priority,
        "ttl_hours": ttl_hours,
        "payload": {"subject": subject, "body": body},
    }


# ── Store creation ──


class TestStoreCreation:
    def test_creates_db_file(self, tmp_path):
        db = tmp_path / "sub" / "messages.db"
        FleetNerveStore(db_path=db)
        assert db.exists()

    def test_tables_created(self, store):
        """All expected tables should exist after init."""
        conn = store._conn()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for expected in ("messages", "peers", "outbox", "wal", "wal_replicas", "work_items", "task_runs"):
            assert expected in tables, f"Missing table: {expected}"

    def test_wal_journal_mode(self, store):
        row = store._conn().execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"


# ── Message insertion and retrieval ──


class TestMessageCRUD:
    def test_store_and_get(self, store):
        store.store_message(_make_msg())
        msg = store.get_message("msg-1")
        assert msg is not None
        assert msg["from_node"] == "mac1"
        assert msg["to_node"] == "mac2"
        assert msg["status"] == "pending"

    def test_get_nonexistent(self, store):
        assert store.get_message("nope") is None

    def test_pending_for_node(self, store):
        store.store_message(_make_msg("m1", to_node="mac2"))
        store.store_message(_make_msg("m2", to_node="mac3"))
        store.store_message(_make_msg("m3", to_node="mac2"))
        pending = store.pending_for_node("mac2")
        assert len(pending) == 2
        assert all(m["to_node"] == "mac2" for m in pending)

    def test_payload_stored_as_json(self, store):
        store.store_message(_make_msg(subject="deploy", body="v2.0"))
        msg = store.get_message("msg-1")
        payload = json.loads(msg["body"])
        assert payload["subject"] == "deploy"
        assert payload["body"] == "v2.0"


# ── Status updates ──


class TestStatusUpdates:
    def test_mark_delivered(self, store):
        store.store_message(_make_msg())
        store.mark_delivered("msg-1")
        msg = store.get_message("msg-1")
        assert msg["status"] == "delivered"
        assert msg["delivered_at"] is not None

    def test_expire_old(self, store):
        msg = _make_msg(ttl_hours=0)  # expires immediately
        store.store_message(msg)
        # Manually backdate created_at so ttl check works
        store._conn().execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (time.time() - 10, "msg-1"),
        )
        store._conn().commit()
        store.expire_old()
        m = store.get_message("msg-1")
        assert m["status"] == "expired"

    def test_delivered_not_expired(self, store):
        store.store_message(_make_msg(ttl_hours=0))
        store.mark_delivered("msg-1")
        store._conn().execute(
            "UPDATE messages SET created_at = ? WHERE id = ?",
            (time.time() - 10, "msg-1"),
        )
        store._conn().commit()
        store.expire_old()
        m = store.get_message("msg-1")
        assert m["status"] == "delivered"  # not expired because already delivered


# ── Dedup (INSERT OR REPLACE) ──


class TestDedup:
    def test_replace_on_duplicate_id(self, store):
        store.store_message(_make_msg(subject="first"))
        store.store_message(_make_msg(subject="second"))
        msg = store.get_message("msg-1")
        payload = json.loads(msg["body"])
        assert payload["subject"] == "second"

    def test_different_ids_coexist(self, store):
        store.store_message(_make_msg("a"))
        store.store_message(_make_msg("b"))
        assert store.get_message("a") is not None
        assert store.get_message("b") is not None


# ── Outbox retry queue ──


class TestOutbox:
    def test_queue_and_retrieve(self, store):
        store.store_message(_make_msg())
        store.queue_outbox("msg-1", "mac2")
        pending = store.pending_outbox()
        assert len(pending) >= 1
        assert pending[0]["target_node"] == "mac2"

    def test_remove_outbox(self, store):
        store.store_message(_make_msg())
        store.queue_outbox("msg-1", "mac2")
        pending = store.pending_outbox()
        store.remove_outbox(pending[0]["id"])
        assert len(store.pending_outbox()) == 0

    def test_bump_retry_increases_attempts(self, store):
        store.store_message(_make_msg())
        store.queue_outbox("msg-1", "mac2")
        pending = store.pending_outbox()
        oid = pending[0]["id"]
        store.bump_outbox_retry(oid)
        row = store._conn().execute("SELECT attempts FROM outbox WHERE id = ?", (oid,)).fetchone()
        assert row[0] == 1


# ── WAL operations ──


class TestWAL:
    def test_write_and_unprocessed(self, store):
        wal_id = store.wal_write("msg-1", '{"type":"test"}')
        assert wal_id > 0
        unprocessed = store.wal_unprocessed()
        assert len(unprocessed) == 1
        assert unprocessed[0]["msg_id"] == "msg-1"

    def test_mark_processed(self, store):
        wal_id = store.wal_write("msg-1", '{}')
        store.wal_mark_processed(wal_id)
        assert len(store.wal_unprocessed()) == 0

    def test_prune_removes_old_processed(self, store):
        wal_id = store.wal_write("msg-1", '{}')
        store.wal_mark_processed(wal_id)
        # Backdate processed_at
        store._conn().execute(
            "UPDATE wal SET processed_at = ? WHERE id = ?",
            (time.time() - 100_000, wal_id),
        )
        store._conn().commit()
        store.wal_prune(max_age_hours=1)
        rows = store._conn().execute("SELECT * FROM wal").fetchall()
        assert len(rows) == 0

    def test_wal_replica_lifecycle(self, store):
        rid = store.wal_replica_write("msg-1", '{"data":1}', "mac1", "mac2")
        replicas = store.wal_replicas_for_node("mac2")
        assert len(replicas) == 1
        store.wal_replica_mark_delivered(rid)
        assert len(store.wal_replicas_for_node("mac2")) == 0


# ── Peers ──


class TestPeers:
    def test_upsert_and_get(self, store):
        store.upsert_peer("mac2", "192.168.1.2", 8855, "AA:BB:CC:DD:EE:02")
        peer = store.get_peer("mac2")
        assert peer is not None
        assert peer["ip"] == "192.168.1.2"
        assert peer["status"] == "online"

    def test_update_heartbeat(self, store):
        store.upsert_peer("mac2", "192.168.1.2")
        store.update_heartbeat("mac2", seq=5)
        peer = store.get_peer("mac2")
        assert peer["last_seq"] == 5

    def test_get_peers_list(self, store):
        store.upsert_peer("mac2", "192.168.1.2")
        store.upsert_peer("mac3", "192.168.1.3")
        peers = store.get_peers()
        assert len(peers) == 2


# ── ACK tracking ──


class TestACKTracking:
    def test_update_and_get_ack_status(self, store):
        store.store_message(_make_msg())
        store.update_ack_status("msg-1", "awaiting")
        ack = store.get_ack_status("msg-1")
        assert ack["ack_status"] == "awaiting"

    def test_bump_retry(self, store):
        store.store_message(_make_msg())
        store.update_ack_status("msg-1", "awaiting")
        store.bump_retry("msg-1")
        ack = store.get_ack_status("msg-1")
        assert ack["retry_count"] == 1

    def test_mark_ack_failed(self, store):
        store.store_message(_make_msg())
        store.mark_ack_failed("msg-1")
        msg = store.get_message("msg-1")
        assert msg["status"] == "failed"
        assert msg["ack_status"] == "failed"


# ── Stats persistence (no key leakage) ──


class TestStats:
    def test_save_and_load(self, store):
        stats = {"messages_sent": 10, "uptime": 3600}
        store.save_stats("mac1", stats)
        loaded = store.load_stats("mac1")
        assert loaded["messages_sent"] == 10

    def test_load_missing_returns_empty(self, store):
        assert store.load_stats("nope") == {}

    def test_corrupt_json_returns_empty(self, store):
        # Manually insert corrupt JSON
        store._conn().execute(
            """INSERT OR REPLACE INTO messages
               (id, type, from_node, subject, body, status, created_at, ttl_hours)
               VALUES (?, 'stats', ?, 'daemon_stats', ?, 'delivered', ?, 8760)""",
            ("stats-mac1", "mac1", "NOT{VALID}JSON[", time.time()),
        )
        store._conn().commit()
        assert store.load_stats("mac1") == {}


# ── Relay (chief P3 channel) ──


class TestRelay:
    def test_relay_queue_and_pending(self, store):
        msg = _make_msg()
        store.relay_queue(msg)
        pending = store.relay_pending("mac2")
        assert len(pending) == 1
        assert pending[0]["from_node"] == "mac1"

    def test_relay_dedup(self, store):
        msg = _make_msg()
        store.relay_queue(msg)
        store.relay_queue(msg)  # duplicate
        pending = store.relay_pending("mac2")
        assert len(pending) == 1

    def test_relay_ack(self, store):
        store.relay_queue(_make_msg())
        assert store.relay_ack("msg-1") is True
        assert len(store.relay_pending("mac2")) == 0

    def test_relay_ack_nonexistent(self, store):
        assert store.relay_ack("nope") is False
