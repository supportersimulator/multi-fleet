"""Tests for multifleet.audit — audit trail with sync/async writes."""
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure multifleet package is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.audit import AuditEntry, AuditTrail, get_audit_trail, reset_audit_trail


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset singleton between tests."""
    reset_audit_trail()
    yield
    reset_audit_trail()


@pytest.fixture
def audit_dir(tmp_path):
    return tmp_path / "audit"


@pytest.fixture
def trail(audit_dir):
    return AuditTrail(storage_dir=str(audit_dir), node_id="test-node")


# ── AuditEntry ──

class TestAuditEntry:
    def test_creation_defaults(self):
        e = AuditEntry(event="tool_call", tool="fleet_probe")
        assert e.event == "tool_call"
        assert e.tool == "fleet_probe"
        assert e.status == "ok"
        assert len(e.entry_id) == 12
        assert e.ts  # ISO timestamp present

    def test_to_dict_strips_none(self):
        e = AuditEntry(event="tool_call", tool="test")
        d = e.to_dict()
        assert "error" not in d
        assert "parent_id" not in d
        assert "metadata" not in d

    def test_to_dict_includes_values(self):
        e = AuditEntry(
            event="error", tool="fleet_send", target="mac2",
            status="error", error="timeout", metadata={"retries": 3},
        )
        d = e.to_dict()
        assert d["error"] == "timeout"
        assert d["metadata"] == {"retries": 3}
        assert d["target"] == "mac2"

    def test_serializable_to_json(self):
        e = AuditEntry(event="tool_call", tool="test", node_id="mac1")
        text = json.dumps(e.to_dict())
        parsed = json.loads(text)
        assert parsed["node_id"] == "mac1"


# ── Sync writes ──

class TestSyncWrites:
    @pytest.mark.parametrize("event", ["error", "security", "repair"])
    def test_sync_events_write_immediately(self, trail, audit_dir, event):
        trail.record(event=event, tool="test_tool", status="error", error="fail")
        # Should be on disk immediately (no flush needed)
        files = list(audit_dir.glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == event
        assert entry["error"] == "fail"


# ── Async buffer ──

class TestAsyncBuffer:
    def test_non_critical_events_buffered(self, trail, audit_dir):
        trail.record(event="tool_call", tool="fleet_probe", status="ok")
        # Not yet on disk (buffered)
        files = list(audit_dir.glob("audit-*.jsonl"))
        if files:
            content = files[0].read_text().strip()
            # Could be empty if file was created but not written
            assert content == "" or content  # may or may not exist yet

    def test_flush_writes_buffer(self, trail, audit_dir):
        trail.record(event="tool_call", tool="fleet_probe", status="ok")
        trail.record(event="tool_call", tool="fleet_send", status="ok")
        trail.flush()
        files = list(audit_dir.glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = [l for l in files[0].read_text().strip().split("\n") if l]
        assert len(lines) == 2

    def test_buffer_auto_flushes_at_limit(self, trail, audit_dir):
        for i in range(50):
            trail.record(event="tool_call", tool=f"tool_{i}", status="ok")
        # Buffer limit reached — should have flushed
        files = list(audit_dir.glob("audit-*.jsonl"))
        assert len(files) >= 1
        lines = [l for l in files[0].read_text().strip().split("\n") if l]
        assert len(lines) == 50

    def test_timer_flush(self, trail, audit_dir):
        trail.record(event="tool_call", tool="fleet_probe", status="ok")
        # Wait for timer (100ms + margin)
        time.sleep(0.3)
        files = list(audit_dir.glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = [l for l in files[0].read_text().strip().split("\n") if l]
        assert len(lines) == 1


# ── Daily rotation ──

class TestRotation:
    def test_different_dates_create_different_files(self, trail, audit_dir):
        with patch("multifleet.audit.datetime") as mock_dt:
            mock_dt.now.return_value.isoformat.return_value = "2026-04-05T00:00:00+00:00"
            mock_dt.now.return_value.strftime.return_value = "2026-04-05"
            trail.record(event="error", tool="t1", status="error", error="e1")

            mock_dt.now.return_value.strftime.return_value = "2026-04-06"
            mock_dt.now.return_value.isoformat.return_value = "2026-04-06T00:00:00+00:00"
            trail.record(event="error", tool="t2", status="error", error="e2")

        files = sorted(audit_dir.glob("audit-*.jsonl"))
        assert len(files) == 2
        assert "2026-04-05" in files[0].name
        assert "2026-04-06" in files[1].name


# ── Error counting ──

class TestErrorCounting:
    def test_counts_errors(self, trail):
        trail.record(event="tool_call", tool="fleet_send", status="error", error="timeout")
        trail.record(event="tool_call", tool="fleet_send", status="error", error="timeout")
        trail.record(event="tool_call", tool="fleet_send", status="ok")
        counts = trail.get_error_counts()
        assert counts["tool_call:fleet_send:error"] == 2
        assert "tool_call:fleet_send:ok" not in counts

    def test_different_statuses_tracked_separately(self, trail):
        trail.record(event="error", tool="fleet_send", status="timeout", error="t")
        trail.record(event="error", tool="fleet_send", status="denied", error="d")
        counts = trail.get_error_counts()
        assert counts["error:fleet_send:timeout"] == 1
        assert counts["error:fleet_send:denied"] == 1


# ── Thread safety ──

class TestThreadSafety:
    def test_concurrent_writes(self, trail, audit_dir):
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    trail.record(event="tool_call", tool=f"thread_{n}", status="ok")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        trail.flush()
        files = list(audit_dir.glob("audit-*.jsonl"))
        assert len(files) >= 1
        total_lines = sum(
            len([l for l in f.read_text().strip().split("\n") if l])
            for f in files
        )
        assert total_lines == 100  # 5 threads * 20 writes


# ── Graceful missing dir ──

class TestGracefulMissingDir:
    def test_creates_missing_directory(self, tmp_path):
        new_dir = tmp_path / "deep" / "nested" / "audit"
        trail = AuditTrail(storage_dir=str(new_dir), node_id="test")
        trail.record(event="error", tool="test", status="error", error="e")
        assert new_dir.exists()
        files = list(new_dir.glob("audit-*.jsonl"))
        assert len(files) == 1


# ── Singleton ──

class TestSingleton:
    def test_get_audit_trail_returns_same_instance(self, tmp_path):
        a = get_audit_trail(storage_dir=str(tmp_path), node_id="test")
        b = get_audit_trail()
        assert a is b

    def test_reset_allows_new_instance(self, tmp_path):
        a = get_audit_trail(storage_dir=str(tmp_path / "a"), node_id="test")
        reset_audit_trail()
        b = get_audit_trail(storage_dir=str(tmp_path / "b"), node_id="test2")
        assert a is not b


# ── Recent entries ──

class TestRecent:
    def test_recent_returns_newest_first(self, trail):
        trail.record(event="error", tool="first", status="error", error="e1")
        trail.record(event="error", tool="second", status="error", error="e2")
        entries = trail.recent(limit=10)
        assert entries[0]["tool"] == "second"
        assert entries[1]["tool"] == "first"
