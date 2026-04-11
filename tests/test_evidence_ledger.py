"""Tests for multifleet.evidence_ledger — tamper-evident append-only log."""

import sqlite3
import threading
import time

import pytest

from multifleet.evidence_ledger import EvidenceLedger


@pytest.fixture
def ledger(tmp_path):
    """Create an EvidenceLedger backed by a temporary SQLite database."""
    db = tmp_path / "test_evidence.db"
    return EvidenceLedger(db_path=str(db))


@pytest.fixture
def ledger_factory(tmp_path):
    """Factory for creating multiple ledgers on the same DB (concurrent test)."""
    db = str(tmp_path / "test_evidence_shared.db")

    def _make():
        return EvidenceLedger(db_path=db)
    return _make


class TestRecord:
    def test_record_creates_entry(self, ledger):
        entry = ledger.record("merge_decision", "mac1", "merge feature-x",
                              {"branch": "feature-x", "approved": True})
        assert entry["entry_id"]
        assert entry["event_type"] == "merge_decision"
        assert entry["node_id"] == "mac1"
        assert entry["subject"] == "merge feature-x"
        assert entry["payload"]["branch"] == "feature-x"
        assert entry["entry_hash"]
        assert entry["prev_hash"] == "genesis"
        assert entry["seq"] == 1

    def test_genesis_entry_has_genesis_prev(self, ledger):
        entry = ledger.record("lease_granted", "mac2", "lease test",
                              {"resource": "branch/main"})
        assert entry["prev_hash"] == "genesis"

    def test_second_entry_links_to_first(self, ledger):
        e1 = ledger.record("lease_granted", "mac1", "first", {"r": 1})
        e2 = ledger.record("lease_granted", "mac2", "second", {"r": 2})
        assert e2["prev_hash"] == e1["entry_hash"]
        assert e2["prev_hash"] != "genesis"


class TestHashChain:
    def test_hash_chain_links_entries(self, ledger):
        entries = []
        for i in range(5):
            e = ledger.record("gains_gate_result", f"mac{i % 3 + 1}",
                              f"gate run {i}", {"pass": True, "run": i})
            entries.append(e)

        # Each entry's prev_hash should be the previous entry's hash
        assert entries[0]["prev_hash"] == "genesis"
        for i in range(1, len(entries)):
            assert entries[i]["prev_hash"] == entries[i - 1]["entry_hash"]

    def test_verify_chain_valid(self, ledger):
        for i in range(3):
            ledger.record("channel_repair", "mac1", f"repair {i}",
                          {"channel": f"P{i + 1}", "success": True})

        result = ledger.verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 3
        assert result["broken_at"] is None

    def test_verify_chain_detects_tampering(self, ledger):
        for i in range(3):
            ledger.record("consensus_reached", "mac1", f"consensus {i}",
                          {"topic": f"topic-{i}"})

        # Tamper with the second entry's payload directly in the DB
        conn = sqlite3.connect(ledger._db_path)
        conn.execute(
            "UPDATE evidence SET payload = ? WHERE seq = 2",
            ('{"topic": "TAMPERED"}',),
        )
        conn.commit()
        conn.close()

        result = ledger.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 2
        assert "mismatch" in result["detail"]

    def test_verify_empty_ledger(self, ledger):
        result = ledger.verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 0

    def test_verify_chain_with_limit(self, ledger):
        for i in range(10):
            ledger.record("gains_gate_result", "mac1", f"run {i}", {"i": i})

        result = ledger.verify_chain(limit=5)
        assert result["valid"] is True
        assert result["checked"] == 5


