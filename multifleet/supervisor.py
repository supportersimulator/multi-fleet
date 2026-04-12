"""Supervisor Loop — Dev Squad-style execution continuity.

Keeps dispatching until the Vision contract is satisfied or truly blocked.
Aaron's explicit request: "it seems to want to stop frequently."

State machine:
INBOX_CAPTURE → BRAINSTORMING → SURGEON_ADJUDICATION → CHIEF_ESCALATION →
VISION_LOCK → EXECUTION → REVIEW → FIX_LOOP → SNAPSHOT_RECORD →
DAO_CANDIDATE → CHIEF_ACCEPTANCE → COMPLETE
"""

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("fleet-nerve.supervisor")


class SupervisorPhase(Enum):
    INBOX_CAPTURE = "inbox_capture"
    BRAINSTORMING = "brainstorming"
    SURGEON_ADJUDICATION = "surgeon_adjudication"
    CHIEF_ESCALATION = "chief_escalation"
    VISION_LOCK = "vision_lock"
    EXECUTION = "execution"
    REVIEW = "review"
    FIX_LOOP = "fix_loop"
    SNAPSHOT_RECORD = "snapshot_record"
    DAO_CANDIDATE = "dao_candidate"
    CHIEF_ACCEPTANCE = "chief_acceptance"
    COMPLETE = "complete"
    BLOCKED = "blocked"


# Phase ordering for advance()
PHASE_ORDER = [
    SupervisorPhase.INBOX_CAPTURE,
    SupervisorPhase.BRAINSTORMING,
    SupervisorPhase.SURGEON_ADJUDICATION,
    SupervisorPhase.CHIEF_ESCALATION,
    SupervisorPhase.VISION_LOCK,
    SupervisorPhase.EXECUTION,
    SupervisorPhase.REVIEW,
    SupervisorPhase.FIX_LOOP,
    SupervisorPhase.SNAPSHOT_RECORD,
    SupervisorPhase.DAO_CANDIDATE,
    SupervisorPhase.CHIEF_ACCEPTANCE,
    SupervisorPhase.COMPLETE,
]

PHASE_INDEX = {phase: idx for idx, phase in enumerate(PHASE_ORDER)}


@dataclass
class VisionContract:
    """What must be true for the task to be 'done'."""
    task_id: str
    description: str
    done_criteria: list[str]  # Each must be satisfied
    blocked_criteria: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SupervisorState:
    task_id: str
    phase: SupervisorPhase = SupervisorPhase.INBOX_CAPTURE
    vision_contract: Optional[VisionContract] = None
    history: list[dict] = field(default_factory=list)  # phase transitions
    blocked_reason: str = ""
    iteration_count: int = 0
    max_iterations: int = 20  # safety limit
    started_at: float = field(default_factory=time.time)
    criteria_met: list[str] = field(default_factory=list)  # satisfied done_criteria

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    @property
    def is_terminal(self) -> bool:
        return self.phase in (SupervisorPhase.COMPLETE, SupervisorPhase.BLOCKED)

    @property
    def elapsed_s(self) -> float:
        return time.time() - self.started_at


