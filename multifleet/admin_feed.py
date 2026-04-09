"""Fleet event feed for admin panel consumption.

Provides a structured event stream that the admin panel can consume
via SSE (Server-Sent Events) or polling endpoint.

Endpoints (wired into daemon HTTP server):
    GET /admin/feed          — recent events as JSON (polling)
    GET /admin/feed/sse      — Server-Sent Events stream (live)
    GET /admin/summary       — dashboard summary snapshot

Architecture:
    FleetEventBus.emit() -> AdminFeed.record_event()
    AdminFeed stores events in a fixed-size ring buffer.
    Daemon HTTP handler reads from AdminFeed on request.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class AdminEvent:
    """Single event in the admin feed."""

    id: str
    event_type: str
    timestamp: float
    source: str
    data: Dict[str, Any]
    node_id: str = ""
    severity: str = "info"  # info | warning | error | critical

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "source": self.source,
            "data": self.data,
            "node_id": self.node_id,
            "severity": self.severity,
        }

    def to_sse(self) -> str:
        """Format as a Server-Sent Events data line."""
        payload = json.dumps(self.to_dict(), separators=(",", ":"))
        return f"event: {self.event_type}\ndata: {payload}\nid: {self.id}\n\n"


# Severity mapping for known event types
_SEVERITY_MAP: Dict[str, str] = {
    "fleet.health.degraded": "warning",
    "fleet.channel.broken": "error",
    "fleet.channel.healing": "warning",
    "fleet.channel.restored": "info",
    "fleet.peer.offline": "warning",
    "fleet.peer.online": "info",
    "fleet.lease.acquired": "info",
    "fleet.lease.released": "info",
    "fleet.message.sent": "info",
    "fleet.message.received": "info",
    "probe.finding.raised": "warning",
    "surgeon.consensus.blocked": "error",
    "surgeon.consensus.reached": "info",
    "invariant.violated": "critical",
    "race.started": "info",
    "race.completed": "info",
}


class AdminFeed:
    """Collects and serves fleet events for admin panel display.

    Thread-safe ring buffer with subscriber support for SSE streaming.
    """

    def __init__(self, max_events: int = 1000):
        self._max_events = max_events
        self._events: deque[AdminEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[AdminEvent], None]] = []
        self._stats = {
            "total_recorded": 0,
            "total_served": 0,
            "subscribers": 0,
        }
        # Counters by event type for summary
        self._type_counts: Dict[str, int] = {}
        self._severity_counts: Dict[str, int] = {"info": 0, "warning": 0, "error": 0, "critical": 0}

    def record_event(
        self,
        event_type: str,
        data: Dict[str, Any],
        source: str = "",
        node_id: str = "",
        severity: str = "",
        event_id: str = "",
    ) -> AdminEvent:
        """Record a fleet event for admin consumption.

        Args:
            event_type: Dotted event type (e.g. "fleet.peer.online").
            data: Event payload dict.
            source: Source identifier (e.g. "fleet-nerve:mac2").
            node_id: Node that produced the event.
            severity: Override severity (auto-detected from event_type if empty).
            event_id: Optional pre-assigned event ID.

        Returns:
            The recorded AdminEvent.
        """
        if not severity:
            severity = _SEVERITY_MAP.get(event_type, "info")

        event = AdminEvent(
            id=event_id or str(uuid.uuid4())[:12],
            event_type=event_type,
            timestamp=time.time(),
            source=source,
            data=data,
            node_id=node_id,
            severity=severity,
        )

        with self._lock:
            self._events.append(event)
            self._stats["total_recorded"] += 1
            self._type_counts[event_type] = self._type_counts.get(event_type, 0) + 1
            self._severity_counts[severity] = self._severity_counts.get(severity, 0) + 1

        # Notify subscribers (best-effort, non-blocking)
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception:
                pass

        return event

    def record_from_envelope(self, envelope: Dict[str, Any]) -> AdminEvent:
        """Record an event from a FleetEventBus envelope dict.

        Extracts type, payload, source, and node_id from the standard envelope
        format produced by FleetEventBus.emit().
        """
        return self.record_event(
            event_type=envelope.get("type", "unknown"),
            data=envelope.get("payload", {}),
            source=envelope.get("source", ""),
            node_id=envelope.get("metadata", {}).get("node_id", ""),
            event_id=envelope.get("id", ""),
        )

    def get_recent(
        self,
        limit: int = 50,
        event_types: Optional[List[str]] = None,
        severity: Optional[str] = None,
        since: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent events, optionally filtered.

        Args:
            limit: Max events to return.
            event_types: Filter to these event types only.
            severity: Filter to this severity level only.
            since: Only events after this Unix timestamp.

        Returns:
            List of event dicts, newest first.
        """
        with self._lock:
            events = list(self._events)

        # Apply filters
        if event_types:
            type_set = set(event_types)
            events = [e for e in events if e.event_type in type_set]
        if severity:
            events = [e for e in events if e.severity == severity]
        if since:
            events = [e for e in events if e.timestamp >= since]

        # Newest first, capped
        events = events[-limit:]
        events.reverse()

        self._stats["total_served"] += len(events)
        return [e.to_dict() for e in events]

    def get_summary(self) -> Dict[str, Any]:
        """Dashboard summary for admin panel.

        Returns node count, event stats, severity breakdown, and recent
        high-priority events.
        """
        with self._lock:
            events = list(self._events)
            total = self._stats["total_recorded"]
            type_counts = dict(self._type_counts)
            severity_counts = dict(self._severity_counts)

        # Unique nodes seen
        nodes = set()
        for e in events:
            if e.node_id:
                nodes.add(e.node_id)

        # Recent warnings/errors (last 10)
        important = [
            e.to_dict() for e in reversed(events)
            if e.severity in ("warning", "error", "critical")
        ][:10]

        # Events in last 5 minutes
        cutoff = time.time() - 300
        recent_count = sum(1 for e in events if e.timestamp >= cutoff)

        # Top event types (last hour)
        hour_cutoff = time.time() - 3600
        hourly_types: Dict[str, int] = {}
        for e in events:
            if e.timestamp >= hour_cutoff:
                hourly_types[e.event_type] = hourly_types.get(e.event_type, 0) + 1

        return {
            "node_count": len(nodes),
            "nodes": sorted(nodes),
            "total_events": total,
            "buffer_size": len(events),
            "buffer_max": self._max_events,
            "events_last_5min": recent_count,
            "severity_counts": severity_counts,
            "type_counts": type_counts,
            "top_types_hourly": dict(sorted(hourly_types.items(), key=lambda x: -x[1])[:10]),
            "recent_important": important,
            "subscribers": len(self._subscribers),
        }

    def subscribe(self, callback: Callable[[AdminEvent], None]) -> Callable[[], None]:
        """Subscribe to live event stream.

        Args:
            callback: Called with each new AdminEvent.

        Returns:
            Unsubscribe function.
        """
        self._subscribers.append(callback)
        self._stats["subscribers"] = len(self._subscribers)

        def unsubscribe():
            try:
                self._subscribers.remove(callback)
                self._stats["subscribers"] = len(self._subscribers)
            except ValueError:
                pass

        return unsubscribe

    @property
    def stats(self) -> Dict[str, Any]:
        """Return feed statistics."""
        return dict(self._stats)


# ── Module-level singleton ───────────────────────────────────────────────

_feed: Optional[AdminFeed] = None


def get_admin_feed() -> AdminFeed:
    """Get or create the global AdminFeed singleton."""
    global _feed
    if _feed is None:
        _feed = AdminFeed()
    return _feed
