"""Fleet EventBus Adapter — bridges fleet daemon events to IDE event system.

Publishes fleet.* namespace events to the 3-surgeons EventBus (WebSocket port 8031)
or falls back to JSONL file when EventBus is unavailable.
"""

import json
import logging
import os
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.eventbus")

EVENTBUS_URL = os.environ.get("FLEET_EVENTBUS_URL", "http://127.0.0.1:8031/events")
EVENTBUS_HEALTH_URL = os.environ.get(
    "FLEET_EVENTBUS_HEALTH_URL", "http://127.0.0.1:8031/health"
)
JSONL_FALLBACK = Path(
    os.environ.get("FLEET_EVENTBUS_JSONL", str(Path.home() / ".fleet-nerve" / "events.jsonl"))
)

# Cache the EventBus availability check for this many seconds
_AVAILABILITY_CACHE_TTL_S = 30.0

# JSONL size management
MAX_JSONL_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_JSONL_ROTATIONS = 2  # keep .jsonl.1 and .jsonl.2

# Events that should be broadcast to all fleet nodes
_BROADCAST_EVENT_TYPES = frozenset({
    "peer.online", "peer.offline",
    "lease.acquired", "lease.released",
    "health.degraded",
    "channel.broken", "channel.healing", "channel.restored",
})


