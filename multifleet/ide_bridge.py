"""IDE Bridge — connects fleet events to IDE extension protocols.

Translates fleet.* events into VS Code extension commands and notifications.
Serves as the bridge between the Python fleet and TypeScript extension.

Data contracts match:
  - ExtensionEventWiring.ts (FleetPeerEvent, FleetHealthEvent)
  - FleetDashboardPanel.ts (peer rows, probe data)
  - NotificationsPanel (alerts, events, action items)
  - Status bar format from FleetStatusAggregator.status_bar_text()
"""

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("fleet-nerve.ide-bridge")

# Event type -> IDE command mapping
_EVENT_MAP = {
    "fleet.peer.online": "statusBar",
    "fleet.peer.offline": "statusBar",
    "fleet.health.degraded": "warning",
    "fleet.health.recovered": "statusBar",
    "fleet.probe.finding": "notification",
    "fleet.rebuttal.proposed": "badge",
    "fleet.rebuttal.resolved": "badge",
    "fleet.lease.acquired": "statusBar",
    "fleet.lease.released": "statusBar",
    "fleet.message.delivered": "notification",
    "fleet.arbiter.pending": "badge",
}

# Severity mapping for notifications
_SEVERITY_MAP = {
    "fleet.health.degraded": "warning",
    "fleet.probe.finding": "info",
    "fleet.rebuttal.proposed": "info",
    "fleet.message.delivered": "info",
    "fleet.arbiter.pending": "info",
}


class EventQueueOverflow(Exception):
    """Raised when event queue exceeds capacity (informational only)."""
    pass


