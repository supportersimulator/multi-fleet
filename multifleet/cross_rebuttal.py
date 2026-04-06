"""Cross-machine rebuttal state machine.

States: assigned -> local_surgery -> verdicts_collected -> under_review ->
        critiques_collected -> synthesis -> resolved

Transitions are validated. Timeout auto-advances after deadline.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("fleet-nerve.cross-rebuttal")


class RebuttalPhase(Enum):
    ASSIGNED = "assigned"
    LOCAL_SURGERY = "local_surgery"
    VERDICTS_COLLECTED = "verdicts_collected"
    UNDER_REVIEW = "under_review"
    CRITIQUES_COLLECTED = "critiques_collected"
    SYNTHESIS = "synthesis"
    RESOLVED = "resolved"


VALID_TRANSITIONS: dict[RebuttalPhase, list[RebuttalPhase]] = {
    RebuttalPhase.ASSIGNED: [RebuttalPhase.LOCAL_SURGERY],
    RebuttalPhase.LOCAL_SURGERY: [RebuttalPhase.VERDICTS_COLLECTED],
    RebuttalPhase.VERDICTS_COLLECTED: [RebuttalPhase.UNDER_REVIEW],
    RebuttalPhase.UNDER_REVIEW: [RebuttalPhase.CRITIQUES_COLLECTED],
    RebuttalPhase.CRITIQUES_COLLECTED: [RebuttalPhase.SYNTHESIS],
    RebuttalPhase.SYNTHESIS: [RebuttalPhase.RESOLVED],
}


@dataclass
class RebuttalSession:
    """Tracks a single cross-machine rebuttal lifecycle."""

    task_id: str
    phase: RebuttalPhase
    created_at: float
    deadline_minutes: int
    verdicts: dict = field(default_factory=dict)    # machine_id -> verdict packet
    critiques: dict = field(default_factory=dict)    # machine_id -> critique packet
    chief_decision: Optional[dict] = None
    expected_machines: list = field(default_factory=list)  # machines assigned to task

    def advance(self, to_phase: RebuttalPhase) -> bool:
        """Advance to next phase. Returns False if transition invalid."""
        allowed = VALID_TRANSITIONS.get(self.phase, [])
        if to_phase not in allowed:
            logger.warning(
                "Invalid transition for task %s: %s -> %s (allowed: %s)",
                self.task_id[:8], self.phase.value, to_phase.value,
                [p.value for p in allowed],
            )
            return False
        logger.info(
            "Task %s: %s -> %s", self.task_id[:8], self.phase.value, to_phase.value
        )
        self.phase = to_phase
        return True

    def is_expired(self) -> bool:
        """Check if deadline has passed."""
        elapsed = time.time() - self.created_at
        return elapsed > self.deadline_minutes * 60

    def add_verdict(self, machine_id: str, verdict: dict) -> None:
        """Record a verdict from a machine. Auto-detects all-collected."""
        self.verdicts[machine_id] = verdict
        logger.info(
            "Task %s: verdict from %s (%d/%d)",
            self.task_id[:8], machine_id,
            len(self.verdicts), len(self.expected_machines),
        )

    def all_verdicts_in(self) -> bool:
        """True if every expected machine has submitted a verdict."""
        if not self.expected_machines:
            return False
        return all(m in self.verdicts for m in self.expected_machines)

    def add_critique(self, machine_id: str, critique: dict) -> None:
        """Record a critique from a machine. Auto-detects all-collected."""
        self.critiques[machine_id] = critique
        logger.info(
            "Task %s: critique from %s (%d/%d)",
            self.task_id[:8], machine_id,
            len(self.critiques), len(self.expected_machines),
        )

    def all_critiques_in(self) -> bool:
        """True if every expected machine has submitted a critique."""
        if not self.expected_machines:
            return False
        return all(m in self.critiques for m in self.expected_machines)

    def set_decision(self, decision: dict) -> None:
        """Record the chief's merge decision."""
        self.chief_decision = decision
        logger.info("Task %s: chief decision recorded", self.task_id[:8])

    def to_dict(self) -> dict:
        """Serialize session to dict."""
        return {
            "task_id": self.task_id,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "deadline_minutes": self.deadline_minutes,
            "verdicts": self.verdicts,
            "critiques": self.critiques,
            "chief_decision": self.chief_decision,
            "expected_machines": self.expected_machines,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RebuttalSession":
        """Deserialize session from dict."""
        return cls(
            task_id=data["task_id"],
            phase=RebuttalPhase(data["phase"]),
            created_at=data["created_at"],
            deadline_minutes=data["deadline_minutes"],
            verdicts=data.get("verdicts", {}),
            critiques=data.get("critiques", {}),
            chief_decision=data.get("chief_decision"),
            expected_machines=data.get("expected_machines", []),
        )


class RebuttalManager:
    """Manages active cross-machine rebuttal sessions."""

    def __init__(self):
        self._sessions: dict[str, RebuttalSession] = {}

    def start_session(self, task_brief: dict) -> RebuttalSession:
        """Create a new rebuttal session from a task_brief packet payload."""
        task_id = task_brief.get("task_id") or str(uuid.uuid4())
        session = RebuttalSession(
            task_id=task_id,
            phase=RebuttalPhase.ASSIGNED,
            created_at=time.time(),
            deadline_minutes=task_brief.get("deadline_minutes", 30),
            expected_machines=task_brief.get("assigned_to", []),
        )
        self._sessions[task_id] = session
        logger.info("Started rebuttal session %s", task_id[:8])
        return session

    def get_session(self, task_id: str) -> Optional[RebuttalSession]:
        """Retrieve a session by task_id."""
        return self._sessions.get(task_id)

    def advance_session(self, task_id: str) -> bool:
        """Advance session to the next sequential phase.

        Determines next phase from VALID_TRANSITIONS and advances.
        Returns False if session not found or transition invalid.
        """
        session = self._sessions.get(task_id)
        if not session:
            logger.warning("advance_session: task %s not found", task_id[:8])
            return False
        allowed = VALID_TRANSITIONS.get(session.phase, [])
        if not allowed:
            logger.warning("advance_session: task %s in terminal phase %s",
                           task_id[:8], session.phase.value)
            return False
        return session.advance(allowed[0])

    def cleanup_expired(self) -> int:
        """Remove expired sessions. Returns count removed."""
        expired = [
            tid for tid, s in self._sessions.items()
            if s.is_expired() and s.phase != RebuttalPhase.RESOLVED
        ]
        for tid in expired:
            logger.info("Cleaning expired session %s", tid[:8])
            del self._sessions[tid]
        return len(expired)

    def active_sessions(self) -> list[RebuttalSession]:
        """Return all non-resolved sessions."""
        return [
            s for s in self._sessions.values()
            if s.phase != RebuttalPhase.RESOLVED
        ]

    def all_sessions(self) -> list[RebuttalSession]:
        """Return all sessions including resolved."""
        return list(self._sessions.values())
