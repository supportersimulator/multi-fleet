"""Event Emitter — bridge between pipeline components and EventBus.

Components call emit() → events flow to:
1. EventBus WebSocket (port 8031) for VS Code panels
2. NATS pub/sub for cross-node distribution
3. Local JSONL log for replay/debugging

This is the glue that makes the "shared bloodstream" actually flow.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from multifleet.event_schema import EventEnvelope, EventType, EventSource, create_event

logger = logging.getLogger("fleet-nerve.event-emitter")

# Default ports
EVENTBUS_PORT = int(os.environ.get("EVENTBUS_PORT", "8031"))
DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))


class EventEmitter:
    """Emits EventEnvelope events to all available sinks.

    Sinks (tried in order, failures are non-blocking):
    1. EventBus WebSocket server (port 8031)
    2. NATS via daemon HTTP endpoint (port 8855)
    3. Local JSONL event log
    """

    def __init__(
        self,
        node_id: str = "",
        event_log_path: Optional[str] = None,
        eventbus_port: int = EVENTBUS_PORT,
        daemon_port: int = DAEMON_PORT,
    ):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._eventbus_port = eventbus_port
        self._daemon_port = daemon_port
        self._log_path = Path(
            event_log_path
            or str(Path.home() / ".fleet-nerve" / "events.jsonl")
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stats = {"emitted": 0, "eventbus_ok": 0, "eventbus_fail": 0, "logged": 0}

    def emit(self, event: EventEnvelope) -> dict:
        """Emit an event to all available sinks. Non-blocking, best-effort."""
        if not event.node_id:
            event.node_id = self._node_id

        results = {}

        # Sink 1: EventBus (WebSocket)
        results["eventbus"] = self._emit_to_eventbus(event)

        # Sink 2: Local JSONL log (always succeeds)
        results["log"] = self._emit_to_log(event)

        self._stats["emitted"] += 1
        logger.debug("event_emitted type=%s id=%s", event.type, event.id)
        return results

    def emit_typed(
        self,
        event_type: EventType,
        source: EventSource,
        payload: dict = None,
        task_id: str = None,
    ) -> dict:
        """Convenience: create and emit an event in one call."""
        event = create_event(
            event_type=event_type,
            source=source,
            node_id=self._node_id,
            payload=payload or {},
            task_id=task_id,
        )
        return self.emit(event)

    def _emit_to_eventbus(self, event: EventEnvelope) -> bool:
        """Try to publish event to EventBus WebSocket server."""
        try:
            url = f"http://127.0.0.1:{self._eventbus_port}/publish"
            data = json.dumps({
                "topic": event.type,
                "data": event.to_dict(),
            }).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
            self._stats["eventbus_ok"] += 1
            return True
        except Exception:
            self._stats["eventbus_fail"] += 1
            return False

    def _emit_to_log(self, event: EventEnvelope) -> bool:
        """Append event to local JSONL log."""
        try:
            with open(self._log_path, "a") as f:
                f.write(event.to_json() + "\n")
            self._stats["logged"] += 1
            return True
        except Exception as e:
            logger.warning("Event log write failed: %s", e)
            return False

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton for convenience
_default_emitter: Optional[EventEmitter] = None


def get_emitter() -> EventEmitter:
    """Get or create the default event emitter."""
    global _default_emitter
    if _default_emitter is None:
        _default_emitter = EventEmitter()
    return _default_emitter


def emit(event: EventEnvelope) -> dict:
    """Emit an event using the default emitter."""
    return get_emitter().emit(event)


def emit_typed(event_type: EventType, source: EventSource, payload: dict = None, task_id: str = None) -> dict:
    """Create and emit an event using the default emitter."""
    return get_emitter().emit_typed(event_type, source, payload, task_id)
