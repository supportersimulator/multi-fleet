"""Dashboard EventBus Consumer — maintains live fleet state from events.

Listens to the in-process EventBus for fleet.* events and maintains a live
state dict that the TUI (or any consumer) can read without HTTP round-trips.

This is the real-time complement to FleetDashboardData (which polls HTTP).
When the EventBus is co-located in the same process as the daemon, this
consumer provides sub-second state updates.
"""

import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger("fleet-nerve.dashboard-events")

# Ring sizes for recent items
MAX_RECENT_MESSAGES = 50
MAX_HEALTH_ALERTS = 20
MAX_LEASE_ACTIVITY = 30


class DashboardEventConsumer:
    """Consumes EventBus events and maintains a live dashboard state dict.

    Thread-safe for reads (state is replaced atomically via dict copy).
    Writes happen only from EventBus dispatch (single-threaded in asyncio loop).
    """

    def __init__(self, bus=None):
        self._bus = bus
        self._live_state = {
            "peers_online": {},
            "active_races": {},
            "recent_messages": deque(maxlen=MAX_RECENT_MESSAGES),
            "health_alerts": deque(maxlen=MAX_HEALTH_ALERTS),
            "lease_activity": deque(maxlen=MAX_LEASE_ACTIVITY),
            "last_event_at": 0.0,
            "events_processed": 0,
        }
        if bus:
            self._subscribe()

    def _subscribe(self) -> None:
        """Register handlers for all fleet.* event patterns."""
        self._bus.on("fleet.peer.*", self._on_peer_event)
        self._bus.on("fleet.message.*", self._on_message_event)
        self._bus.on("fleet.health.*", self._on_health_event)
        self._bus.on("fleet.lease.*", self._on_lease_event)
        self._bus.on("fleet.rebuttal.*", self._on_rebuttal_event)
        self._bus.on("fleet.race.*", self._on_race_event)
        logger.info("DashboardEventConsumer subscribed to fleet.* events")

    def attach(self, bus) -> None:
        """Attach to an EventBus after construction (for lazy init)."""
        self._bus = bus
        self._subscribe()

    # ── Event handlers ──

    def _on_peer_event(self, event: dict) -> None:
        """Handle fleet.peer.online / fleet.peer.offline events."""
        self._tick(event)
        payload = event.get("payload", {})
        peer_id = payload.get("peer_id", "?")
        event_type = event.get("type", "")

        if event_type.endswith(".online"):
            self._live_state["peers_online"][peer_id] = {
                "since": time.time(),
                "sessions": payload.get("sessions", 0),
            }
        elif event_type.endswith(".offline"):
            self._live_state["peers_online"].pop(peer_id, None)

    def _on_message_event(self, event: dict) -> None:
        """Handle fleet.message.sent / fleet.message.received events."""
        self._tick(event)
        payload = event.get("payload", {})
        entry = {
            "type": event.get("type", ""),
            "timestamp": time.time(),
            "target": payload.get("target", payload.get("from", "?")),
            "channel": payload.get("channel", "?"),
            "msg_type": payload.get("type", "?"),
        }
        self._live_state["recent_messages"].append(entry)

    def _on_health_event(self, event: dict) -> None:
        """Handle fleet.health.degraded events."""
        self._tick(event)
        payload = event.get("payload", {})
        alert = {
            "timestamp": time.time(),
            "peer_id": payload.get("peer_id", "?"),
            "score": payload.get("score", "?"),
            "broken": payload.get("broken", []),
        }
        self._live_state["health_alerts"].append(alert)

    def _on_lease_event(self, event: dict) -> None:
        """Handle fleet.lease.acquired / fleet.lease.released events."""
        self._tick(event)
        payload = event.get("payload", {})
        entry = {
            "type": event.get("type", ""),
            "timestamp": time.time(),
            "resource_id": payload.get("resource_id", "?"),
            "holder": payload.get("holder", "?"),
        }
        self._live_state["lease_activity"].append(entry)

    def _on_rebuttal_event(self, event: dict) -> None:
        """Handle fleet.rebuttal.* events (placeholder for future enrichment)."""
        self._tick(event)

    def _on_race_event(self, event: dict) -> None:
        """Handle fleet.race.* events — track active race state."""
        self._tick(event)
        payload = event.get("payload", {})
        race_id = payload.get("race_id", "?")
        event_type = event.get("type", "")

        if "started" in event_type or "created" in event_type:
            self._live_state["active_races"][race_id] = {
                "started_at": time.time(),
                "participants": payload.get("participants", []),
                "topic": payload.get("topic", "?"),
            }
        elif "completed" in event_type or "resolved" in event_type:
            self._live_state["active_races"].pop(race_id, None)

    def _tick(self, event: dict) -> None:
        """Update universal counters on every event."""
        self._live_state["last_event_at"] = time.time()
        self._live_state["events_processed"] += 1

    # ── Read API ──

    @property
    def state(self) -> dict:
        """Return a snapshot of live state (deques converted to lists for serialization)."""
        s = dict(self._live_state)
        s["recent_messages"] = list(s["recent_messages"])
        s["health_alerts"] = list(s["health_alerts"])
        s["lease_activity"] = list(s["lease_activity"])
        s["active_races"] = dict(s["active_races"])
        s["peers_online"] = dict(s["peers_online"])
        return s

    @property
    def peers_online(self) -> dict:
        """Just the online peers dict."""
        return dict(self._live_state["peers_online"])

    @property
    def events_processed(self) -> int:
        return self._live_state["events_processed"]
