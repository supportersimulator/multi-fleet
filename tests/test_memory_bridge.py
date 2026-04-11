"""Tests for Evidence → ContextDNA Memory Bridge."""

import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from multifleet.memory_bridge import MemoryBridge


class FakeLedger:
    """Minimal evidence ledger stub for testing."""

    def __init__(self, entries=None):
        self._entries = entries or []

    def query(self, since=None, limit=50, **kwargs):
        if since:
            return [e for e in self._entries if e.get("timestamp", 0) >= since]
        return self._entries[:limit]


class FakeEventBus:
    def __init__(self):
        self.events = []

    def emit(self, event_type, payload):
        self.events.append({"type": event_type, "payload": payload})


@pytest.fixture(autouse=True)
def _no_external_calls(monkeypatch):
    """Prevent all tests from calling brain.py or auto_capture (subprocess)."""
    monkeypatch.setattr(MemoryBridge, "_call_brain", lambda *a, **k: True)
    monkeypatch.setattr(MemoryBridge, "_call_auto_capture", lambda *a, **k: True)


def _make_entry(entry_id="abc-123", event_type="merge_decision",
                severity="high", subject="merged feature-x",
                node_id="mac2", timestamp=None):
    return {
        "entry_id": entry_id,
        "timestamp": timestamp or time.time(),
        "event_type": event_type,
        "node_id": node_id,
        "subject": subject,
        "payload": json.dumps({"severity": severity, "details": "test details"}),
        "entry_hash": "fakehash123",
    }


def test_promote_creates_wisdom_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        entry = _make_entry()
        ledger = FakeLedger([entry])
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)

        promoted = bridge.promote_recent(hours=1, min_severity="medium")

        assert len(promoted) == 1
        # Check wisdom file was created
        files = [f for f in os.listdir(tmpdir) if f.endswith(".json") and not f.startswith(".")]
        assert len(files) == 1

        wisdom = json.loads(open(os.path.join(tmpdir, files[0])).read())
        assert wisdom["source"] == "fleet-evidence"
        assert wisdom["entry_id"] == "abc-123"
        assert wisdom["event_type"] == "merge_decision"
        assert wisdom["action"] == "success"
        assert wisdom["area"] == "architecture"


def test_promote_filters_by_severity():
    with tempfile.TemporaryDirectory() as tmpdir:
        entries = [
            _make_entry(entry_id="low-1", severity="low"),
            _make_entry(entry_id="high-1", severity="high"),
            _make_entry(entry_id="med-1", severity="medium"),
        ]
        ledger = FakeLedger(entries)
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)

        promoted = bridge.promote_recent(hours=1, min_severity="high")

        ids = [e["entry_id"] for e in promoted]
        assert "high-1" in ids
        assert "low-1" not in ids
        assert "med-1" not in ids


def test_already_promoted_skipped():
    with tempfile.TemporaryDirectory() as tmpdir:
        entry = _make_entry(entry_id="dup-1")
        ledger = FakeLedger([entry])
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)

        # First promotion
        p1 = bridge.promote_recent(hours=1)
        assert len(p1) == 1

        # Second promotion — same entry should be skipped
        p2 = bridge.promote_recent(hours=1)
        assert len(p2) == 0


def test_promotion_stats():
    with tempfile.TemporaryDirectory() as tmpdir:
        entries = [
            _make_entry(entry_id="s1", event_type="merge_decision", severity="high"),
            _make_entry(entry_id="s2", event_type="scan_finding", severity="critical"),
        ]
        ledger = FakeLedger(entries)
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)
        bridge.promote_recent(hours=1, min_severity="medium")

        stats = bridge.get_wisdom_stats()
        assert stats["total_promoted"] == 2
        assert stats["wisdom_files"] == 2
        assert stats["session_promoted"] == 2
        assert "merge_decision" in stats["by_event_type"]
        assert "scan_finding" in stats["by_event_type"]


def test_brain_integration(monkeypatch):
    """Test that brain.py is called (mocked via method)."""
    calls = []

    def mock_brain(self, action, task, details):
        calls.append({"action": action, "task": task, "details": details})
        return True

    # Override the autouse fixture's mock with our tracking mock
    monkeypatch.setattr(MemoryBridge, "_call_brain", mock_brain)

    with tempfile.TemporaryDirectory() as tmpdir:
        entry = _make_entry(severity="high")
        ledger = FakeLedger([entry])
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)
        bridge.promote_entry(entry)

        assert len(calls) >= 1
        assert calls[0]["action"] == "success"


def test_eventbus_emits_on_promotion():
    with tempfile.TemporaryDirectory() as tmpdir:
        entry = _make_entry(severity="high")
        ledger = FakeLedger([entry])
        bus = FakeEventBus()
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir,
                              eventbus=bus)
        bridge.promote_entry(entry)

        assert len(bus.events) == 1
        assert bus.events[0]["type"] == "evidence.promoted"
        assert bus.events[0]["payload"]["entry_id"] == "abc-123"


def test_run_promotion_cycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        entries = [_make_entry(entry_id=f"cyc-{i}", severity="high")
                   for i in range(3)]
        ledger = FakeLedger(entries)
        bridge = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)

        result = bridge.run_promotion_cycle()
        assert result["promoted_count"] == 3
        assert result["total_promoted"] == 3
        assert result["elapsed_s"] >= 0


def test_promoted_ids_persist_across_instances():
    with tempfile.TemporaryDirectory() as tmpdir:
        entry = _make_entry(entry_id="persist-1", severity="high")
        ledger = FakeLedger([entry])

        # First instance promotes
        b1 = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)
        b1.promote_recent(hours=1)
        assert len(b1._promoted_ids) == 1

        # Second instance should load the promoted ID
        b2 = MemoryBridge(evidence_ledger=ledger, memory_path=tmpdir)
        assert "persist-1" in b2._promoted_ids

        # And skip it
        p2 = b2.promote_recent(hours=1)
        assert len(p2) == 0


def test_no_ledger_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        bridge = MemoryBridge(evidence_ledger=None, memory_path=tmpdir)
        result = bridge.promote_recent()
        assert result == []
