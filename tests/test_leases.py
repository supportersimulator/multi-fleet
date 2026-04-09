"""Tests for multifleet.leases — lease-based task ownership."""

import time

import pytest

from multifleet.leases import LEASE_TTLS, LeaseManager
from multifleet.store import FleetNerveStore


@pytest.fixture
def store(tmp_path):
    """Create a FleetNerveStore backed by a temporary SQLite database."""
    db = tmp_path / "test_leases.db"
    return FleetNerveStore(db_path=db)


@pytest.fixture
def mgr(store):
    """LeaseManager for mac1."""
    return LeaseManager(store, "mac1")


@pytest.fixture
def mgr2(store):
    """LeaseManager for mac2 (same DB, different node)."""
    return LeaseManager(store, "mac2")


@pytest.fixture
def mgr_chief(store):
    """LeaseManager for mac1 acting as chief."""
    return LeaseManager(store, "mac1", chief_node_id="mac1")


@pytest.fixture
def mgr_non_chief(store):
    """LeaseManager for mac2 with mac1 as chief (chief unreachable in tests)."""
    return LeaseManager(store, "mac2", chief_node_id="mac1", daemon_port=19999)


class TestAcquire:
    def test_acquire_available(self, mgr):
        result = mgr.acquire("branch/feature-x")
        assert result["acquired"] is True
        assert result["holder"] == "mac1"
        assert result["expires_at"] > time.time()
        assert "fence_token" in result
        assert result["fence_token"] >= 1

    def test_acquire_already_held(self, mgr, mgr2):
        mgr.acquire("branch/feature-x")
        result = mgr2.acquire("branch/feature-x")
        assert result["acquired"] is False
        assert result["holder"] == "mac1"
        assert "reason" in result

    def test_acquire_by_same_node_renews(self, mgr):
        r1 = mgr.acquire("branch/feature-x")
        r2 = mgr.acquire("branch/feature-x")
        assert r2["acquired"] is True
        assert r2["expires_at"] >= r1["expires_at"]

    def test_acquire_expired_reclaim(self, mgr, mgr2):
        """Expired lease can be reclaimed by another node."""
        # Acquire with heartbeat TTL (30s) then fake expiry
        mgr.acquire("branch/feature-x", lease_type="heartbeat")
        # Manually expire it by backdating
        conn = mgr._store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id = ?",
            (time.time() - 100, "branch/feature-x"),
        )
        conn.commit()
        # mac2 should be able to reclaim
        result = mgr2.acquire("branch/feature-x")
        assert result["acquired"] is True
        assert result["holder"] == "mac2"

    def test_acquire_unknown_type(self, mgr):
        result = mgr.acquire("x", lease_type="invalid")
        assert result["acquired"] is False
        assert "unknown lease_type" in result["reason"]


class TestFenceTokens:
    def test_fence_token_increments(self, mgr, mgr2):
        """Each new acquire of a resource gets a higher fence token."""
        r1 = mgr.acquire("res-fence")
        assert r1["fence_token"] == 1

        # Release and re-acquire by another node
        mgr.release("res-fence")
        r2 = mgr2.acquire("res-fence")
        assert r2["fence_token"] == 2

    def test_fence_token_monotonic_after_expiry(self, mgr, mgr2):
        """Fence token increments even when previous lease expired."""
        mgr.acquire("res-fence2", lease_type="heartbeat")
        conn = mgr._store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id = ?",
            (time.time() - 100, "res-fence2"),
        )
        conn.commit()
        r2 = mgr2.acquire("res-fence2")
        assert r2["acquired"] is True
        assert r2["fence_token"] >= 2  # At least 2 (first was 1)

    def test_fence_token_in_renew(self, mgr):
        """Renew response includes fence token."""
        r1 = mgr.acquire("res-fence3")
        r2 = mgr.renew("res-fence3")
        assert r2["renewed"] is True
        assert r2["fence_token"] == r1["fence_token"]

    def test_fence_token_different_resources(self, mgr):
        """Each resource has its own fence token sequence."""
        r1 = mgr.acquire("res-a")
        r2 = mgr.acquire("res-b")
        assert r1["fence_token"] == 1
        assert r2["fence_token"] == 1