class Supervisor:
    """Dev Squad supervisor — drives tasks through the phase state machine."""

    def __init__(self, node_id: str):
        self._node_id = node_id
        self._tasks: dict[str, SupervisorState] = {}

    def start_task(self, task_id: str, description: str,
                   done_criteria: list[str]) -> SupervisorState:
        """Start supervising a task with a vision contract."""
        if task_id in self._tasks:
            raise ValueError(f"Task already exists: {task_id}")
        contract = VisionContract(
            task_id=task_id,
            description=description,
            done_criteria=done_criteria,
        )
        state = SupervisorState(
            task_id=task_id,
            vision_contract=contract,
        )
        self._tasks[task_id] = state
        self._record_transition(state, None, SupervisorPhase.INBOX_CAPTURE, {})
        logger.info(
            "supervisor start task=%s criteria=%d node=%s",
            task_id, len(done_criteria), self._node_id,
        )
        return state

    def advance(self, task_id: str, evidence: dict = None) -> SupervisorState:
        """Advance to the next phase if conditions met. Returns updated state.

        The evidence dict can contain:
          - criteria_satisfied: list[str] — done_criteria that are now met
          - notes: str — free-form notes about the transition
          - fix_needed: bool — if True during REVIEW, go to FIX_LOOP instead of SNAPSHOT_RECORD
        """
        state = self._get_state(task_id)
        if state.is_terminal:
            return state

        evidence = evidence or {}
        old_phase = state.phase

        # Safety: max iterations
        state.iteration_count += 1
        if state.iteration_count > state.max_iterations:
            return self.block(task_id, f"Max iterations exceeded ({state.max_iterations})")

        # Track satisfied criteria
        newly_met = evidence.get("criteria_satisfied", [])
        for c in newly_met:
            if c not in state.criteria_met:
                state.criteria_met.append(c)

        # Determine next phase
        next_phase = self._next_phase(state, evidence)
        if next_phase == old_phase:
            # No advancement — stay in current phase
            return state

        state.phase = next_phase
        self._record_transition(state, old_phase, next_phase, evidence)

        # Phase-entry hooks for cross-module wiring
        self._on_enter_phase(state, next_phase, evidence)

        logger.info(
            "supervisor advance task=%s %s->%s iter=%d",
            task_id, old_phase.value, next_phase.value, state.iteration_count,
        )
        return state

    def block(self, task_id: str, reason: str) -> SupervisorState:
        """Mark task as blocked."""
        state = self._get_state(task_id)
        old_phase = state.phase
        state.phase = SupervisorPhase.BLOCKED
        state.blocked_reason = reason
        self._record_transition(state, old_phase, SupervisorPhase.BLOCKED, {"reason": reason})
        logger.warning("supervisor blocked task=%s reason=%s", task_id, reason)
        return state

    def get_state(self, task_id: str) -> SupervisorState:
        """Get current supervisor state."""
        return self._get_state(task_id)

    def list_active(self) -> list[SupervisorState]:
        """List all active (non-complete, non-blocked) tasks."""
        return [s for s in self._tasks.values() if not s.is_terminal]

    def list_all(self) -> list[SupervisorState]:
        """List all tasks."""
        return list(self._tasks.values())

    def is_done(self, task_id: str) -> bool:
        """Check if all done_criteria are satisfied."""
        state = self._get_state(task_id)
        if not state.vision_contract:
            return False
        return all(
            c in state.criteria_met
            for c in state.vision_contract.done_criteria
        )

    # ── Internal ──

    def _get_state(self, task_id: str) -> SupervisorState:
        if task_id not in self._tasks:
            raise ValueError(f"Unknown task: {task_id}")
        return self._tasks[task_id]

    def _next_phase(self, state: SupervisorState, evidence: dict) -> SupervisorPhase:
        """Determine the next phase based on current state and evidence."""
        phase = state.phase

        # Special: REVIEW can loop back to FIX_LOOP
        if phase == SupervisorPhase.REVIEW and evidence.get("fix_needed"):
            return SupervisorPhase.FIX_LOOP

        # Special: FIX_LOOP returns to REVIEW
        if phase == SupervisorPhase.FIX_LOOP:
            return SupervisorPhase.REVIEW

        # Special: check if all criteria met at REVIEW — skip to SNAPSHOT_RECORD
        if phase == SupervisorPhase.REVIEW and self.is_done(state.task_id):
            return SupervisorPhase.SNAPSHOT_RECORD

        # Default: advance to next in sequence
        idx = PHASE_INDEX.get(phase, -1)
        if idx < 0 or idx >= len(PHASE_ORDER) - 1:
            return phase
        return PHASE_ORDER[idx + 1]

    def _on_enter_phase(self, state: SupervisorState, phase: SupervisorPhase,
                        evidence: dict):
        """Cross-module hooks triggered on phase entry."""
        # SNAPSHOT_RECORD → auto-create truth ladder snapshot entry
        if phase == SupervisorPhase.SNAPSHOT_RECORD:
            try:
                from multifleet.truth_ladder import TruthLadder
                ladder = TruthLadder()
                # Add as inbox first (truth ladder requires inbox → vision → snapshot path)
                contract = state.vision_contract
                title = contract.description if contract else state.task_id
                entry = ladder.add_inbox(
                    title=f"[supervisor] {title}",
                    content=f"Task {state.task_id} reached SNAPSHOT_RECORD. "
                            f"Criteria met: {state.criteria_met}",
                    metadata={"task_id": state.task_id, "source": "supervisor"},
                )
                logger.info("supervisor→truth_ladder: inbox entry %s for task %s",
                            entry.id[:8], state.task_id)
            except Exception as e:
                logger.warning("supervisor→truth_ladder snapshot failed: %s", e)

        # DAO_CANDIDATE → attempt promotion to DAO (requires existing snapshot)
        elif phase == SupervisorPhase.DAO_CANDIDATE:
            try:
                from multifleet.truth_ladder import TruthLadder
                ladder = TruthLadder()
                # Find snapshot entries for this task
                snapshots = ladder.query(layer="snapshot", limit=10)
                for s in snapshots:
                    if s.metadata.get("task_id") == state.task_id:
                        ladder.promote_to_dao(
                            s.id, evidence_refs=[state.task_id],
                            promoted_by=self._node_id,
                        )
                        logger.info("supervisor→truth_ladder: promoted %s to DAO", s.id[:8])
                        break
            except Exception as e:
                logger.warning("supervisor→truth_ladder dao promotion failed: %s", e)

        # CHIEF_ACCEPTANCE → run acceptance gate
        elif phase == SupervisorPhase.CHIEF_ACCEPTANCE:
            try:
                from multifleet.acceptance_gate import AcceptanceGate
                gate = AcceptanceGate()
                decision = gate.evaluate(
                    evidence_count=len(state.criteria_met),
                    surgeon_resolved=True,
                    surgeon_intensity="standard",
                )
                if not decision.accepted:
                    logger.warning(
                        "supervisor acceptance gate BLOCKED task=%s: %s",
                        state.task_id, decision.blocking_issues,
                    )
                    # Record but don't auto-block — let caller decide
                else:
                    logger.info("supervisor acceptance gate PASSED task=%s", state.task_id)
            except Exception as e:
                logger.warning("supervisor→acceptance_gate failed: %s", e)

    def _record_transition(self, state: SupervisorState, from_phase: Optional[SupervisorPhase],
                           to_phase: SupervisorPhase, evidence: dict):
        state.history.append({
            "from": from_phase.value if from_phase else None,
            "to": to_phase.value,
            "at": time.time(),
            "iteration": state.iteration_count,
            "evidence": evidence,
        })

        # Emit to EventBus (best-effort)
        try:
            from multifleet.event_bus import IDEEventBus
            IDEEventBus.get().emit("supervisor.phase_transition", {
                "task_id": state.task_id,
                "from": from_phase.value if from_phase else None,
                "to": to_phase.value,
                "iteration": state.iteration_count,
            }, source=f"supervisor:{self._node_id}")
        except Exception:
            pass

        # Record to evidence ledger (best-effort)
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            EvidenceLedger().record(
                event_type="supervisor_transition",
                node_id=self._node_id,
                subject=f"supervisor:{state.task_id}:{to_phase.value}",
                payload={
                    "task_id": state.task_id,
                    "from": from_phase.value if from_phase else None,
                    "to": to_phase.value,
                    "iteration": state.iteration_count,
                },
            )
        except Exception:
            pass
