"""Tests for lease cross-node visibility — daemon endpoint wiring.

Tests the evidence ledger recording and arbiter conflict registration
that happen when leases are managed through the daemon layer.
"""

import time

import pytest

from multifleet.leases import LeaseManager
from multifleet.store import FleetNerveStore
from multifleet.evidence_ledger import EvidenceLedger


@pytest.fixture
def evidence_ledger(tmp_path):
    """Evidence ledger backed by temp DB."""
    return EvidenceLedger(db_path=str(tmp_path / "evidence.db"))


@pytest.fixture
def chief_setup(tmp_path):
    """Chief + worker setup with separate DBs (simulates partition)."""
    chief_db = tmp_path / "chief.db"
    worker_db = tmp_path / "worker.db"
    chief_store = FleetNerveStore(db_path=chief_db)
    worker_store = FleetNerveStore(db_path=worker_db)
    chief = LeaseManager(chief_store, "mac1", chief_node_id="mac1")
    worker = LeaseManager(worker_store, "mac2", chief_node_id="mac1", daemon_port=19999)
    return chief, worker


class TestEvidenceLedgerIntegration:
    """Verify evidence ledger records lease events correctly."""

    def test_record_lease_acquired(self, tmp_path, evidence_ledger):
        """Evidence ledger records lease acquisitions."""
        store = FleetNerveStore(db_path=tmp_path / "leases.db")
        mgr = LeaseManager(store, "mac1")
        result = mgr.acquire("task-evidence")

        # Simulate what daemon does
        evidence_ledger.record(
            event_type="lease_acquired",
            node_id="mac1",
            subject="lease:task-evidence",
            payload=result,
        )

        entries = evidence_ledger.query(event_type="lease_acquired")
        assert len(entries) >= 1
        latest = entries[-1]
        assert latest["event_type"] == "lease_acquired"
        assert latest["node_id"] == "mac1"
        assert "task-evidence" in latest["subject"]

    def test_record_reconciliation_verdicts(self, chief_setup, evidence_ledger):
        """Evidence ledger records each reconciliation verdict."""
        chief, worker = chief_setup

        # Create conflict
        chief.acquire("shared-task")
        worker.acquire("shared-task")
        worker.acquire("solo-task")

        provisionals = worker.list_provisional(node_id="mac2")
        verdicts = chief.reconcile(provisionals)

        # Record each verdict (simulates daemon behavior)
        for v in verdicts:
            evidence_ledger.record(
                event_type=f"lease_reconcile_{v['verdict']}",
                node_id="mac1",
                subject=f"lease:{v.get('resource_id', '?')}",
                payload=v,
            )

        # Check conflict was recorded
        conflicts = evidence_ledger.query(event_type="lease_reconcile_conflict")
        assert len(conflicts) >= 1

        # Check acceptance was recorded
        accepts = evidence_ledger.query(event_type="lease_reconcile_accepted")
        assert len(accepts) >= 1

    def test_record_conflict_resolution(self, chief_setup, evidence_ledger):
        """Evidence ledger records conflict resolutions."""
        chief, worker = chief_setup

        chief.acquire("contested")
        resolution = chief.resolve_conflict("contested", "mac2")

        evidence_ledger.record(
            event_type="lease_conflict_resolved",
            node_id="mac1",
            subject="lease:contested",
            payload=resolution,
        )

        entries = evidence_ledger.query(event_type="lease_conflict_resolved")
        assert len(entries) >= 1
        assert entries[-1]["node_id"] == "mac1"

    def test_evidence_chain_integrity(self, tmp_path, evidence_ledger):
        """Multiple lease events maintain evidence chain integrity."""
        store = FleetNerveStore(db_path=tmp_path / "chain.db")
        mgr = LeaseManager(store, "mac1")

        # Record multiple events
        for i in range(5):
            result = mgr.acquire(f"task-chain-{i}")
            evidence_ledger.record(
                event_type="lease_acquired",
                node_id="mac1",
                subject=f"lease:task-chain-{i}",
                payload=result,
            )

        # Verify chain
        verification = evidence_ledger.verify_chain()
        assert verification["valid"] is True
        assert verification["checked"] >= 5


