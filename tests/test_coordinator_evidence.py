"""Tests for coordinator_evidence.py — evidence pipeline to global chain."""

import time

import pytest

from multifleet.coordinator_evidence import EvidencePipeline, EVENT_TYPES, MAX_QUEUE_SIZE


class MockLedger:
    """Mock EvidenceLedger for testing."""

    def __init__(self, fail_after=None):
        self.entries = []
        self._fail_after = fail_after
        self._call_count = 0

    def record(self, event_type, node_id, subject, payload):
        self._call_count += 1
        if self._fail_after is not None and self._call_count > self._fail_after:
            raise RuntimeError("ledger write failed")
        entry = {
            "event_type": event_type,
            "node_id": node_id,
            "subject": subject,
            "payload": payload,
            "entry_id": f"mock-{self._call_count}",
        }
        self.entries.append(entry)
        return entry


class TestEvidencePipeline:
    def test_init_with_ledger(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        assert ep.node_id == "mac1"
        assert ep._ledger is ledger

    def test_init_without_ledger(self):
        ep = EvidencePipeline(node_id="mac1", ledger=None)
        # Won't crash, just won't have ledger
        assert ep.node_id == "mac1"

    def test_record_to_ledger(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record("coordinator_started", "agent started", {"key": "val"})
        assert result is not None
        assert result["event_type"] == "coordinator_started"
        assert ep.stats["recorded"] == 1
        assert ep.queue_size == 0

    def test_record_queues_on_failure(self):
        ledger = MockLedger(fail_after=0)  # fails immediately
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record("coordinator_heartbeat", "hb", {})
        assert result is None
        assert ep.queue_size == 1
        assert ep.stats["queued"] == 1

    def test_record_queues_without_ledger(self):
        ep = EvidencePipeline(node_id="mac1", ledger=None)
        ep._ledger = None  # ensure no auto-init
        result = ep.record("coordinator_heartbeat", "hb", {})
        assert result is None
        assert ep.queue_size == 1

    def test_queue_overflow(self):
        ep = EvidencePipeline(node_id="mac1", ledger=None)
        ep._ledger = None
        for i in range(MAX_QUEUE_SIZE + 50):
            ep.record("coordinator_heartbeat", f"hb-{i}", {})
        assert ep.queue_size == MAX_QUEUE_SIZE

    def test_flush_queue(self):
        ledger = MockLedger(fail_after=0)
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)

        # Queue some events
        ep.record("coordinator_heartbeat", "hb1", {})
        ep.record("coordinator_heartbeat", "hb2", {})
        assert ep.queue_size == 2

        # Fix the ledger
        ledger._fail_after = None
        flushed = ep.flush_queue()
        assert flushed == 2
        assert ep.queue_size == 0
        assert ep.stats["flushed"] == 2

    def test_flush_empty_queue(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        flushed = ep.flush_queue()
        assert flushed == 0

    def test_flush_partial(self):
        """Some events flush, others fail."""
        ledger = MockLedger(fail_after=1)  # first succeeds, rest fail
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        ep._ledger = None  # force queuing
        ep.record("coordinator_heartbeat", "hb1", {})
        ep.record("coordinator_heartbeat", "hb2", {})
        ep.record("coordinator_heartbeat", "hb3", {})
        assert ep.queue_size == 3

        ep._ledger = ledger
        flushed = ep.flush_queue()
        assert flushed == 1
        assert ep.queue_size == 2

    # ── Convenience methods ──

    def test_record_health_change(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record_health_change("redis", healthy=False, details={"port": 6379})
        assert result is not None
        assert "degraded" in result["subject"]

    def test_record_health_recovered(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record_health_change("redis", healthy=True)
        assert "recovered" in result["subject"]

    def test_record_heartbeat(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record_heartbeat({"overall_healthy": True})
        assert result is not None
        assert result["event_type"] == "coordinator_heartbeat"

    def test_record_task_assigned(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac2", ledger=ledger)
        result = ep.record_task_assigned("task-123", "mac1", {"prompt": "do thing"})
        assert "task-123" in result["subject"]

    def test_record_task_completed(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac2", ledger=ledger)
        result = ep.record_task_completed("task-123", 1500.0)
        assert result["event_type"] == "coordinator_task_completed"

    def test_record_task_failed(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac2", ledger=ledger)
        result = ep.record_task_failed("task-123", "timeout")
        assert result["event_type"] == "coordinator_task_failed"

    def test_record_surgeon_outcome(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record_surgeon_outcome({"operation": "cardio_review", "success": True})
        assert result["event_type"] == "coordinator_surgeon_outcome"

    def test_record_surgeon_availability(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        avail = {
            "atlas": {"available": True},
            "cardiologist": {"available": True},
            "neurologist": {"available": False},
        }
        result = ep.record_surgeon_availability(avail)
        assert "atlas" in result["subject"]
        assert "cardiologist" in result["subject"]

    def test_record_fleet_decision(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac2", ledger=ledger)
        result = ep.record_fleet_decision({"decision": "merge", "task_id": "t-1"})
        assert result["event_type"] == "coordinator_fleet_decision"

    def test_record_self_heal(self):
        ledger = MockLedger()
        ep = EvidencePipeline(node_id="mac1", ledger=ledger)
        result = ep.record_self_heal("restart_nats", "mac2", success=True)
        assert "ok" in result["subject"]


class TestEventTypes:
    def test_all_event_types_defined(self):
        """All convenience methods should use documented event types."""
        assert "coordinator_health_change" in EVENT_TYPES
        assert "coordinator_heartbeat" in EVENT_TYPES
        assert "coordinator_task_assigned" in EVENT_TYPES
        assert "coordinator_task_completed" in EVENT_TYPES
        assert "coordinator_task_failed" in EVENT_TYPES
        assert "coordinator_surgeon_outcome" in EVENT_TYPES
        assert "coordinator_started" in EVENT_TYPES
        assert "coordinator_stopped" in EVENT_TYPES
