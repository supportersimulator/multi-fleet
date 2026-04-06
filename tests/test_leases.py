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


class TestAcquire:
    def test_acquire_available(self, mgr):
        result = mgr.acquire("branch/feature-x")
        assert result["acquired"] is True
        assert result["holder"] == "mac1"
        assert result["expires_at"] > time.time()

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

    def test_query_nonexistent(self, mgr):
        assert mgr.query("nope") is None


class TestStats:
    def test_stats(self, mgr, mgr2):
        mgr.acquire("res-1")
        mgr2.acquire("res-2")
        mgr2.acquire("res-1")  # denied
        stats = mgr.stats
        assert stats["active"] == 2
        assert stats["this_node"] == 1
        assert stats["total_acquired"] == 1