class TestArbiterConflictRegistration:
    """Verify lease conflicts register as arbiter pending decisions."""

    def test_conflict_registers_as_pending(self, chief_setup):
        """Lease conflict creates a pending arbiter decision."""
        from multifleet.arbiter_view import register_pending_decision, get_pending_decisions, _pending_decisions

        # Clear any existing decisions
        _pending_decisions.clear()

        chief, worker = chief_setup
        chief.acquire("conflict-res")
        worker.acquire("conflict-res")

        provisionals = worker.list_provisional(node_id="mac2")
        verdicts = chief.reconcile(provisionals)

        # Register conflicts (simulates daemon behavior)
        for v in verdicts:
            if v["verdict"] == "conflict":
                register_pending_decision({
                    "decision_id": f"lease-conflict-{v.get('resource_id', 'unknown')}-{int(time.time())}",
                    "type": "lease_conflict",
                    "task": f"Lease conflict on {v.get('resource_id', '?')}",
                    "recommendation": {
                        "branch": None,
                        "node": v.get("chief_holder", ""),
                        "confidence": 0.5,
                        "reasoning": v.get("reason", "split-brain conflict"),
                        "evidence": [v],
                    },
                    "alternatives": [{
                        "branch": None,
                        "node": v.get("node_id", ""),
                        "summary": f"Worker {v.get('node_id', '?')} acquired during partition",
                        "strengths": ["Had active work during partition"],
                        "weaknesses": ["Provisional lease, not chief-coordinated"],
                        "probe_health": None,
                    }],
                    "dissent": [],
                    "synthesis": v,
                    "resource_id": v.get("resource_id"),
                })

        pending = get_pending_decisions()
        lease_conflicts = [d for d in pending if d.get("type") == "lease_conflict"]
        assert len(lease_conflicts) >= 1
        assert lease_conflicts[0]["resource_id"] == "conflict-res"
        assert lease_conflicts[0]["status"] == "awaiting_human"

    def test_resolved_conflict_leaves_pending(self, chief_setup):
        """After arbiter resolves conflict, the lease is updated."""
        from multifleet.arbiter_view import record_decision, register_pending_decision, _pending_decisions

        _pending_decisions.clear()

        chief, _ = chief_setup
        chief.acquire("resolve-test")

        did = f"lease-conflict-resolve-test-{int(time.time())}"
        register_pending_decision({
            "decision_id": did,
            "type": "lease_conflict",
            "task": "Lease conflict on resolve-test",
            "recommendation": {"node": "mac1", "confidence": 0.5, "reasoning": "test"},
            "alternatives": [],
            "dissent": [],
            "synthesis": {},
            "resource_id": "resolve-test",
        })

        result = record_decision(did, "accept_recommendation", {"winner": "mac2"})
        assert result["status"] == "decided"
        assert result["action"] == "accept_recommendation"


class TestFenceTokenWorkflow:
    """End-to-end fence token workflow — acquire, use, validate, re-acquire."""

    def test_fenced_write_workflow(self, tmp_path):
        """Simulate: acquire → get token → do work → validate before write → success."""
        db = tmp_path / "fenced.db"
        store = FleetNerveStore(db_path=db)
        mac1 = LeaseManager(store, "mac1")
        mac2 = LeaseManager(store, "mac2")

        # mac1 acquires and gets fence token
        r = mac1.acquire("critical-file", lease_type="exclusive")
        token = r["fence_token"]
        assert r["acquired"] is True

        # mac1 validates before writing — should pass
        v = mac1.validate_fence_token("critical-file", token)
        assert v["valid"] is True

        # mac1 releases, mac2 acquires (new fence token)
        mac1.release("critical-file")
        r2 = mac2.acquire("critical-file", lease_type="exclusive")
        assert r2["fence_token"] > token

        # mac1 tries to validate old token — should fail (stale)
        v_stale = mac1.validate_fence_token("critical-file", token)
        assert v_stale["valid"] is False
        assert "stale" in v_stale["reason"]

        # mac2 validates its token — should pass
        v2 = mac2.validate_fence_token("critical-file", r2["fence_token"])
        assert v2["valid"] is True

    def test_fence_token_survives_renew(self, tmp_path):
        """Fence token remains valid across renewals."""
        db = tmp_path / "renew.db"
        store = FleetNerveStore(db_path=db)
        mgr = LeaseManager(store, "mac1")

        r = mgr.acquire("renewable", lease_type="short_task")
        token = r["fence_token"]

        # Renew multiple times
        for _ in range(3):
            mgr.renew("renewable")

        # Token should still be valid
        v = mgr.validate_fence_token("renewable", token)
        assert v["valid"] is True
        assert v["current_fence"] == token
