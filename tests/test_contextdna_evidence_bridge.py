"""Tests for multifleet.contextdna_evidence_bridge — bidirectional evidence sync."""

import json
import time

import pytest

from multifleet.evidence_ledger import EvidenceLedger
from multifleet.contextdna_evidence_bridge import ContextDNAEvidenceBridge


@pytest.fixture
def ledger(tmp_path):
    """Create an EvidenceLedger backed by a temporary SQLite database."""
    db = tmp_path / "test_evidence.db"
    return EvidenceLedger(db_path=str(db))


@pytest.fixture
def state_path(tmp_path):
    """Temporary sync state path."""
    return str(tmp_path / "sync_state.json")


@pytest.fixture
def bridge(ledger, state_path):
    """Create a ContextDNAEvidenceBridge with fake deps."""
    return ContextDNAEvidenceBridge(
        ledger=ledger,
        product_bridge=None,  # No product bridge for unit tests
        state_path=state_path,
    )


class _FakeProductBridge:
    """Fake ProductBridge for testing pull direction."""

    def __init__(self, memory_results=None):
        self._memory_results = memory_results or {}
        self.fleet_to_memory_calls = []

    def fleet_to_memory(self, evidence):
        self.fleet_to_memory_calls.append(evidence)
        return {"stored": True, "entry_id": "fake-id", "backend": "test"}

    def memory_to_fleet(self, query):
        results = self._memory_results.get(query, [])
        return {"results": results, "count": len(results), "source": "test"}


class TestPushToContextDNA:
    def test_push_with_no_ledger(self, state_path):
        bridge = ContextDNAEvidenceBridge(
            ledger=None, state_path=state_path,
        )
        result = bridge.push_to_contextdna()
        assert result["pushed"] == 0
        assert result["reason"] == "no ledger configured"

    def test_push_empty_ledger(self, bridge):
        result = bridge.push_to_contextdna()
        assert result["pushed"] == 0
        assert result["errors"] == 0
        assert result["skipped"] == 0

    def test_push_with_product_bridge(self, ledger, state_path):
        fake_pb = _FakeProductBridge()
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        # Nullify memory_bridge so ProductBridge is used
        bridge._memory_bridge = None

        # Record some evidence
        ledger.record("race_completed", "mac1", "race done",
                      {"race_id": "abc", "winner": "mac2"})
        ledger.record("merge_decision", "mac2", "merge ok",
                      {"branch": "feature-x"})

        result = bridge.push_to_contextdna()
        assert result["pushed"] == 2
        assert result["errors"] == 0
        assert len(fake_pb.fleet_to_memory_calls) == 2

    def test_push_dedup_same_entry_twice(self, ledger, state_path):
        fake_pb = _FakeProductBridge()
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        bridge._memory_bridge = None

        ledger.record("race_completed", "mac1", "race done",
                      {"race_id": "abc"})

        # First push
        r1 = bridge.push_to_contextdna()
        assert r1["pushed"] == 1

        # Second push (same entries) should skip
        r2 = bridge.push_to_contextdna()
        assert r2["pushed"] == 0
        assert r2["skipped"] == 1

    def test_push_specific_entry_ids(self, ledger, state_path):
        fake_pb = _FakeProductBridge()
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        bridge._memory_bridge = None

        e1 = ledger.record("race_completed", "mac1", "race 1", {"r": 1})
        e2 = ledger.record("merge_decision", "mac1", "merge 1", {"r": 2})

        # Push only e1
        result = bridge.push_to_contextdna(entry_ids=[e1["entry_id"]])
        assert result["pushed"] == 1
        assert len(fake_pb.fleet_to_memory_calls) == 1


