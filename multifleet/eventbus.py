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


class FleetEventBus:
    """Publishes fleet events to the 3-surgeons EventBus or JSONL fallback.

    This is a producer only — it does not run a server or consume events.
    All methods are synchronous and safe to call from both sync and async code
    (no blocking I/O longer than the HTTP timeout).
    """

    def __init__(self, node_id: str):
        self._node_id = node_id
        self._eventbus_available: Optional[bool] = None  # None = unknown
        self._last_availability_check: float = 0.0
        self._stats = {
            "emitted": 0,
            "eventbus_delivered": 0,
            "jsonl_fallback": 0,
            "errors": 0,
        }

    # ── Core emit ──

    def emit(self, event_type: str, payload: dict, correlation_id: str = None) -> bool:
        """Emit a fleet.* event.

        Tries 3-surgeons EventBus first, falls back to JSONL.
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
                return True
        except Exception as exc:
            logger.debug("EventBus delivery failed: %s", exc)

        # Fallback to JSONL
        try:
            if self._write_jsonl(event):
                self._stats["jsonl_fallback"] += 1
                return True
        except Exception as exc:
            logger.warning("JSONL fallback also failed: %s", exc)

        self._stats["errors"] += 1
        return False

    # ── EventBus transport ──

    def _try_eventbus(self, event: dict) -> bool:
        """POST event to 3-surgeons EventBus. Returns True on 2xx."""
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

    # ── JSONL fallback ──

    def _write_jsonl(self, event: dict) -> bool:
        """Append one JSON line to the JSONL fallback file."""
        JSONL_FALLBACK.parent.mkdir(parents=True, exist_ok=True)
        with open(JSONL_FALLBACK, "a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
        return True

    # ── Convenience methods for common fleet events ──

    def peer_online(self, peer_id: str, sessions: int = 0):
        """Emit fleet.peer.online event."""
        self.emit("peer.online", {"peer_id": peer_id, "sessions": sessions})

    def peer_offline(self, peer_id: str, last_seen_s: float = 0):
        """Emit fleet.peer.offline event."""
        self.emit("peer.offline", {"peer_id": peer_id, "last_seen_s": last_seen_s})

    def message_sent(self, target: str, channel: str, msg_type: str, elapsed_s: float):
        """Emit fleet.message.sent event."""
        self.emit(
            "message.sent",
            {"target": target, "channel": channel, "type": msg_type, "elapsed_s": elapsed_s},
        )

    def message_received(self, from_node: str, msg_type: str):
        """Emit fleet.message.received event."""
        self.emit("message.received", {"from": from_node, "type": msg_type})

    def lease_changed(self, resource_id: str, action: str, holder: str):
        """Emit fleet.lease.acquired or fleet.lease.released."""
        self.emit(f"lease.{action}", {"resource_id": resource_id, "holder": holder})

    def health_degraded(self, peer_id: str, score: str, broken: list):
        """Emit fleet.health.degraded event."""
        self.emit("health.degraded", {"peer_id": peer_id, "score": score, "broken": broken})

    # ── Stats ──

    @property
    def stats(self) -> dict:
        """Return a copy of emission statistics."""
        return dict(self._stats)