class TestQuery:
    def test_query_filters_by_event_type(self, ledger):
        ledger.record("lease_granted", "mac1", "lease a", {"r": "a"})
        ledger.record("merge_decision", "mac1", "merge b", {"r": "b"})
        ledger.record("lease_granted", "mac2", "lease c", {"r": "c"})

        results = ledger.query(event_type="lease_granted")
        assert len(results) == 2
        assert all(r["event_type"] == "lease_granted" for r in results)

    def test_query_filters_by_node_id(self, ledger):
        ledger.record("lease_granted", "mac1", "a", {"r": 1})
        ledger.record("lease_granted", "mac2", "b", {"r": 2})
        ledger.record("merge_decision", "mac1", "c", {"r": 3})

        results = ledger.query(node_id="mac1")
        assert len(results) == 2
        assert all(r["node_id"] == "mac1" for r in results)

    def test_query_filters_by_since(self, ledger):
        ledger.record("lease_granted", "mac1", "old", {"r": 1})
        cutoff = time.time()
        ledger.record("lease_granted", "mac1", "new", {"r": 2})

        results = ledger.query(since=cutoff)
        assert len(results) == 1
        assert results[0]["subject"] == "new"

    def test_query_combined_filters(self, ledger):
        ledger.record("lease_granted", "mac1", "a", {})
        ledger.record("lease_granted", "mac2", "b", {})
        ledger.record("merge_decision", "mac1", "c", {})

        results = ledger.query(event_type="lease_granted", node_id="mac1")
        assert len(results) == 1
        assert results[0]["subject"] == "a"

    def test_query_respects_limit(self, ledger):
        for i in range(10):
            ledger.record("gains_gate_result", "mac1", f"run {i}", {"i": i})

        results = ledger.query(limit=3)
        assert len(results) == 3


class TestLatest:
    def test_latest_returns_recent(self, ledger):
        for i in range(5):
            ledger.record("gains_gate_result", "mac1", f"run {i}", {"i": i})

        results = ledger.latest(count=3)
        assert len(results) == 3
        # Most recent first
        assert results[0]["subject"] == "run 4"
        assert results[1]["subject"] == "run 3"
        assert results[2]["subject"] == "run 2"

    def test_latest_with_fewer_entries(self, ledger):
        ledger.record("lease_granted", "mac1", "only one", {})
        results = ledger.latest(count=10)
        assert len(results) == 1


class TestStats:
    def test_stats(self, ledger):
        ledger.record("lease_granted", "mac1", "a", {})
        ledger.record("lease_granted", "mac2", "b", {})
        ledger.record("merge_decision", "mac1", "c", {})

        s = ledger.stats
        assert s["total"] == 3
        assert s["by_type"]["lease_granted"] == 2
        assert s["by_type"]["merge_decision"] == 1
        assert s["last_entry_at"] is not None
        assert s["last_hash_prefix"] is not None
        assert s["chain_valid"] is True

    def test_stats_empty(self, ledger):
        s = ledger.stats
        assert s["total"] == 0
        assert s["chain_valid"] is True


class TestPayloadParsing:
    def test_query_returns_parsed_payload(self, ledger):
        """Query results should have payload as dict, not raw JSON string."""
        ledger.record("merge_decision", "mac1", "test",
                      {"branch": "feature-x", "count": 42})
        results = ledger.query(event_type="merge_decision")
        assert isinstance(results[0]["payload"], dict)
        assert results[0]["payload"]["count"] == 42

    def test_latest_returns_parsed_payload(self, ledger):
        ledger.record("lease_granted", "mac1", "test", {"resource": "main"})
        results = ledger.latest(count=1)
        assert isinstance(results[0]["payload"], dict)
        assert results[0]["payload"]["resource"] == "main"


class TestPrevHashTampering:
    def test_verify_detects_prev_hash_tampering(self, ledger):
        """Modifying prev_hash without changing entry_hash should be caught."""
        ledger.record("lease_granted", "mac1", "a", {"r": 1})
        ledger.record("lease_granted", "mac1", "b", {"r": 2})

        conn = sqlite3.connect(ledger._db_path)
        conn.execute("UPDATE evidence SET prev_hash = 'bogus' WHERE seq = 2")
        conn.commit()
        conn.close()

        result = ledger.verify_chain()
        assert result["valid"] is False
        assert result["broken_at"] == 2


class TestConcurrency:
    def test_concurrent_writes_maintain_chain(self, ledger_factory):
        """Multiple threads writing simultaneously should produce a valid chain."""
        errors = []

        def writer(thread_id):
            try:
                lg = ledger_factory()
                for i in range(5):
                    lg.record("gains_gate_result", f"mac{thread_id}",
                              f"thread-{thread_id}-run-{i}",
                              {"thread": thread_id, "run": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Writer errors: {errors}"

        # Verify the chain is intact
        lg = ledger_factory()
        result = lg.verify_chain()
        assert result["valid"] is True
        assert result["checked"] == 20  # 4 threads x 5 entries