class TestAtomicAcquire:
    def test_acquire_is_atomic(self, store):
        """Verify BEGIN IMMEDIATE is used — concurrent managers on same DB don't corrupt."""
        m1 = LeaseManager(store, "mac1")
        m2 = LeaseManager(store, "mac2")

        # Both try to acquire the same resource — one must win, one must lose
        r1 = m1.acquire("contested-resource")
        r2 = m2.acquire("contested-resource")

        assert r1["acquired"] is True
        assert r2["acquired"] is False
        assert r2["holder"] == "mac1"

    def test_acquire_rollback_on_denial(self, mgr, mgr2):
        """Denied acquire should not leave partial state."""
        mgr.acquire("res-atomic")
        mgr2.acquire("res-atomic")  # denied
        # Original lease should be intact
        info = mgr.query("res-atomic")
        assert info["node_id"] == "mac1"
        assert info["active"] is True


class TestChiefCoordination:
    def test_chief_node_acquires_locally(self, mgr_chief):
        """Chief node processes leases locally without HTTP."""
        result = mgr_chief.acquire("res-chief")
        assert result["acquired"] is True
        assert "_local_only" not in result

    def test_non_chief_falls_back_on_unreachable(self, mgr_non_chief):
        """Non-chief with unreachable chief falls back to local with _local_only flag."""
        result = mgr_non_chief.acquire("res-nonchief")
        assert result["acquired"] is True
        assert result.get("_local_only") is True

    def test_is_chief_property(self, mgr_chief, mgr_non_chief):
        assert mgr_chief.is_chief is True
        assert mgr_non_chief.is_chief is False

    def test_no_chief_configured_acts_as_chief(self, mgr):
        """When no chief is configured, node acts as chief (local only, no flag)."""
        result = mgr.acquire("res-nochief")
        assert result["acquired"] is True
        assert "_local_only" not in result
        assert mgr.is_chief is True


class TestRenew:
    def test_renew_own_lease(self, mgr):
        mgr.acquire("res-1")
        result = mgr.renew("res-1")
        assert result["renewed"] is True
        assert result["expires_at"] > time.time()

    def test_renew_others_lease(self, mgr, mgr2):
        mgr.acquire("res-1")
        result = mgr2.renew("res-1")
        assert result["renewed"] is False
        assert "held by mac1" in result["reason"]

    def test_renew_nonexistent(self, mgr):
        result = mgr.renew("nope")
        assert result["renewed"] is False
        assert "no lease found" in result["reason"]

    def test_renew_expired(self, mgr):
        mgr.acquire("res-1", lease_type="heartbeat")
        conn = mgr._store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id = ?",
            (time.time() - 100, "res-1"),
        )
        conn.commit()
        result = mgr.renew("res-1")
        assert result["renewed"] is False
        assert "expired" in result["reason"]


class TestRelease:
    def test_release(self, mgr):
        mgr.acquire("res-1")
        result = mgr.release("res-1")
        assert result["released"] is True

    def test_release_others_lease(self, mgr, mgr2):
        mgr.acquire("res-1")
        result = mgr2.release("res-1")
        assert result["released"] is False

    def test_release_nonexistent(self, mgr):
        result = mgr.release("nope")
        assert result["released"] is False

    def test_released_lease_reclaimable(self, mgr, mgr2):
        mgr.acquire("res-1")
        mgr.release("res-1")
        result = mgr2.acquire("res-1")
        assert result["acquired"] is True
        assert result["holder"] == "mac2"


class TestCleanup:
    def test_cleanup_expired(self, mgr):
        mgr.acquire("res-1", lease_type="heartbeat")
        mgr.acquire("res-2", lease_type="heartbeat")
        mgr.acquire("res-3", lease_type="long_task")
        # Expire res-1 and res-2
        conn = mgr._store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id IN (?, ?)",
            (time.time() - 100, "res-1", "res-2"),
        )
        conn.commit()
        cleaned = mgr.cleanup_expired()
        assert cleaned == 2
        # res-3 should still exist
        assert mgr.query("res-3") is not None
        assert mgr.query("res-1") is None

    def test_cleanup_released(self, mgr):
        mgr.acquire("res-1")
        mgr.release("res-1")
        cleaned = mgr.cleanup_expired()
        assert cleaned == 1


