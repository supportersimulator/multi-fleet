"""Supervisor Watchdog — detects stalled phases and auto-advances.

The supervisor state machine can stall when no external caller drives
advance(). This watchdog detects phases that have been stuck beyond a
configurable timeout and either auto-advances or marks them blocked.

Usage:
    from multifleet.supervisor_watchdog import SupervisorWatchdog
    watchdog = SupervisorWatchdog(supervisor)
    stalled = watchdog.check_and_recover()
"""

import logging
import time
from typing import Optional

from multifleet.supervisor import Supervisor, SupervisorPhase, SupervisorState

logger = logging.getLogger("fleet-nerve.supervisor_watchdog")

# Per-phase stall timeouts (seconds). Expensive phases get more time.
PHASE_TIMEOUTS = {
    SupervisorPhase.INBOX_CAPTURE: 60,
    SupervisorPhase.BRAINSTORMING: 120,
    SupervisorPhase.SURGEON_ADJUDICATION: 180,
    SupervisorPhase.CHIEF_ESCALATION: 120,
    SupervisorPhase.VISION_LOCK: 60,
    SupervisorPhase.EXECUTION: 600,  # 10 min — actual work happens here
    SupervisorPhase.REVIEW: 120,
    SupervisorPhase.FIX_LOOP: 300,   # 5 min — may involve code changes
    SupervisorPhase.SNAPSHOT_RECORD: 60,
    SupervisorPhase.DAO_CANDIDATE: 60,
    SupervisorPhase.CHIEF_ACCEPTANCE: 120,
}

# Phases safe to auto-advance (no external input required)
AUTO_ADVANCE_PHASES = frozenset({
    SupervisorPhase.INBOX_CAPTURE,
    SupervisorPhase.VISION_LOCK,
    SupervisorPhase.SNAPSHOT_RECORD,
    SupervisorPhase.DAO_CANDIDATE,
})

# Phases that should block (not auto-advance) because they need real work
BLOCK_ON_STALL_PHASES = frozenset({
    SupervisorPhase.EXECUTION,
    SupervisorPhase.FIX_LOOP,
    SupervisorPhase.SURGEON_ADJUDICATION,
})


class SupervisorWatchdog:
    """Monitors supervisor tasks for stalls and triggers recovery."""

    def __init__(self, supervisor: Supervisor, timeout_overrides: dict = None):
        self._supervisor = supervisor
        self._timeouts = dict(PHASE_TIMEOUTS)
        if timeout_overrides:
            for phase, timeout in timeout_overrides.items():
                self._timeouts[phase] = timeout
        self._recoveries = 0

    def check_and_recover(self) -> list[dict]:
        """Check all active tasks for stalls. Returns list of actions taken.

        For each stalled task:
        - AUTO_ADVANCE_PHASES: force advance to next phase
        - BLOCK_ON_STALL_PHASES: mark as blocked with timeout reason
        - Others: attempt advance with stall evidence
        """
        actions = []
        now = time.time()

        for state in self._supervisor.list_active():
            last_transition_at = self._last_transition_time(state)
            if last_transition_at is None:
                continue

            elapsed = now - last_transition_at
            timeout = self._timeouts.get(state.phase, 120)

            if elapsed < timeout:
                continue

            # Phase is stalled
            action = self._recover(state, elapsed, timeout)
            if action:
                actions.append(action)
                self._recoveries += 1

        return actions

    def _last_transition_time(self, state: SupervisorState) -> Optional[float]:
        """Get timestamp of last phase transition."""
        if not state.history:
            return state.started_at
        return state.history[-1].get("at", state.started_at)

    def _recover(self, state: SupervisorState, elapsed: float,
                 timeout: float) -> Optional[dict]:
        """Attempt recovery for a stalled task."""
        task_id = state.task_id
        phase = state.phase

        logger.warning(
            "watchdog stall_detected task=%s phase=%s elapsed=%.0fs timeout=%.0fs",
            task_id, phase.value, elapsed, timeout,
        )

        evidence = {
            "notes": f"Watchdog: stalled {elapsed:.0f}s in {phase.value} (timeout={timeout}s)",
            "watchdog_recovery": True,
        }

        if phase in AUTO_ADVANCE_PHASES:
            # Safe to auto-advance
            new_state = self._supervisor.advance(task_id, evidence)
            action = {
                "task_id": task_id,
                "action": "auto_advance",
                "from_phase": phase.value,
                "to_phase": new_state.phase.value,
                "elapsed_s": round(elapsed, 1),
            }
            logger.info("watchdog auto_advance task=%s %s->%s",
                        task_id, phase.value, new_state.phase.value)
            return action

        if phase in BLOCK_ON_STALL_PHASES:
            # Needs real work — block rather than skip
            self._supervisor.block(
                task_id,
                f"Stalled in {phase.value} for {elapsed:.0f}s (timeout={timeout}s). "
                f"Needs manual intervention or re-dispatch.",
            )
            action = {
                "task_id": task_id,
                "action": "blocked",
                "from_phase": phase.value,
                "to_phase": "blocked",
                "elapsed_s": round(elapsed, 1),
            }
            logger.warning("watchdog blocked task=%s phase=%s", task_id, phase.value)
            return action

        # Default: try advancing with stall evidence
        new_state = self._supervisor.advance(task_id, evidence)
        action = {
            "task_id": task_id,
            "action": "advance_with_evidence",
            "from_phase": phase.value,
            "to_phase": new_state.phase.value,
            "elapsed_s": round(elapsed, 1),
        }
        logger.info("watchdog advance task=%s %s->%s",
                     task_id, phase.value, new_state.phase.value)
        return action

    @property
    def stats(self) -> dict:
        return {"total_recoveries": self._recoveries}