class TestPullFromContextDNA:
    def test_pull_with_no_ledger(self, state_path):
        bridge = ContextDNAEvidenceBridge(
            ledger=None, state_path=state_path,
        )
        result = bridge.pull_from_contextdna()
        assert result["pulled"] == 0

    def test_pull_with_no_product_bridge(self, ledger, state_path):
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=None, state_path=state_path,
        )
        # Force no product bridge (constructor may auto-discover)
        bridge._product_bridge = None
        result = bridge.pull_from_contextdna()
        assert result["pulled"] == 0
        assert result["reason"] == "no product bridge configured"

    def test_pull_records_memory_learnings(self, ledger, state_path):
        memory_results = {
            "fleet": [
                {"subject": "fleet race pattern", "event_type": "race_completed",
                 "timestamp": time.time()},
            ],
            "race": [],
            "synthesis": [
                {"subject": "synthesis lesson", "event_type": "merge_decision",
                 "timestamp": time.time()},
            ],
            "evidence": [],
        }
        fake_pb = _FakeProductBridge(memory_results=memory_results)
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )

        result = bridge.pull_from_contextdna()
        assert result["pulled"] == 2
        assert result["errors"] == 0

        # Verify entries were recorded in ledger
        entries = ledger.query(event_type="memory_learning")
        assert len(entries) == 2
        for e in entries:
            assert "[contextdna]" in e["subject"]
            assert e["payload"]["source"] == "contextdna_pull"

    def test_pull_dedup_same_content(self, ledger, state_path):
        learning = {"subject": "same thing", "event_type": "test",
                    "timestamp": time.time()}
        memory_results = {
            "fleet": [learning],
            "race": [learning],  # Same content via different query
            "synthesis": [],
            "evidence": [],
        }
        fake_pb = _FakeProductBridge(memory_results=memory_results)
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )

        result = bridge.pull_from_contextdna()
        # Should only record once despite appearing in two query results
        assert result["pulled"] == 1
        assert result["skipped"] >= 1


class TestSyncCycle:
    def test_sync_cycle_round_trip(self, ledger, state_path):
        memory_results = {
            "fleet": [{"subject": "learning", "event_type": "test",
                       "timestamp": time.time()}],
            "race": [],
            "synthesis": [],
            "evidence": [],
        }
        fake_pb = _FakeProductBridge(memory_results=memory_results)
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        bridge._memory_bridge = None

        # Add an entry to push
        ledger.record("race_completed", "mac1", "done", {"r": 1})

        result = bridge.sync_cycle()
        assert result["pushed"]["pushed"] == 1
        assert result["pulled"]["pulled"] == 1
        assert result["elapsed_s"] >= 0
        assert result["node_id"]

    def test_sync_cycle_empty(self, bridge):
        result = bridge.sync_cycle()
        assert result["pushed"]["pushed"] == 0


class TestEventHooks:
    def test_on_race_completed(self, ledger, state_path):
        fake_pb = _FakeProductBridge()
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        bridge._memory_bridge = None

        # Record a race_completed entry first (simulates fleet_race)
        ledger.record("race_completed", "mac1", "race_completed:test123",
                      {"race_id": "test123", "winner": "mac2"})

        # Hook should find and push it
        bridge.on_race_completed("test123", {"winner": "mac2"})
        assert len(fake_pb.fleet_to_memory_calls) >= 1

    def test_on_probe_finding(self, ledger, state_path):
        fake_pb = _FakeProductBridge()
        bridge = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        bridge._memory_bridge = None

        finding = {
            "title": "Drift detected in config",
            "severity": "high",
            "probe": "config_drift",
        }
        bridge.on_probe_finding(finding)

        # Should record in ledger AND push to contextdna
        entries = ledger.query(event_type="scan_finding")
        assert len(entries) == 1
        assert "probe" in entries[0]["subject"]
        assert len(fake_pb.fleet_to_memory_calls) >= 1

    def test_on_race_completed_no_ledger(self, state_path):
        bridge = ContextDNAEvidenceBridge(
            ledger=None, state_path=state_path,
        )
        # Should not raise
        bridge.on_race_completed("test", {})

    def test_on_probe_finding_no_ledger(self, state_path):
        bridge = ContextDNAEvidenceBridge(
            ledger=None, state_path=state_path,
        )
        # Should not raise
        bridge.on_probe_finding({"title": "test"})


class TestStateManagement:
    def test_state_persists_across_instances(self, ledger, state_path):
        fake_pb = _FakeProductBridge()

        # First instance pushes
        b1 = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        b1._memory_bridge = None
        ledger.record("race_completed", "mac1", "test", {"r": 1})
        b1.push_to_contextdna()

        # Second instance should see the watermark
        b2 = ContextDNAEvidenceBridge(
            ledger=ledger, product_bridge=fake_pb, state_path=state_path,
        )
        assert b2._state.get("last_push_ts", 0) > 0
        assert len(b2._state.get("pushed_hashes", [])) > 0

    def test_stats_property(self, bridge):
        s = bridge.stats
        assert "pushed" in s
        assert "pulled" in s
        assert "memory_bridge_available" in s
        assert "product_bridge_available" in s
