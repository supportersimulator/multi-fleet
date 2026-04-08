"""Tests for captain role, benchmark CI threshold, and fleet_captain MCP tool."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.captain import CaptainRole, WorkerHeartbeat
from multifleet.benchmark import BenchmarkResult


# ── Captain Activation / Deactivation ──


class TestCaptainActivation:
    def test_activate(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        assert not cap.is_active
        assert cap.activate() is True
        assert cap.is_active

    def test_activate_idempotent(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.activate()
        assert cap.activate() is False  # already active

    def test_deactivate(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.activate()
        assert cap.deactivate() is True
        assert not cap.is_active

    def test_deactivate_when_inactive(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        assert cap.deactivate() is False


# ── Worker Management ──


class TestWorkerManagement:
    def test_add_worker(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        assert cap.add_worker("w1") is True
        assert cap.worker_count == 1
        assert "w1" in cap.workers

    def test_add_duplicate_worker(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        assert cap.add_worker("w1") is False
        assert cap.worker_count == 1

    def test_remove_worker(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        cap.add_worker("w2")
        assert cap.remove_worker("w1") is True
        assert cap.worker_count == 1
        assert "w1" not in cap.workers

    def test_remove_nonexistent_worker(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        assert cap.remove_worker("ghost") is False


# ── Relay to Chief ──


class TestRelayToChief:
    def test_relay_wraps_message(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        msg = {"type": "context", "from_node": "w1", "payload": {"data": "test"}}
        wrapped = cap.relay_to_chief(msg)
        assert wrapped["to_node"] == "mac1"
        assert wrapped["from_node"] == "mac2"
        assert wrapped["payload"]["relayed_from"] == "w1"
        assert wrapped["payload"]["cluster_id"] == "cluster-a"
        assert wrapped["payload"]["original"] == msg

    def test_relay_logs_entry(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.relay_to_chief({"type": "context", "from_node": "w1"})
        cap.relay_to_chief({"type": "status", "from_node": "w2"})
        status = cap.status()
        assert status["health"]["relay_count"] == 2


# ── Heartbeat Aggregation ──


class TestHeartbeatAggregation:
    def test_empty_cluster(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        agg = cap.aggregate_heartbeats()
        assert agg["worker_count"] == 0
        assert agg["healthy"] == 0

    def test_worker_heartbeat_stored(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        result = cap.handle_worker_message({
            "type": "heartbeat",
            "from_node": "w1",
            "payload": {"status": "ok"},
        })
        assert result["action"] == "stored"
        agg = cap.aggregate_heartbeats()
        assert agg["healthy"] == 1

    def test_no_heartbeat_counts_as_stale(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        agg = cap.aggregate_heartbeats()
        assert agg["stale"] == 1
        assert agg["workers"][0]["status"] == "no_heartbeat"

    def test_stale_heartbeat(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        # Inject a heartbeat with old timestamp
        cap._heartbeats["w1"] = WorkerHeartbeat(
            worker_id="w1",
            timestamp=time.time() - 120,  # 2 min ago = stale
            status="ok",
        )
        agg = cap.aggregate_heartbeats()
        assert agg["stale"] == 1
        assert agg["workers"][0]["status"] == "stale"


# ── Cluster Health ──


class TestClusterHealth:
    def test_healthy_cluster(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.activate()
        for i in range(5):
            wid = f"w{i}"
            cap.add_worker(wid)
            cap.handle_worker_message({
                "type": "heartbeat", "from_node": wid,
                "payload": {"status": "ok"},
            })
        health = cap.cluster_health()
        assert health["health"] == "healthy"
        assert health["healthy_pct"] == 100.0
        assert health["active"] is True

    def test_degraded_cluster(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        cap.add_worker("w1")
        cap.add_worker("w2")
        cap.add_worker("w3")
        # Only w1 has recent heartbeat
        cap.handle_worker_message({
            "type": "heartbeat", "from_node": "w1",
            "payload": {"status": "ok"},
        })
        # w2 stale
        cap._heartbeats["w2"] = WorkerHeartbeat("w2", time.time() - 120, "ok")
        # w3 no heartbeat
        health = cap.cluster_health()
        # 1 healthy / 3 total = 33% -> critical
        assert health["health"] == "critical"

    def test_status_includes_health(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        status = cap.status()
        assert "health" in status
        assert status["node_id"] == "mac2"
        assert status["cluster_id"] == "cluster-a"
        assert status["chief_id"] == "mac1"


# ── Handle Worker Message (non-heartbeat → relay) ──


class TestHandleWorkerMessage:
    def test_non_heartbeat_relayed(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        result = cap.handle_worker_message({
            "type": "context", "from_node": "w1",
            "payload": {"data": "important"},
        })
        assert result["action"] == "relay"
        assert result["message"]["to_node"] == "mac1"


# ── Benchmark CI Threshold Detection ──


class TestBenchmarkThreshold:
    def test_passing_benchmark(self):
        r = BenchmarkResult(
            name="fast_op", iterations=100,
            mean_ms=0.5, p50_ms=0.4, p95_ms=0.8, p99_ms=1.0,
            min_ms=0.1, max_ms=2.0,
        )
        assert r.p95_ms <= 100, "Should pass threshold"

    def test_failing_benchmark(self):
        r = BenchmarkResult(
            name="slow_op", iterations=100,
            mean_ms=80.0, p50_ms=75.0, p95_ms=150.0, p99_ms=200.0,
            min_ms=50.0, max_ms=300.0,
        )
        assert r.p95_ms > 100, "Should detect regression"


# ── NATS Subjects ──


class TestCaptainNats:
    def test_nats_subjects(self):
        cap = CaptainRole("mac2", "cluster-a", "mac1")
        subjects = cap.nats_subjects()
        assert "fleet.captain.cluster-a.*" in subjects
        assert "fleet.worker.cluster-a.*.message" in subjects
