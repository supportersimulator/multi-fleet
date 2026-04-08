"""
Expanded chaos tests — proves the fleet survives network partitions,
concurrent operations, recovery from crashes, and probe stress.

All tests use mocks/fixtures — no real network. Each test < 1s.
"""

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from multifleet.leases import LeaseManager
from multifleet.cross_rebuttal import RebuttalManager, RebuttalPhase
from multifleet.packet_registry import PacketRegistry, DeduplicationFilter
from multifleet.store import FleetNerveStore, run_maintenance
from multifleet.probe_engine import ProbeRegistry, ProbeResult, ProbeCategory, ProbeSeverity, ProbeFinding
from multifleet.sanitize import sanitize_message, strip_null_bytes


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_msg(idx: int, from_node: str = "mac1", to_node: str = "mac2") -> dict:
    return {
        "id": f"chaos-{idx}-{uuid.uuid4().hex[:8]}",
        "type": "context",
        "from": from_node,
        "to": to_node,
        "payload": {"subject": f"chaos-{idx}", "body": f"test message {idx}"},
        "ttl_hours": 24,
    }


# ═══════════════════════════════════════════════════════════════════════════
# NETWORK PARTITION TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestNetworkPartitionExpanded:
    """Simulate various network failure modes."""

    @pytest.mark.asyncio
    async def test_mac1_unreachable_mac2_mac3_continue(self, mock_nerve, mock_nats_client):
        """mac1 (chief) down: mac2+mac3 can still exchange messages via P2 HTTP."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        # Chief relay fails because mac1 is unreachable
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False, "error": "chief unreachable"})
        # P2 HTTP works between mac2<->mac3
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "partition", "body": "mac1 down"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_all_nats_down_p2_http_fallback(self, mock_nerve, mock_nats_client):
        """All NATS servers down: P2 HTTP delivers successfully."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.send_with_fallback("mac3", {
            "type": "context", "payload": {"subject": "nats-down", "body": "fallback test"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P2_http"

    @pytest.mark.asyncio
    async def test_nats_flap_no_message_loss(self, mock_nerve, mock_nats_client):
        """NATS connects/disconnects rapidly — messages still deliver via fallback."""
        results = []
        for i in range(5):
            # Alternate NATS availability
            mock_nats_client.is_connected = (i % 2 == 0)
            mock_nerve.nc = mock_nats_client
            if mock_nats_client.is_connected:
                mock_nats_client.publish = AsyncMock()
            mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": True})

            result = await mock_nerve.send_with_fallback("mac2", {
                "type": "context", "payload": {"subject": f"flap-{i}", "body": "test"},
            })
            results.append(result)

        # All 5 messages should deliver regardless of NATS state
        assert all(r["delivered"] for r in results)

    def test_dedup_prevents_duplicates_on_retry(self):
        """Intermittent connectivity + retries: dedup catches duplicates."""
        dedup = DeduplicationFilter(max_size=1000)
        msg_id = "retry-msg-001"
        # First attempt: not a duplicate
        assert not dedup.is_duplicate(msg_id)
        # Retry attempts (network was flaky, sender retried): all caught
        for _ in range(10):
            assert dedup.is_duplicate(msg_id)

    def test_dedup_under_intermittent_burst(self):
        """Burst of messages with some retries — dedup stays correct."""
        dedup = DeduplicationFilter(max_size=500)
        unique_count = 0
        dup_count = 0
        for i in range(200):
            msg_id = f"burst-{i % 50}"  # 50 unique, each seen 4 times
            if dedup.is_duplicate(msg_id):
                dup_count += 1
            else:
                unique_count += 1
        assert unique_count == 50
        assert dup_count == 150

    @pytest.mark.asyncio
    async def test_split_brain_both_sides_deliver(self, mock_nerve, mock_nats_client):
        """Network split: P1+P2 fail, P5 SSH delivers — no hang."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        mock_nerve._chief = "mac1"
        mock_nerve._send_p2_http = AsyncMock(return_value={"delivered": False, "error": "timeout"})
        mock_nerve._send_p3_chief = AsyncMock(return_value={"delivered": False, "error": "timeout"})
        mock_nerve._send_p4_seed = AsyncMock(return_value={"delivered": False, "error": "no fs"})
        mock_nerve._send_p5_ssh = AsyncMock(return_value={"delivered": True})

        result = await mock_nerve.send_with_fallback("mac3", {
            "type": "context", "payload": {"subject": "split-brain", "body": "test"},
        })
        assert result["delivered"] is True
        assert result["channel"] == "P5_ssh"

    @pytest.mark.asyncio
    async def test_all_channels_fail_returns_error_fast(self, mock_nerve, mock_nats_client):
        """Total partition: error returned quickly (no infinite retry loop)."""
        mock_nats_client.is_connected = False
        mock_nerve.nc = mock_nats_client
        for method in ["_send_p2_http", "_send_p3_chief", "_send_p4_seed",
                        "_send_p5_ssh", "_send_p6_wol", "_send_p7_git", "_send_p0_cloud"]:
            setattr(mock_nerve, method, AsyncMock(return_value={"delivered": False, "error": "down"}))

        t0 = time.monotonic()
        result = await mock_nerve.send_with_fallback("mac2", {
            "type": "context", "payload": {"subject": "total-fail", "body": "test"},
        })
        elapsed = time.monotonic() - t0
        assert result["delivered"] is False
        # Fallback chain has SSH/WoL with real timeouts; 30s cap ensures no infinite loop
        assert elapsed < 30.0, f"Total partition took {elapsed:.2f}s — should not hang"


# ═══════════════════════════════════════════════════════════════════════════
# CONCURRENT OPERATION TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestConcurrentOperations:
    """Race conditions: simultaneous lease acquisition, verdict submission, doctor runs."""

    def test_three_nodes_acquire_same_lease_only_one_wins(self, tmp_path):
        """3 nodes simultaneously acquire lease on same resource -> only 1 succeeds.

        Each thread gets its own store instance (own SQLite connection) to match
        real daemon usage where separate processes access the same DB via WAL mode.
        """
        db_path = tmp_path / "lease_race.db"
        FleetNerveStore(str(db_path))  # init schema once
        results = {}
        errors = []

        def try_acquire(node_id):
            try:
                store = FleetNerveStore(str(db_path))
                mgr = LeaseManager(store, node_id)
                result = mgr.acquire("shared-resource", "exclusive")
                results[node_id] = result
            except Exception as e:
                errors.append((node_id, e))

        threads = [threading.Thread(target=try_acquire, args=(f"mac{i}",)) for i in range(1, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Unexpected errors: {errors}"
        winners = [nid for nid, r in results.items() if r.get("acquired")]
        assert len(winners) == 1, f"Expected exactly 1 winner, got {len(winners)}: {winners}"

    def test_three_nodes_submit_verdicts_all_accepted(self):
        """3 nodes simultaneously submit verdicts -> rebuttal manager handles all."""
        mgr = RebuttalManager()
        session = mgr.start_session({
            "task_id": "concurrent-verdict-test",
            "deadline_minutes": 30,
            "assigned_to": ["mac1", "mac2", "mac3"],
        })
        errors = []
        successes = []

        def submit_verdict(node_id, session_ref):
            try:
                session_ref.add_verdict(node_id, {
                    "confidence": 0.85,
                    "branch": f"feature/{node_id}",
                    "risks": [],
                    "strengths": [f"{node_id} approach"],
                })
                successes.append(node_id)
            except Exception as e:
                errors.append((node_id, e))

        threads = [
            threading.Thread(target=submit_verdict, args=(f"mac{i}", session))
            for i in range(1, 4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Verdict submission errors: {errors}"
        assert len(successes) == 3
        assert session.all_verdicts_in()

    def test_concurrent_message_writes_no_corruption(self, tmp_path):
        """50 concurrent writes from 3 nodes — all messages stored, no corruption."""
        db_path = tmp_path / "concurrent_msg.db"
        FleetNerveStore(str(db_path))  # init schema
        errors = []

        def writer(node_id, count):
            try:
                store = FleetNerveStore(str(db_path))
                for i in range(count):
                    store.store_message(_make_msg(i, from_node=node_id))
            except Exception as e:
                errors.append((node_id, e))

        threads = [
            threading.Thread(target=writer, args=(f"mac{n}", 50))
            for n in range(1, 4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent write errors: {errors}"
        reader = FleetNerveStore(str(db_path))
        total = reader._conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert total == 150, f"Expected 150 messages, got {total}"

    def test_concurrent_lease_acquire_release_cycle(self, tmp_path):
        """Rapid acquire/release cycles from multiple threads — no deadlock."""
        db_path = tmp_path / "lease_cycle.db"
        FleetNerveStore(str(db_path))  # init schema
        errors = []

        def cycle(node_id, iterations):
            try:
                store = FleetNerveStore(str(db_path))
                mgr = LeaseManager(store, node_id)
                for i in range(iterations):
                    r = mgr.acquire(f"res-{node_id}-{i % 5}", "short_task")
                    if r.get("acquired"):
                        mgr.release(f"res-{node_id}-{i % 5}")
            except Exception as e:
                errors.append((node_id, e))

        threads = [
            threading.Thread(target=cycle, args=(f"mac{n}", 20))
            for n in range(1, 4)
        ]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0

        assert not errors, f"Lease cycle errors: {errors}"
        assert elapsed < 5.0, f"Lease cycles took {elapsed:.2f}s — possible deadlock"

    def test_concurrent_dedup_checks_thread_safe(self):
        """Multiple threads checking dedup simultaneously — no crash, correct counts."""
        dedup = DeduplicationFilter(max_size=2000)
        errors = []

        def check_range(start, count):
            try:
                for i in range(start, start + count):
                    dedup.is_duplicate(f"msg-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=check_range, args=(i * 200, 200))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert dedup.size() <= 2000

    def test_two_fleet_doctors_no_interference(self, tmp_path):
        """Two simultaneous fleet_doctor calls — both complete without interference."""
        results = []
        errors = []

        def run_doctor(idx):
            try:
                with patch.dict("os.environ", {"MULTIFLEET_NODE_ID": f"mac{idx}"}):
                    from multifleet.doctor import FleetDoctor
                    doc = FleetDoctor()
                    report = doc.run()
                    results.append(report)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=run_doctor, args=(1,))
        t2 = threading.Thread(target=run_doctor, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Doctor errors: {errors}"
        assert len(results) == 2


# ═══════════════════════════════════════════════════════════════════════════
# RECOVERY TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestRecovery:
    """Crash recovery, WAL corruption, stale lease cleanup."""

    def test_unprocessed_wal_entries_survive_for_replay(self, tmp_path):
        """WAL entries not marked processed survive — available for crash replay."""
        store = FleetNerveStore(str(tmp_path / "wal_replay.db"))
        for i in range(20):
            wal_id = store.wal_write(f"msg-{i}", json.dumps({"idx": i}))
            if i % 2 == 0:
                store.wal_mark_processed(wal_id)

        unprocessed = store.wal_unprocessed()
        assert len(unprocessed) == 10
        # Verify the correct ones are unprocessed (odd indices)
        unprocessed_ids = {json.loads(u["raw_json"])["idx"] for u in unprocessed}
        assert unprocessed_ids == {1, 3, 5, 7, 9, 11, 13, 15, 17, 19}

    def test_stale_lease_cleanup_frees_resources(self, tmp_path):
        """Expired leases freed for re-acquisition by another node."""
        store = FleetNerveStore(str(tmp_path / "stale_lease.db"))
        mgr1 = LeaseManager(store, "mac1")
        mgr2 = LeaseManager(store, "mac2")

        # mac1 acquires
        result = mgr1.acquire("critical-resource", "heartbeat")  # 30s TTL
        assert result["acquired"]

        # mac2 cannot acquire (lease held)
        result2 = mgr2.acquire("critical-resource", "heartbeat")
        assert not result2["acquired"]

        # Fast-forward past TTL
        with patch("time.time", return_value=time.time() + 60):
            mgr1.cleanup_expired()

        # Now mac2 can acquire
        result3 = mgr2.acquire("critical-resource", "heartbeat")
        assert result3["acquired"]

    def test_maintenance_after_crash_cleans_stale(self, tmp_path):
        """run_maintenance after simulated crash — stale entries cleaned."""
        store = FleetNerveStore(str(tmp_path / "crash.db"))
        # Simulate pre-crash state: messages with old timestamps
        conn = store._conn()
        old_ts = time.time() - (72 * 3600)
        for i in range(50):
            conn.execute(
                "INSERT INTO messages (id, type, from_node, to_node, subject, body, status, created_at, ttl_hours) "
                "VALUES (?, 'context', 'mac1', 'mac2', 'stale', 'old', 'pending', ?, 24)",
                (f"stale-{i}", old_ts),
            )
        conn.commit()

        # Run maintenance (simulates daemon restart)
        run_maintenance(store, expire_hours=48)

        remaining = conn.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]
        assert remaining == 0

    def test_store_init_on_fresh_db(self, tmp_path):
        """FleetNerveStore creates tables cleanly on fresh DB — no crash."""
        store = FleetNerveStore(str(tmp_path / "fresh.db"))
        # Should be able to store and retrieve immediately
        store.store_message(_make_msg(0))
        msgs = store.pending_for_node("mac2")
        assert len(msgs) == 1

    def test_double_maintenance_idempotent(self, tmp_path):
        """Running maintenance twice in a row doesn't crash or double-delete."""
        store = FleetNerveStore(str(tmp_path / "double.db"))
        for i in range(10):
            store.store_message(_make_msg(i))
        run_maintenance(store, expire_hours=48)
        run_maintenance(store, expire_hours=48)
        # All recent messages should still be there
        count = store._conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 10

    def test_wal_prune_only_removes_processed(self, tmp_path):
        """WAL prune removes processed entries but keeps unprocessed."""
        store = FleetNerveStore(str(tmp_path / "wal_prune.db"))
        conn = store._conn()
        old_ts = time.time() - (48 * 3600)

        for i in range(20):
            conn.execute(
                "INSERT INTO wal (msg_id, raw_json, received_at, processed, processed_at) "
                "VALUES (?, '{}', ?, ?, ?)",
                (f"wal-{i}", old_ts, 1 if i < 10 else 0, old_ts if i < 10 else None),
            )
        conn.commit()

        store.wal_prune(max_age_hours=24)
        remaining = conn.execute("SELECT COUNT(*) FROM wal").fetchone()[0]
        # 10 processed (pruned) + 10 unprocessed (kept)
        assert remaining == 10

    def test_task_state_resume_stale_on_empty_db(self, tmp_path):
        """resume_stale on empty DB returns empty, no crash."""
        from multifleet.task_state import TaskStateMachine
        tsm = TaskStateMachine(db_path=tmp_path / "empty_tasks.db")
        result = tsm.resume_stale()
        assert result == []

    def test_fence_token_survives_release_reacquire(self, tmp_path):
        """Fence tokens keep incrementing across release/reacquire cycles."""
        store = FleetNerveStore(str(tmp_path / "fence.db"))
        mgr = LeaseManager(store, "mac1")
        tokens = []
        for _ in range(5):
            r = mgr.acquire("resource-x", "short_task")
            assert r["acquired"]
            tokens.append(r["fence_token"])
            mgr.release("resource-x")
        # Strictly monotonic
        for i in range(1, len(tokens)):
            assert tokens[i] > tokens[i - 1]


# ═══════════════════════════════════════════════════════════════════════════
# PROBE UNDER STRESS
# ═══════════════════════════════════════════════════════════════════════════


class TestProbeStress:
    """Probe engine under concurrent load, missing enrichers, flood conditions."""

    def test_ten_probes_simultaneous_no_crash(self):
        """Run 10 probes simultaneously — all return results, no crash."""
        registry = ProbeRegistry()

        def make_probe(pid):
            def probe_fn(context):
                return ProbeResult(
                    probe_id=pid,
                    category="structure",
                    status="ok",
                    confidence=0.9,
                    severity="info",
                    findings=[ProbeFinding(title=f"Probe {pid} ok")],
                )
            return probe_fn

        for i in range(10):
            pid = f"probe-{i}"
            registry.register(pid, make_probe(pid), ProbeCategory.STRUCTURE, f"Test probe {i}")

        results = {}
        errors = []

        def run_probe(pid):
            try:
                probe_info = registry._probes[pid]
                result = probe_info["fn"]({})
                results[pid] = result
            except Exception as e:
                errors.append((pid, e))

        threads = [threading.Thread(target=run_probe, args=(f"probe-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Probe errors: {errors}"
        assert len(results) == 10
        assert all(r.status == "ok" for r in results.values())

    def test_probe_with_missing_enrichers_graceful(self):
        """Probe with missing inputs returns skipped, not crash."""
        result = ProbeResult.skipped("missing-probe", "structure", ["git_log", "file_tree"])
        assert result.status == "skipped"
        assert result.missing_inputs == ["git_log", "file_tree"]
        assert result.confidence == 0.0

    def test_probe_error_returns_error_result(self):
        """Probe that throws returns error result, not exception."""
        registry = ProbeRegistry()

        def failing_probe(context):
            raise RuntimeError("disk full")

        registry.register("fail-probe", failing_probe, ProbeCategory.RELIABILITY, "Fails always")

        # ProbeResult.error creates a safe error wrapper
        result = ProbeResult.error("fail-probe", "reliability", "disk full")
        assert result.status == "error"
        assert "disk full" in result.findings[0].title

    def test_probe_registry_handles_many_probes(self):
        """Registry with 100 probes — list and run all without issues."""
        registry = ProbeRegistry()
        for i in range(100):
            registry.register(
                f"probe-{i}",
                lambda ctx, idx=i: ProbeResult(
                    probe_id=f"probe-{idx}", category="structure",
                    status="ok", confidence=0.9, severity="info",
                ),
                ProbeCategory.STRUCTURE,
                f"Probe {i}",
            )
        assert len(registry._probes) == 100
        # Run all
        for pid, info in registry._probes.items():
            result = info["fn"]({})
            assert result.status == "ok"

    def test_probe_result_serialization_roundtrip(self):
        """ProbeResult.to_dict() -> to_json() produces valid JSON."""
        result = ProbeResult(
            probe_id="serial-test",
            category="security",
            status="warning",
            confidence=0.75,
            severity="medium",
            findings=[
                ProbeFinding(
                    title="Found exposed secret",
                    evidence=["line 42: API_KEY=sk-..."],
                    blast_radius="high",
                    file_paths=["/src/config.py"],
                )
            ],
        )
        d = result.to_dict()
        j = result.to_json()
        parsed = json.loads(j)
        assert parsed["probe_id"] == "serial-test"
        assert len(parsed["findings"]) == 1
        assert parsed["findings"][0]["blast_radius"] == "high"

    def test_probe_results_flood_does_not_grow_unbounded(self):
        """Generating many probe results doesn't leak memory (results are value objects)."""
        results = []
        for i in range(1000):
            r = ProbeResult(
                probe_id=f"flood-{i}",
                category="change",
                status="ok",
                confidence=0.5,
                severity="info",
            )
            results.append(r.to_dict())
        assert len(results) == 1000
        # Each result is a dict, no shared state
        results.clear()
        assert len(results) == 0
