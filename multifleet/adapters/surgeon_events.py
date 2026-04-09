"""3-Surgeons Event Adapter — convert surgeon phases to fleet events.

Bridges the 3-surgeon protocol into the fleet EventEnvelope system so
that surgeon phase changes, consensus results, and cross-exam outcomes
are visible to all fleet components (dashboard, overlay, Superset).

"Live surgeon visibility: 3-Surgeons phase change -> event bus -> Superset overlay update"
"""

import logging
import os
import time
from typing import Optional

from multifleet.event_schema import (
    EventEnvelope, EventType, EventSource, create_event,
    surgeon_phase_event,
)
from multifleet.event_emitter import emit

logger = logging.getLogger("fleet-nerve.adapter.surgeon-events")


class SurgeonEventsAdapter:
    """Converts 3-surgeon protocol state changes into fleet events.

    Usage:
        adapter = SurgeonEventsAdapter()
        # When surgeon phase changes:
        adapter.on_phase_change("triage", intensity="standard", disagreement=0.3)
        # When consensus reached:
        adapter.on_consensus(task_id="fix-auth", agreed=True, confidence=0.85)
        # Periodic state poll:
        adapter.poll_and_emit()
    """

    def __init__(self, node_id: str = ""):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._last_phase: str = ""
        self._last_poll: float = 0.0
        self._poll_interval_s: float = 10.0
        self._stats = {
            "phase_events": 0,
            "consensus_events": 0,
            "poll_events": 0,
            "errors": 0,
        }

    def on_phase_change(
        self,
        phase: str,
        intensity: str = "light",
        disagreement: float = 0.0,
        task_id: str = "",
    ) -> Optional[EventEnvelope]:
        """Emit an event when surgeon phase changes.

        Args:
            phase: New surgeon phase (triage/operate/review/close).
            intensity: Review intensity (light/standard/full).
            disagreement: Disagreement score between surgeons.
            task_id: Optional task ID context.
        """
        if phase == self._last_phase:
            return None  # Deduplicate

        self._last_phase = phase
        event = surgeon_phase_event(
            node_id=self._node_id,
            phase=phase,
            intensity=intensity,
            disagreement=disagreement,
        )
        if task_id:
            event.task_id = task_id

        try:
            emit(event)
            self._stats["phase_events"] += 1
            logger.info(
                "surgeon_event phase=%s intensity=%s disagreement=%.2f",
                phase, intensity, disagreement,
            )
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Surgeon event emit failed: %s", e)

        return event

    def on_consensus(
        self,
        task_id: str,
        agreed: bool,
        confidence: float = 0.0,
        dissent_count: int = 0,
        surgeons_consulted: list[str] = None,
    ) -> EventEnvelope:
        """Emit consensus reached/blocked event."""
        et = EventType.SURGEON_CONSENSUS_REACHED if agreed else EventType.SURGEON_CONSENSUS_BLOCKED
        event = create_event(
            event_type=et,
            source=EventSource.THREE_SURGEONS,
            node_id=self._node_id,
            payload={
                "agreed": agreed,
                "confidence": round(confidence, 3),
                "dissent_count": dissent_count,
                "surgeons_consulted": surgeons_consulted or [],
            },
            task_id=task_id,
        )
        try:
            emit(event)
            self._stats["consensus_events"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Consensus event emit failed: %s", e)

        return event

    def poll_and_emit(self) -> Optional[EventEnvelope]:
        """Poll surgeon adapter for state and emit if changed.

        Respects poll_interval_s to avoid spamming.
        """
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval_s:
            return None
        self._last_poll = now

        try:
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            if not adapter.is_available():
                return None

            state = adapter.get_state()
            phase = state.get("phase", "unknown")
            disagreement = state.get("disagreement_score", 0.0)
            intensity = state.get("intensity", "light")

            if phase != self._last_phase:
                self._stats["poll_events"] += 1
                return self.on_phase_change(phase, intensity, disagreement)
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Surgeon poll failed: %s", e)

        return None

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_adapter: Optional[SurgeonEventsAdapter] = None


def get_surgeon_events() -> SurgeonEventsAdapter:
    global _adapter
    if _adapter is None:
        _adapter = SurgeonEventsAdapter()
    return _adapter
