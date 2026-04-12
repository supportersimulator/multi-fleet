"""Adapter between superpowers workflow phases and fleet coordination.

Prevents fleet races during brainstorming/planning phases.
Coordinates fleet execution when workflow enters executing phase.

Maps superpowers phases (signal, brainstorming, writing-plans, executing-plans, done)
to fleet dispatch decisions. Also understands SupervisorPhase from supervisor.py
for internal fleet-side phase tracking.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.superpowers-adapter")

# Node ID for event emission — defaults to hostname
_NODE_ID: Optional[str] = None


def _get_node_id() -> str:
    global _NODE_ID
    if _NODE_ID is None:
        _NODE_ID = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
    return _NODE_ID


class SuperpowersAdapter:
    """Bridges superpowers workflow state to fleet dispatch decisions.

    Usage:
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming", source="webhook")
        ok, reason = adapter.can_dispatch_race()
        # ok=False, reason="Workflow in brainstorming phase — races blocked"
    """

    # Phases where fleet races must NOT be dispatched — design is in progress
    BLOCKING_PHASES = frozenset({
        "signal",
        "brainstorming",
        "writing-plans",
        # Supervisor equivalents
        "inbox_capture",
        "surgeon_adjudication",
        "chief_escalation",
        "vision_lock",
    })

    # Phases where fleet execution is allowed
    EXECUTION_PHASES = frozenset({
        "executing-plans",
        "done",
        # Supervisor equivalents
        "execution",
        "review",
        "fix_loop",
        "snapshot_record",
        "dao_candidate",
        "chief_acceptance",
        "complete",
    })

    def __init__(self):
        self._current_phase: str = "unknown"
        self._phase_source: Optional[str] = None  # How we learned the phase
        self._updated_at: float = 0.0
        self._queued_races: list[dict] = []  # Races deferred during blocking phase

    def update_phase(self, phase: str, source: str = "manual") -> None:
        """Update current workflow phase.

        Args:
            phase: New phase name (superpowers or supervisor format).
            source: How we learned about this phase (webhook, event, manual).
        """
        old = self._current_phase
        self._current_phase = phase
        self._phase_source = source
        self._updated_at = time.time()

        if old != phase:
            logger.info(
                "superpowers phase: %s -> %s (source=%s)", old, phase, source,
            )
            self.on_phase_change(old, phase)

    def can_dispatch_race(self) -> tuple[bool, str]:
        """Check if fleet is allowed to dispatch a race right now.

        Returns:
            (allowed, reason) tuple.
        """
        if self._current_phase in self.BLOCKING_PHASES:
            return (
                False,
                f"Workflow in {self._current_phase} phase — races blocked",
            )
        if self._current_phase == "unknown":
            # Unknown phase — allow by default (graceful degradation)
            return True, "ok (phase unknown — default allow)"
        return True, "ok"

    def on_phase_change(self, old_phase: str, new_phase: str) -> None:
        """React to workflow phase transitions.

        Emits superpowers.phase.changed on the IDEEventBus.
        If entering an execution phase, flush any queued races.
        If entering a blocking phase, log the hold.
        """
        # Emit phase change event (graceful degradation if bus unavailable)
        try:
            from .event_bus import IDEEventBus
            bus = IDEEventBus.get()
            bus.superpowers_phase_changed(
                old_phase=old_phase,
                new_phase=new_phase,
                node_id=_get_node_id(),
                timestamp=self._updated_at,
            )
        except Exception as exc:
            logger.debug(
                "Could not emit superpowers.phase.changed: %s", exc,
            )

        if new_phase in self.EXECUTION_PHASES and self._queued_races:
            count = len(self._queued_races)
            logger.info(
                "Entering %s — releasing %d queued race(s)", new_phase, count,
            )
            self._queued_races.clear()

        if new_phase in self.BLOCKING_PHASES:
            logger.info(
                "Entering %s — fleet race dispatch paused", new_phase,
            )

    def queue_race(self, race_info: dict) -> None:
        """Queue a race for later dispatch when phase allows.

        Args:
            race_info: Dict with task, nodes, etc. — same args as FleetRace.start().
        """
        self._queued_races.append({
            **race_info,
            "queued_at": time.time(),
        })
        logger.info(
            "Race queued (phase=%s): %s",
            self._current_phase, race_info.get("task", "?")[:60],
        )

    def get_queued_races(self) -> list[dict]:
        """Return list of races waiting for an execution phase."""
        return list(self._queued_races)

    def get_state(self) -> dict:
        """Current adapter state for dashboard/overlay."""
        can_dispatch, reason = self.can_dispatch_race()
        return {
            "current_phase": self._current_phase,
            "phase_source": self._phase_source,
            "updated_at": self._updated_at,
            "can_dispatch": can_dispatch,
            "dispatch_reason": reason,
            "queued_races": len(self._queued_races),
        }


# ── Module-level singleton ───────────────────────────────────────────────

_adapter: Optional[SuperpowersAdapter] = None


def get_adapter() -> SuperpowersAdapter:
    """Get or create the global SuperpowersAdapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = SuperpowersAdapter()
    return _adapter
