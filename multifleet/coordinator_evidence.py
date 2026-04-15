"""Coordinator Evidence Pipeline — feeds local findings into global chain.

Bridges local coordinator events (health changes, surgical outcomes,
task completions, pattern discoveries) into the EvidenceLedger hash chain.

All writes are best-effort: if the ledger is unavailable, events queue
locally and flush on reconnect. Zero silent failures.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.coordinator-evidence")

# Max queued events before oldest are dropped
MAX_QUEUE_SIZE = 500

# Event types this pipeline emits
EVENT_TYPES = (
    "coordinator_health_change",
    "coordinator_heartbeat",
    "coordinator_task_assigned",
    "coordinator_task_completed",
    "coordinator_task_failed",
    "coordinator_surgeon_outcome",
    "coordinator_surgeon_availability",
    "coordinator_self_heal",
    "coordinator_started",
    "coordinator_stopped",
    "coordinator_fleet_decision",
)


class EvidencePipeline:
    """Feeds local coordinator events into the global evidence chain.

    Uses the EvidenceLedger (SQLite hash chain) when available,
    queues events locally when ledger is unreachable.
    """

    def __init__(self, node_id: str, ledger=None):
        self.node_id = node_id
        self._ledger = ledger
        self._queue: list[dict] = []
        self._stats = {
            "recorded": 0,
            "queued": 0,
            "flushed": 0,
            "errors": 0,
        }
        self._init_ledger()

    def _init_ledger(self):
        """Try to connect to evidence ledger if not provided."""
        if self._ledger is not None:
            return
        try:
            from .evidence_ledger import EvidenceLedger
            self._ledger = EvidenceLedger()
            logger.info("Evidence pipeline connected to ledger")
        except Exception as e:
            logger.debug("Evidence ledger not available: %s", e)

    def record(self, event_type: str, subject: str, data: dict) -> Optional[dict]:
        """Record an event to the evidence ledger.

        Args:
            event_type: One of EVENT_TYPES
            subject: Human-readable description
            data: Event payload (must be JSON-serializable)

        Returns:
            Evidence entry dict if recorded, None if queued.
        """
        event = {
            "event_type": event_type,
            "node_id": self.node_id,
            "subject": subject,
            "data": data,
            "timestamp": time.time(),
        }

        if self._ledger is not None:
            try:
                entry = self._ledger.record(
                    event_type=event_type,
                    node_id=self.node_id,
                    subject=subject,
                    payload=data,
                )
                self._stats["recorded"] += 1
                return entry
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning("Evidence record failed, queuing: %s", e)

        # Queue for later
        self._queue.append(event)
        if len(self._queue) > MAX_QUEUE_SIZE:
            dropped = len(self._queue) - MAX_QUEUE_SIZE
            self._queue = self._queue[-MAX_QUEUE_SIZE:]
            logger.warning("Evidence queue overflow, dropped %d oldest events", dropped)
        self._stats["queued"] += 1
        return None

    def flush_queue(self) -> int:
        """Attempt to flush queued events to the ledger.

        Returns number of events flushed.
        """
        if not self._queue:
            return 0
        if self._ledger is None:
            self._init_ledger()
            if self._ledger is None:
                return 0

        flushed = 0
        remaining = []
        for event in self._queue:
            try:
                self._ledger.record(
                    event_type=event["event_type"],
                    node_id=event["node_id"],
                    subject=event["subject"],
                    payload=event["data"],
                )
                flushed += 1
            except Exception as e:
                remaining.append(event)
                logger.debug("Flush failed for event: %s", e)

        self._queue = remaining
        self._stats["flushed"] += flushed
        if flushed:
            logger.info("Flushed %d queued evidence events (%d remaining)", flushed, len(remaining))
        return flushed

    # ── Convenience methods for common events ──

    def record_health_change(self, service: str, healthy: bool, details: dict = None) -> Optional[dict]:
        """Record a health state change."""
        return self.record(
            "coordinator_health_change",
            f"{service} {'recovered' if healthy else 'degraded'} on {self.node_id}",
            {"service": service, "healthy": healthy, **(details or {})},
        )

    def record_heartbeat(self, health_snapshot: dict) -> Optional[dict]:
        """Record a heartbeat event."""
        return self.record(
            "coordinator_heartbeat",
            f"Heartbeat from {self.node_id}",
            health_snapshot,
        )

    def record_task_assigned(self, task_id: str, origin: str, payload: dict) -> Optional[dict]:
        """Record task assignment."""
        return self.record(
            "coordinator_task_assigned",
            f"Task {task_id[:8]} assigned to {self.node_id} from {origin}",
            {"task_id": task_id, "origin": origin, "payload_keys": list(payload.keys())},
        )

    def record_task_completed(self, task_id: str, duration_ms: float) -> Optional[dict]:
        """Record task completion."""
        return self.record(
            "coordinator_task_completed",
            f"Task {task_id[:8]} completed on {self.node_id}",
            {"task_id": task_id, "duration_ms": round(duration_ms, 1)},
        )

    def record_task_failed(self, task_id: str, error: str) -> Optional[dict]:
        """Record task failure."""
        return self.record(
            "coordinator_task_failed",
            f"Task {task_id[:8]} failed on {self.node_id}: {error[:100]}",
            {"task_id": task_id, "error": error},
        )

    def record_surgeon_outcome(self, outcome_dict: dict) -> Optional[dict]:
        """Record a surgical outcome."""
        return self.record(
            "coordinator_surgeon_outcome",
            f"Surgery {outcome_dict.get('operation', '?')} on {self.node_id}",
            outcome_dict,
        )

    def record_surgeon_availability(self, availability: dict) -> Optional[dict]:
        """Record surgeon availability change."""
        available = [k for k, v in availability.items() if v.get("available")]
        return self.record(
            "coordinator_surgeon_availability",
            f"Surgeons on {self.node_id}: {', '.join(available) or 'none'}",
            availability,
        )

    def record_fleet_decision(self, decision: dict) -> Optional[dict]:
        """Record receipt of a fleet decision."""
        return self.record(
            "coordinator_fleet_decision",
            f"Fleet decision received on {self.node_id}: {decision.get('decision', '?')}",
            decision,
        )

    def record_self_heal(self, action: str, target: str, success: bool) -> Optional[dict]:
        """Record a self-healing action."""
        return self.record(
            "coordinator_self_heal",
            f"Self-heal {action} on {target} ({'ok' if success else 'failed'})",
            {"action": action, "target": target, "success": success},
        )

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    @property
    def stats(self) -> dict:
        return {**self._stats, "queue_size": len(self._queue)}