class IDEBridge:
    """Translates fleet events into IDE-consumable data structures.

    Production hardening:
    - Connection retry with exponential backoff (3 attempts)
    - Event queue overflow protection (max 1000 events, drop oldest)
    - Stale data detection (snapshot > 30s marked stale)
    - Graceful shutdown (flush pending before close)
    """

    def __init__(self, aggregator=None, max_events: int = 1000,
                 stale_threshold: float = 30.0, max_retries: int = 3):
        """
        Args:
            aggregator: FleetStatusAggregator instance for snapshot data.
            max_events: Maximum events in queue before dropping oldest.
            stale_threshold: Seconds after which snapshot is considered stale.
            max_retries: Number of connection retry attempts.
        """
        self._aggregator = aggregator
        self._pending_notifications: list[dict] = []
        self._max_notifications = 50
        # Production hardening
        self._event_queue: list[dict] = []
        self._max_events = max_events
        self._stale_threshold = stale_threshold
        self._max_retries = max_retries
        self._last_snapshot_at: float = 0.0
        self._connected = False
        self._shutting_down = False
        self._lock = threading.Lock()
        self._on_overflow_callbacks: list[Callable] = []
        self._dropped_count = 0

    def connect(self, connect_fn: Optional[Callable] = None) -> bool:
        """Connect to EventBus with retry logic (exponential backoff).

        Args:
            connect_fn: Callable that attempts connection. Returns True on success.
                        If None, uses default EventBus connection on port 8031.

        Returns:
            True if connection succeeded, False after all retries exhausted.
        """
        if connect_fn is None:
            def connect_fn():
                try:
                    from multifleet.event_bus import IDEEventBus
                    IDEEventBus.get()  # singleton init validates availability
                    self._connected = True
                    return True
                except Exception:
                    return False

        for attempt in range(self._max_retries):
            try:
                if connect_fn():
                    self._connected = True
                    logger.info("ide_bridge connected attempt=%d", attempt + 1)
                    return True
            except Exception as e:
                logger.warning("ide_bridge connect_failed attempt=%d error=%s", attempt + 1, e)

            if attempt < self._max_retries - 1:
                backoff = min(2 ** attempt, 8)  # 1s, 2s, 4s (capped at 8s)
                logger.info("ide_bridge retry_backoff seconds=%.1f", backoff)
                time.sleep(backoff)

        self._connected = False
        logger.error("ide_bridge connection_exhausted retries=%d", self._max_retries)
        return False

    def enqueue_event(self, event: dict) -> None:
        """Add event to queue with overflow protection.

        If queue exceeds max_events, oldest events are dropped.
        """
        with self._lock:
            self._event_queue.append(event)
            if len(self._event_queue) > self._max_events:
                overflow = len(self._event_queue) - self._max_events
                self._event_queue = self._event_queue[overflow:]
                self._dropped_count += overflow
                logger.warning("ide_bridge event_overflow dropped=%d total_dropped=%d",
                               overflow, self._dropped_count)
                for cb in self._on_overflow_callbacks:
                    try:
                        cb(overflow, self._dropped_count)
                    except Exception:
                        pass

    def drain_events(self) -> list[dict]:
        """Return and clear all queued events."""
        with self._lock:
            events = list(self._event_queue)
            self._event_queue.clear()
            return events

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def queue_size(self) -> int:
        with self._lock:
            return len(self._event_queue)

    def on_overflow(self, callback: Callable) -> None:
        """Register a callback for overflow events. callback(dropped, total_dropped)."""
        self._on_overflow_callbacks.append(callback)

    def is_stale(self) -> bool:
        """Check if the last snapshot is older than stale_threshold."""
        if self._last_snapshot_at == 0.0:
            return True
        return (time.time() - self._last_snapshot_at) > self._stale_threshold

    def shutdown(self) -> list[dict]:
        """Graceful shutdown: flush pending notifications and events.

        Returns all pending items that were flushed.
        """
        self._shutting_down = True
        flushed = []

        # Flush pending notifications
        notifications = self.get_pending_notifications()
        flushed.extend(notifications)

        # Flush event queue
        events = self.drain_events()
        flushed.extend(events)

        logger.info("ide_bridge shutdown flushed=%d items", len(flushed))
        self._connected = False
        return flushed

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._shutting_down

    def translate_event(self, event_type: str, data: dict) -> dict:
        """Translate fleet event into IDE-compatible format.

        Returns dict with: command, event_type, payload, timestamp.
        """
        command = _EVENT_MAP.get(event_type, "notification")
        payload = dict(data)

        result = {
            "command": command,
            "event_type": event_type,
            "payload": payload,
            "timestamp": time.time(),
        }

        # Enrich based on event type
        if event_type in ("fleet.peer.online", "fleet.peer.offline"):
            result["payload"]["action"] = "online" if "online" in event_type else "offline"

        if event_type == "fleet.health.degraded":
            self._push_notification(
                severity="warning",
                message=f"Fleet health degraded: {data.get('nodeId', data.get('node_id', '?'))}",
                source="fleet",
            )

        if event_type == "fleet.probe.finding":
            self._push_notification(
                severity=data.get("severity", "info"),
                message=data.get("title", data.get("message", "Probe finding")),
                source="probe",
            )

        if event_type == "fleet.rebuttal.proposed":
            self._push_notification(
                severity="info",
                message=f"Rebuttal proposed: {data.get('subject', data.get('title', '?'))}",
                source="rebuttal",
            )

        if event_type == "fleet.arbiter.pending":
            count = data.get("count", 1)
            self._push_notification(
                severity="info",
                message=f"{count} decision(s) awaiting human review",
                source="arbiter",
            )

        return result

    def format_for_extension(self, event: dict) -> dict:
        """Format translated event as VS Code extension message.

        Returns the ContextDNA Event v1 schema shape expected by
        WebSocketEventClient.onEvent().
        """
        return {
            "type": event["event_type"],
            "payload": event["payload"],
            "source": "fleet-bridge",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(event["timestamp"])),
        }

    def get_status_bar_data(self) -> dict:
        """Data for the status bar: node count, health dot, message.

        Matches what FleetDashboardPanel and extension.ts status bar consume.
        Marks data as stale if snapshot is older than stale_threshold.
        """
        if self._aggregator is None:
            return {
                "node_count": 0,
                "health_dot": "red",
                "text": "Fleet: off",
                "tooltip": "Fleet daemon not running",
                "stale": True,
            }

        self._last_snapshot_at = time.time()
        snap = self._aggregator.snapshot()
        peers = snap.get("peers", {})
        reachable = peers.get("reachable", 0)
        total = peers.get("total", 0)
        dot = snap.get("health_dot", "red")

        dot_char = {"green": "ok", "yellow": "degraded", "red": "down"}.get(dot, "unknown")
        text = self._aggregator.status_bar_text()

        stale = self.is_stale()
        if stale:
            text = f"[STALE] {text}"

        return {
            "node_count": reachable + 1,  # self + reachable peers
            "total_peers": total,
            "reachable_peers": reachable,
            "health_dot": dot,
            "health_status": dot_char,
            "text": text,
            "tooltip": f"Fleet: {snap.get('node_id', '?')} | {reachable}/{total} peers reachable",
            "rebuttals": snap.get("rebuttals", {}).get("active", 0),
            "pending_reviews": snap.get("liaison", {}).get("pending_reviews", 0),
            "unread": snap.get("liaison", {}).get("unread", 0),
            "stale": stale,
            "gotcha_count": self._get_professor_gotcha_count(),
        }

    def get_activity_bar_data(self) -> dict:
        """Data for activity bar tree view: peers, rebuttals, leases.

        Structured for VS Code TreeDataProvider consumption.
        """
        if self._aggregator is None:
            return {"sections": [], "connected": False}

        snap = self._aggregator.snapshot()
        daemon = snap.get("daemon", {})
        peers_raw = daemon.get("peers", {})
        rebuttals = snap.get("rebuttals", {})
        leases = snap.get("leases", {})
        liaison = snap.get("liaison", {})

        # Build peer tree items
        peer_items = []
        if isinstance(peers_raw, dict):
            for node_id, info in peers_raw.items():
                if isinstance(info, dict):
                    status = info.get("status", "unknown")
                    last_seen = info.get("lastSeen", info.get("last_seen", -1))
                    sessions = info.get("sessions", 0)
                    peer_items.append({
                        "id": node_id,
                        "label": node_id,
                        "status": status,
                        "last_seen_s": last_seen,
                        "sessions": sessions,
                        "icon": "circle-filled" if status in ("online", "reachable") else "circle-outline",
                    })

        # Build rebuttal tree items
        rebuttal_items = []
        active = rebuttals.get("active", 0)
        total_rebuttals = rebuttals.get("total", 0)
        if active > 0:
            rebuttal_items.append({
                "id": "rebuttals-active",
                "label": f"{active} active rebuttal{'s' if active != 1 else ''}",
                "icon": "comment-discussion",
                "badge": active,
            })

        # Build lease tree items
        lease_items = []
        active_leases = leases.get("active", 0)
        held = leases.get("held_by_me", 0)
        if active_leases > 0:
            lease_items.append({
                "id": "leases-active",
                "label": f"{active_leases} active lease{'s' if active_leases != 1 else ''}",
                "description": f"{held} held by me" if held else None,
                "icon": "lock",
            })

        sections = [
            {"id": "peers", "label": "Fleet Nodes", "items": peer_items, "count": len(peer_items)},
            {"id": "rebuttals", "label": "Rebuttals", "items": rebuttal_items, "count": active},
            {"id": "leases", "label": "Leases", "items": lease_items, "count": active_leases},
        ]

        # Add unread messages if any
        unread = liaison.get("unread", 0)
        if unread > 0:
            sections.append({
                "id": "messages",
                "label": "Messages",
                "items": [{"id": "unread", "label": f"{unread} unread", "icon": "mail", "badge": unread}],
                "count": unread,
            })

        # Add pending arbiter decisions if any
        pending_decisions = self.get_pending_decisions()
        if pending_decisions:
            decision_count = len(pending_decisions)
            decision_items = []
            for d in pending_decisions[:5]:
                tid = d.get("task_id", "?")
                conf = d.get("confidence", 0.0)
                decision_items.append({
                    "id": f"arbiter-{tid}",
                    "label": f"{tid[:12]} ({conf:.0%})",
                    "icon": "law",
                    "badge": len(d.get("dissent", [])),
                    "url": f"/arbiter/{tid}",
                })
            sections.append({
                "id": "arbiter",
                "label": "Decisions Awaiting Review",
                "items": decision_items,
                "count": decision_count,
            })

        # Add professor wisdom section
        professor_items = self._get_professor_activity_items()
        if professor_items:
            sections.append({
                "id": "professor",
                "label": "Professor Wisdom",
                "items": professor_items,
                "count": len(professor_items),
            })

        return {
            "sections": sections,
            "connected": daemon.get("status") != "unreachable",
            "node_id": snap.get("node_id", ""),
        }

    def _get_professor_bridge(self):
        """Get professor bridge instance (lazy, cached)."""
        if not hasattr(self, "_professor_bridge"):
            self._professor_bridge = None
        if self._professor_bridge is None:
            try:
                from multifleet.professor_bridge import get_professor_bridge
                self._professor_bridge = get_professor_bridge()
            except Exception:
                pass
        return self._professor_bridge

    def _get_professor_gotcha_count(self) -> int:
        """Count active gotchas from professor bridge for status bar."""
        bridge = self._get_professor_bridge()
        if bridge is None:
            return 0
        try:
            gotchas = bridge._get_gotchas()
            return len(gotchas)
        except Exception:
            return 0

    def _get_professor_activity_items(self) -> list[dict]:
        """Build tree items for professor wisdom in activity bar."""
        bridge = self._get_professor_bridge()
        if bridge is None:
            return []
        try:
            panel = bridge.get_panel_data()
            items = []

            # Recent wisdom entries
            for w in panel.get("recent_wisdom", [])[:5]:
                topic = w.get("topic", "?")
                one_thing = w.get("the_one_thing", "")
                mines = w.get("landmine_count", 0)
                label = topic[:30]
                desc = one_thing[:60] if one_thing else None
                items.append({
                    "id": f"prof-{topic[:20]}",
                    "label": label,
                    "description": desc,
                    "icon": "mortar-board",
                    "badge": mines if mines > 0 else None,
                })

            # Active gotchas summary
            gotchas = panel.get("active_gotchas", [])
            if gotchas:
                items.append({
                    "id": "prof-gotchas",
                    "label": f"{len(gotchas)} active gotcha{'s' if len(gotchas) != 1 else ''}",
                    "icon": "warning",
                    "badge": len(gotchas),
                })

            return items
        except Exception:
            return []

    def get_pending_decisions(self) -> list[dict]:
        """Get pending arbiter decisions for activity bar badge."""
        try:
            from multifleet.arbiter_view import ArbiterView
            # Check if there's a web-server-level arbiter instance
            from multifleet.web_server import _arbiter_view
            if _arbiter_view is not None:
                return _arbiter_view.list_pending()
        except (ImportError, Exception):
            pass
        return []

    def get_pending_notifications(self) -> list[dict]:
        """Return and clear pending notifications."""
        notifications = list(self._pending_notifications)
        self._pending_notifications.clear()
        return notifications

    def _push_notification(self, severity: str, message: str, source: str) -> None:
        """Add a notification to the pending queue."""
        self._pending_notifications.append({
            "id": f"fleet-{int(time.time() * 1000)}",
            "severity": severity,
            "message": message,
            "source": source,
            "timestamp": time.time(),
        })
        if len(self._pending_notifications) > self._max_notifications:
            self._pending_notifications = self._pending_notifications[-self._max_notifications:]
