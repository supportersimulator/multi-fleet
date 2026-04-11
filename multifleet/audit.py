"""Structured audit trail for fleet-nerve operations.

Hybrid sync/async: hard failures write immediately, metrics buffer.
Append-only JSONL — one file per day for easy rotation.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fleet-nerve-audit")

SYNC_EVENTS = frozenset({"error", "security", "repair"})
BUFFER_LIMIT = 50
FLUSH_INTERVAL_S = 0.1  # 100ms


@dataclass
class AuditEntry:
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event: str = "tool_call"
    node_id: str = ""
    tool: str = ""
    target: str = ""
    status: str = "ok"
    latency_ms: float = 0.0
    error: Optional[str] = None
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Strip None values for compact JSONL
        return {k: v for k, v in d.items() if v is not None}


class AuditTrail:
    """Append-only JSONL audit log with daily rotation and async buffering."""

    def __init__(self, storage_dir: Optional[str] = None, node_id: str = ""):
        self._dir = Path(storage_dir) if storage_dir else Path.home() / ".fleet-nerve"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._lock = threading.Lock()
        self._buffer: List[AuditEntry] = []
        self._timer: Optional[threading.Timer] = None
        self._error_counts: Dict[str, int] = {}
        self._closed = False
        atexit.register(self.flush)

    def _log_path(self) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._dir / f"audit-{date}.jsonl"

    def record(
        self,
        event: str,
        tool: str,
        target: str = "",
        status: str = "ok",
        latency_ms: float = 0.0,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        parent_id: Optional[str] = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            event=event,
            node_id=self._node_id,
            tool=tool,
            target=target,
            status=status,
            latency_ms=latency_ms,
            error=error,
            parent_id=parent_id,
            metadata=metadata,
        )

        # Track errors
        if status != "ok":
            key = f"{event}:{tool}:{status}"
            with self._lock:
                self._error_counts[key] = self._error_counts.get(key, 0) + 1

        # Sync write for critical events
        if event in SYNC_EVENTS:
            self._write_entry(entry)
            return entry

        # Async buffered write
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= BUFFER_LIMIT:
                self._flush_locked()
            elif self._timer is None and not self._closed:
                self._timer = threading.Timer(FLUSH_INTERVAL_S, self.flush)
                self._timer.daemon = True
                self._timer.start()

        return entry

    def flush(self) -> None:
        """Force-write all buffered entries to disk."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Flush buffer while lock is held."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._buffer:
            return
        entries = self._buffer[:]
        self._buffer.clear()
        # Write outside critical section would be ideal but keeping simple
        for entry in entries:
            self._write_entry(entry)

    def _write_entry(self, entry: AuditEntry) -> None:
        """Append a single JSON line to the daily audit file."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with open(self._log_path(), "a") as f:
                f.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.error("Audit write failed: %s", exc)

    def get_error_counts(self) -> Dict[str, int]:
        """Return current error counts by event:tool:status key."""
        with self._lock:
            return dict(self._error_counts)

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Read recent audit entries (newest first)."""
        self.flush()
        entries: List[Dict[str, Any]] = []
        for log_file in sorted(self._dir.glob("audit-*.jsonl"), reverse=True):
            try:
                lines = log_file.read_text().strip().split("\n")
            except OSError:
                continue
            for line in reversed(lines):
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
                if len(entries) >= limit:
                    return entries
        return entries

    def close(self) -> None:
        """Flush and mark closed (no more timers)."""
        self._closed = True
        self.flush()


# ── Singleton ──

_instance: Optional[AuditTrail] = None
_instance_lock = threading.Lock()


def get_audit_trail(storage_dir: Optional[str] = None, node_id: str = "") -> AuditTrail:
    """Get or create the singleton AuditTrail instance."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AuditTrail(storage_dir=storage_dir, node_id=node_id)
    return _instance


def reset_audit_trail() -> None:
    """Reset singleton (for testing)."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close()
        _instance = None