class FleetEventBus:
    """Publishes fleet events to the 3-surgeons EventBus or JSONL fallback.

    This is a producer only — it does not run a server or consume events.
    All methods are synchronous and safe to call from both sync and async code
    (no blocking I/O longer than the HTTP timeout).
    """

    def __init__(self, node_id: str, daemon_port: int = 8855):
        self._node_id = node_id
        self._daemon_port = daemon_port
        self._eventbus_available: Optional[bool] = None  # None = unknown
        self._last_availability_check: float = 0.0
        self._core_bus = None  # In-process EventBus (set by daemon when co-located)
        self._stats = {
            "emitted": 0,
            "eventbus_delivered": 0,
            "jsonl_fallback": 0,
            "broadcast_sent": 0,
            "errors": 0,
        }

    def set_core_bus(self, core_bus) -> None:
        """Inject the in-process EventBus for direct emit (no HTTP overhead).

        Called by the daemon when EventBusServer is co-located in the same process.
        """
        self._core_bus = core_bus
        logger.info("FleetEventBus: using in-process EventBus (direct emit)")

    # ── Core emit ──

    def emit(self, event_type: str, payload: dict, correlation_id: str = None,
             broadcast: bool = False) -> bool:
        """Emit a fleet.* event.

        Tries 3-surgeons EventBus first, falls back to JSONL.
        If broadcast=True (or event_type is in _BROADCAST_EVENT_TYPES),
        also sends the event to all fleet nodes via the daemon broadcast endpoint.

        Returns True if event was recorded somewhere.
        """
        event = {
            "id": str(uuid.uuid4()),
            "version": 1,
            "type": f"fleet.{event_type}",
            "source": f"fleet-nerve:{self._node_id}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "payload": payload,
            "metadata": {
                "node_id": self._node_id,
                "correlation_id": correlation_id or str(uuid.uuid4()),
            },
        }

        self._stats["emitted"] += 1

        # Try EventBus first
        try:
            if self._try_eventbus(event):
                self._stats["eventbus_delivered"] += 1
                recorded = True
            else:
                recorded = False
        except Exception as exc:
            logger.debug("EventBus delivery failed: %s", exc)
            recorded = False

        # Fallback to JSONL
        if not recorded:
            try:
                if self._write_jsonl(event):
                    self._stats["jsonl_fallback"] += 1
                    recorded = True
            except Exception as exc:
                logger.warning("JSONL fallback also failed: %s", exc)

        if not recorded:
            self._stats["errors"] += 1

        # Broadcast important events to fleet
        should_broadcast = broadcast or event_type in _BROADCAST_EVENT_TYPES
        if should_broadcast:
            self._broadcast_to_fleet(event)

        return recorded

    # ── EventBus transport ──

    def _try_eventbus(self, event: dict) -> bool:
        """Deliver event to EventBus. Prefers in-process bus, falls back to HTTP POST."""
        # Fast path: in-process EventBus (same daemon process, no HTTP)
        if self._core_bus is not None:
            try:
                self._core_bus.emit(
                    event_type=event["type"],
                    payload=event.get("payload", {}),
                    source=event.get("source", f"fleet-nerve:{self._node_id}"),
                    correlation_id=event.get("metadata", {}).get("correlation_id"),
                )
                return True
            except Exception as exc:
                logger.debug("In-process EventBus emit failed: %s", exc)
                # Fall through to HTTP

        # Slow path: HTTP POST to EventBus server
        if not self._is_eventbus_available():
            return False

        body = json.dumps(event).encode("utf-8")
        req = urllib.request.Request(
            EVENTBUS_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=2)
            return 200 <= resp.status < 300
        except Exception:
            # Mark unavailable so we don't probe every call
            self._eventbus_available = False
            self._last_availability_check = time.time()
            return False

    def _is_eventbus_available(self) -> bool:
        """Check cached availability, re-probe if stale."""
        now = time.time()
        if (
            self._eventbus_available is not None
            and now - self._last_availability_check < _AVAILABILITY_CACHE_TTL_S
        ):
            return self._eventbus_available

        self._eventbus_available = self.check_eventbus()
        self._last_availability_check = now
        return self._eventbus_available

    def check_eventbus(self) -> bool:
        """Probe if 3-surgeons EventBus is reachable (GET /health)."""
        try:
            req = urllib.request.Request(EVENTBUS_HEALTH_URL, method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            return 200 <= resp.status < 300
        except Exception:
            return False

    # ── Fleet broadcast ──

    def _broadcast_to_fleet(self, event: dict):
        """Send event to all fleet nodes via daemon broadcast endpoint.

        Best-effort: failure is logged but does not affect the return value of emit().
        Only used for important cross-node events (peer status, leases, health).
        """
        payload = json.dumps({
            "type": "event_broadcast",
            "payload": event,
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._daemon_port}/broadcast",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=3)
            if 200 <= resp.status < 300:
                self._stats["broadcast_sent"] += 1
                logger.debug("Broadcast event %s to fleet", event.get("type", "?"))
            else:
                logger.debug("Broadcast returned status %d", resp.status)
        except Exception as exc:
            logger.debug("Fleet broadcast failed (non-fatal): %s", exc)

    # ── JSONL fallback ──

    def _rotate_jsonl(self):
        """Rotate JSONL file if it exceeds MAX_JSONL_SIZE.

        Keeps up to MAX_JSONL_ROTATIONS rotated files (.jsonl.1, .jsonl.2).
        """
        try:
            if not JSONL_FALLBACK.exists():
                return
            size = JSONL_FALLBACK.stat().st_size
            if size < MAX_JSONL_SIZE:
                return
        except OSError:
            return

        # Shift existing rotations: .jsonl.2 -> delete, .jsonl.1 -> .jsonl.2, current -> .jsonl.1
        for i in range(MAX_JSONL_ROTATIONS, 0, -1):
            rotated = JSONL_FALLBACK.parent / f"{JSONL_FALLBACK.name}.{i}"
            if i == MAX_JSONL_ROTATIONS:
                # Delete the oldest
                try:
                    rotated.unlink(missing_ok=True)
                except OSError:
                    pass
            if i > 1:
                prev = JSONL_FALLBACK.parent / f"{JSONL_FALLBACK.name}.{i - 1}"
                if prev.exists():
                    try:
                        prev.rename(rotated)
                    except OSError:
                        pass

        # Move current to .jsonl.1
        first_rotation = JSONL_FALLBACK.parent / f"{JSONL_FALLBACK.name}.1"
        try:
            JSONL_FALLBACK.rename(first_rotation)
        except OSError:
            pass
        logger.info("Rotated JSONL fallback (was %d bytes)", size)

    def _write_jsonl(self, event: dict) -> bool:
        """Append one JSON line to the JSONL fallback file.

        Rotates the file if it exceeds MAX_JSONL_SIZE before writing.
        """
        JSONL_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_jsonl()
        with open(JSONL_FALLBACK, "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
        return True

    # ── Convenience methods for common fleet events ──

    def peer_online(self, peer_id: str, sessions: int = 0):
        """Emit fleet.peer.online event (broadcast to fleet)."""
        self.emit("peer.online", {"peer_id": peer_id, "sessions": sessions}, broadcast=True)

    def peer_offline(self, peer_id: str, last_seen_s: float = 0):
        """Emit fleet.peer.offline event (broadcast to fleet)."""
        self.emit("peer.offline", {"peer_id": peer_id, "last_seen_s": last_seen_s}, broadcast=True)

    def message_sent(self, target: str, channel: str, msg_type: str, elapsed_s: float):
        """Emit fleet.message.sent event (local only — high frequency)."""
        self.emit(
            "message.sent",
            {"target": target, "channel": channel, "type": msg_type, "elapsed_s": elapsed_s},
        )

    def message_received(self, from_node: str, msg_type: str):
        """Emit fleet.message.received event (local only — high frequency)."""
        self.emit("message.received", {"from": from_node, "type": msg_type})

    def lease_changed(self, resource_id: str, action: str, holder: str):
        """Emit fleet.lease.acquired or fleet.lease.released (broadcast to fleet)."""
        self.emit(f"lease.{action}", {"resource_id": resource_id, "holder": holder}, broadcast=True)

    def health_degraded(self, peer_id: str, score: str, broken: list):
        """Emit fleet.health.degraded event (broadcast to fleet)."""
        self.emit("health.degraded", {"peer_id": peer_id, "score": score, "broken": broken}, broadcast=True)

    def channel_broken(self, peer_id: str, score: str, broken: list, severity: str = "high"):
        """Emit fleet.channel.broken event (broadcast to fleet)."""
        self.emit("channel.broken", {
            "peer_id": peer_id, "score": score, "broken": broken, "severity": severity,
        }, broadcast=True)

    def channel_healing(self, peer_id: str, score: str, escalation_level: int = 1):
        """Emit fleet.channel.healing event (broadcast to fleet)."""
        self.emit("channel.healing", {
            "peer_id": peer_id, "score": score, "escalation_level": escalation_level,
        }, broadcast=True)

    def channel_restored(self, peer_id: str, score: str):
        """Emit fleet.channel.restored event (broadcast to fleet)."""
        self.emit("channel.restored", {"peer_id": peer_id, "score": score}, broadcast=True)

    # ── Stats ──

    @property
    def stats(self) -> dict:
        """Return a copy of emission statistics."""
        return dict(self._stats)