class TestListActive:
    def test_list_active(self, mgr, mgr2):
        mgr.acquire("res-a")
        mgr2.acquire("res-b")
        all_leases = mgr.list_active()
        assert len(all_leases) == 2

    def test_list_active_filtered(self, mgr, mgr2):
        mgr.acquire("res-a")
        mgr2.acquire("res-b")
        mac1_leases = mgr.list_active(node_id="mac1")
        assert len(mac1_leases) == 1
        assert mac1_leases[0]["node_id"] == "mac1"

    def test_list_excludes_expired(self, mgr):
        mgr.acquire("res-1", lease_type="heartbeat")
        conn = mgr._store._conn()
        conn.execute(
            "UPDATE leases SET acquired_at = ? WHERE resource_id = ?",
            (time.time() - 100, "res-1"),
        )
        conn.commit()
        assert len(mgr.list_active()) == 0


class TestLeaseTypes:
    def test_each_type_has_correct_ttl(self, mgr):
        for lease_type, expected_ttl in LEASE_TTLS.items():
            result = mgr.acquire(f"res-{lease_type}", lease_type=lease_type)
            assert result["acquired"] is True
            # expires_at should be approximately now + TTL
            expected = time.time() + expected_ttl
            assert abs(result["expires_at"] - expected) < 2  # 2s tolerance


class TestQuery:
    def test_query_existing(self, mgr):
        mgr.acquire("res-1", lease_type="long_task")
        info = mgr.query("res-1")
        assert info is not None
        assert info["node_id"] == "mac1"
        assert info["lease_type"] == "long_task"
        assert info["active"] is True
        assert "fence_token" in info

    def test_query_nonexistent(self, mgr):
        assert mgr.query("nope") is None


class TestConcurrentLeases:
    def test_multiple_tasks_same_node(self, mgr):
        """A single node can hold leases on multiple different resources."""
        r1 = mgr.acquire("task-a")
        r2 = mgr.acquire("task-b")
        r3 = mgr.acquire("task-c")
        assert r1["acquired"] is True
        assert r2["acquired"] is True
        assert r3["acquired"] is True
        active = mgr.list_active(node_id="mac1")
        assert len(active) == 3

    def test_multiple_nodes_different_tasks(self, mgr, mgr2):
        """Different nodes can hold leases on different resources simultaneously."""
        r1 = mgr.acquire("task-a")
        r2 = mgr2.acquire("task-b")
        assert r1["acquired"] is True
        assert r2["acquired"] is True
        all_active = mgr.list_active()
        assert len(all_active) == 2


class TestPersistence:
    def test_lease_survives_reopen(self, tmp_path):
        """Leases persist across LeaseManager instances (SQLite durability)."""
        db = tmp_path / "persist_test.db"
        store1 = FleetNerveStore(db_path=db)
        m1 = LeaseManager(store1, "mac1")
        m1.acquire("persist-res", lease_type="long_task")

        # Create new store + manager on same DB file (simulates restart)
        store2 = FleetNerveStore(db_path=db)
        m2 = LeaseManager(store2, "mac1")
        info = m2.query("persist-res")
        assert info is not None
        assert info["node_id"] == "mac1"
        assert info["active"] is True

    def test_different_node_sees_persisted_lease(self, tmp_path):
        """A different node reopening the DB sees existing leases and cannot acquire them."""
        db = tmp_path / "persist_test2.db"
        store1 = FleetNerveStore(db_path=db)
        m1 = LeaseManager(store1, "mac1")
        m1.acquire("persist-res2", lease_type="exclusive")

        # New store instance on same DB — mac2 should see mac1's lease
        store2 = FleetNerveStore(db_path=db)
        m2 = LeaseManager(store2, "mac2")
        result = m2.acquire("persist-res2")
        assert result["acquired"] is False
        assert result["holder"] == "mac1"


class TestProvisionalLeases:
    """Tests for provisional lease acquisition during partition."""

    def test_provisional_flag_on_chief_unreachable(self, mgr_non_chief):
        """Non-chief with unreachable chief creates provisional lease."""
        result = mgr_non_chief.acquire("res-prov")
        assert result["acquired"] is True
        assert result.get("provisional") is True
        assert result.get("partition_id") is not None
        assert result["partition_id"].startswith("part-")

    def test_provisional_stored_in_db(self, mgr_non_chief):
        """Provisional flag persists in SQLite."""
        mgr_non_chief.acquire("res-prov-db")
        info = mgr_non_chief.query("res-prov-db")
        assert info["provisional"] is True
        assert info["partition_id"] is not None

    def test_chief_lease_not_provisional(self, mgr_chief):
        """Chief-acquired leases are never provisional."""
        result = mgr_chief.acquire("res-auth")
        assert result["acquired"] is True
        assert "provisional" not in result  # not set on authoritative
        info = mgr_chief.query("res-auth")
        assert info["provisional"] is False

    def test_list_provisional(self, store):
        """list_provisional returns only provisional leases."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        worker = LeaseManager(store, "mac2", chief_node_id="mac1", daemon_port=19999)
        chief.acquire("res-auth-1")
        worker.acquire("res-prov-1")
        worker.acquire("res-prov-2")
        provisionals = worker.list_provisional()
        assert len(provisionals) == 2
        assert all(p["provisional"] for p in provisionals)

    def test_list_provisional_filtered_by_node(self, store):
        """list_provisional can filter by node_id."""
        worker1 = LeaseManager(store, "mac2", chief_node_id="mac1", daemon_port=19999)
        worker2 = LeaseManager(store, "mac3", chief_node_id="mac1", daemon_port=19999)
        worker1.acquire("res-w1")
        worker2.acquire("res-w2")
        assert len(worker1.list_provisional(node_id="mac2")) == 1
        assert len(worker1.list_provisional(node_id="mac3")) == 1


class TestReconciliation:
    """Tests for partition-heal reconciliation protocol."""

    def test_reconcile_accept_no_chief_lease(self, store):
        """Provisional lease accepted when chief has no lease for that resource."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        verdicts = chief.reconcile([{
            "resource_id": "task-orphan",
            "node_id": "mac2",
            "fence_token": 1,
            "lease_type": "short_task",
            "partition_id": "part-abc",
        }])
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "accepted"
        # Promoted lease should now exist on chief
        info = chief.query("task-orphan")
        assert info is not None
        assert info["node_id"] == "mac2"
        assert info["provisional"] is False
        assert info["reconciled_at"] is not None

    def test_reconcile_reject_chief_higher_fence(self, store):
        """Chief lease with higher fence token wins — provisional rejected."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        # Chief acquires first (fence=1), then again (fence=2)
        chief.acquire("task-contested")
        chief.release("task-contested")
        chief.acquire("task-contested")  # fence=2
        verdicts = chief.reconcile([{
            "resource_id": "task-contested",
            "node_id": "mac2",
            "fence_token": 1,
            "partition_id": "part-xyz",
        }])
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "rejected"
        assert verdicts[0]["chief_fence"] == 2
        assert verdicts[0]["provisional_fence"] == 1

    def test_reconcile_conflict_split_brain(self, store):
        """Same fence token, different holders — true split-brain conflict."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        chief.acquire("task-split")  # fence=1, held by mac1
        verdicts = chief.reconcile([{
            "resource_id": "task-split",
            "node_id": "mac2",
            "fence_token": 1,
            "partition_id": "part-split",
        }])
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "conflict"
        assert "split-brain" in verdicts[0]["reason"]

    def test_reconcile_must_be_chief(self, store):
        """Non-chief node cannot reconcile."""
        worker = LeaseManager(store, "mac2", chief_node_id="mac1", daemon_port=19999)
        verdicts = worker.reconcile([{
            "resource_id": "task-x",
            "node_id": "mac2",
            "fence_token": 1,
        }])
        assert verdicts[0]["verdict"] == "error"

    def test_reconcile_multiple_leases(self, store):
        """Reconcile handles a batch of provisional leases."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        chief.acquire("task-a")  # will conflict
        # task-b has no chief lease — will be accepted
        verdicts = chief.reconcile([
            {"resource_id": "task-a", "node_id": "mac2", "fence_token": 1, "partition_id": "p1"},
            {"resource_id": "task-b", "node_id": "mac2", "fence_token": 1, "partition_id": "p1",
             "lease_type": "short_task"},
        ])
        assert len(verdicts) == 2
        verdict_map = {v["resource_id"]: v["verdict"] for v in verdicts}
        assert verdict_map["task-a"] == "conflict"  # split-brain
        assert verdict_map["task-b"] == "accepted"


class TestConflictResolution:
    """Tests for arbiter-driven conflict resolution."""

    def test_resolve_conflict(self, store):
        """Resolve a conflict by choosing the winning node."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        chief.acquire("task-conflict")  # mac1 holds, fence=1
        result = chief.resolve_conflict("task-conflict", "mac2")
        assert result["resolved"] is True
        assert result["holder"] == "mac2"
        assert result["fence_token"] == 2  # bumped
        # Verify the lease is now authoritative for mac2
        info = chief.query("task-conflict")
        assert info["node_id"] == "mac2"
        assert info["provisional"] is False

    def test_resolve_nonexistent(self, store):
        """Resolving a nonexistent resource fails gracefully."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        result = chief.resolve_conflict("nope", "mac2")
        assert result["resolved"] is False

    def test_fence_token_invalidated_after_resolve(self, store):
        """Old fence tokens become invalid after conflict resolution."""
        chief = LeaseManager(store, "mac1", chief_node_id="mac1")
        r = chief.acquire("task-fence-inv")
        old_fence = r["fence_token"]
        chief.resolve_conflict("task-fence-inv", "mac2")
        # Old fence should be invalid (holder changed + fence bumped)
        v = chief.validate_fence_token("task-fence-inv", old_fence)
        assert v["valid"] is False


class TestPartitionRecovery:
    """End-to-end partition simulation: partition -> work -> heal -> reconcile."""

    def test_full_partition_recovery_flow(self, tmp_path):
        """Simulate: chief + worker partition, both acquire, heal, reconcile."""
        # Separate DBs to simulate true partition (no shared state)
        chief_db = tmp_path / "chief.db"
        worker_db = tmp_path / "worker.db"

        chief_store = FleetNerveStore(db_path=chief_db)
        worker_store = FleetNerveStore(db_path=worker_db)

        chief = LeaseManager(chief_store, "mac1", chief_node_id="mac1")
        worker = LeaseManager(worker_store, "mac2", chief_node_id="mac1", daemon_port=19999)

        # During partition: both acquire the same resource
        chief.acquire("task-X")  # authoritative, fence=1
        worker_result = worker.acquire("task-X")  # provisional, fence=1
        assert worker_result.get("provisional") is True

        # Partition heals: worker sends provisionals to chief
        worker_provisionals = worker.list_provisional(node_id="mac2")
        assert len(worker_provisionals) == 1

        verdicts = chief.reconcile(worker_provisionals)
        assert len(verdicts) == 1
        # Split-brain: same fence, different holders
        assert verdicts[0]["verdict"] == "conflict"

        # Arbiter resolves: mac2 wins
        resolution = chief.resolve_conflict("task-X", "mac2")
        assert resolution["resolved"] is True
        assert resolution["holder"] == "mac2"
        assert resolution["fence_token"] == 2  # bumped past conflict


class TestStats:
    def test_stats(self, mgr, mgr2):
        mgr.acquire("res-1")
        mgr2.acquire("res-2")
        mgr2.acquire("res-1")  # denied
        stats = mgr.stats
        assert stats["active"] == 2
        assert stats["this_node"] == 1
        assert stats["total_acquired"] == 1
