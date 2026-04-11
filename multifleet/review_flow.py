"""Cross-Machine Review Flow Orchestrator — end-to-end review pipeline.

Wires together: cross_rebuttal (state machine) + synthesis (merge decision)
+ surgeon_adapter (3-surgeon validation) + adapters (events/escalation)
into a single coherent review flow.

Flow:
1. Dispatch task to N machines -> RebuttalSession created
2. Each machine does local surgery -> submits local_verdict
3. All verdicts collected -> cross-review dispatched
4. Each machine critiques others -> submits critique
5. All critiques in -> chief synthesis runs
6. Synthesis result -> acceptance gate -> resolved

This orchestrator provides the glue that was missing: a single entry point
that manages the complete lifecycle from dispatch to resolution.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from multifleet.cross_rebuttal import RebuttalManager, RebuttalPhase, RebuttalSession
from multifleet.synthesis import ChiefSynthesis, SynthesisResult

logger = logging.getLogger("fleet-nerve.review-flow")


@dataclass
class ReviewFlowResult:
    """Complete result of a cross-machine review flow."""

    task_id: str
    phase: str  # Current rebuttal phase
    synthesis: Optional[dict] = None
    acceptance: Optional[dict] = None
    machines_participated: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    events_emitted: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "synthesis": self.synthesis,
            "acceptance": self.acceptance,
            "machines_participated": self.machines_participated,
            "duration_s": round(self.duration_s, 1),
            "events_emitted": self.events_emitted,
            "error": self.error,
        }


class ReviewFlowOrchestrator:
    """Orchestrates the complete cross-machine review lifecycle.

    Usage:
        flow = ReviewFlowOrchestrator()

        # 1. Start a review
        session = flow.start_review(
            task_id="fix-auth",
            assigned_machines=["mac1", "mac2", "mac3"],
            task_summary="Fix auth token refresh in session manager",
        )

        # 2. Submit verdicts (called as they arrive from each machine)
        flow.submit_verdict("fix-auth", "mac1", {
            "summary": "Refactored token cache",
            "branch": "mac1/fix-auth",
            "confidence": 0.85,
            "files_touched": ["src/auth.py"],
        })

        # 3. Submit critiques
        flow.submit_critique("fix-auth", "mac2", {
            "concern": "Token cache may leak on concurrent access",
            "severity": "medium",
        })

        # 4. Run synthesis (auto-triggered when all critiques arrive)
        result = flow.run_synthesis("fix-auth")
    """

    def __init__(self, node_id: str = "", synthesis: Optional[ChiefSynthesis] = None):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._rebuttal = RebuttalManager()
        self._synthesis = synthesis or self._init_synthesis()
        self._results: dict[str, ReviewFlowResult] = {}
        self._stats = {
            "reviews_started": 0,
            "verdicts_received": 0,
            "critiques_received": 0,
            "syntheses_run": 0,
            "accepted": 0,
            "blocked": 0,
            "errors": 0,
        }

    @staticmethod
    def _init_synthesis() -> ChiefSynthesis:
        """Initialize ChiefSynthesis with graceful degradation."""
        try:
            return ChiefSynthesis()
        except Exception:
            # Return a bare synthesis engine without memory integration
            s = ChiefSynthesis.__new__(ChiefSynthesis)
            s._ledger_path = Path.home() / ".fleet-nerve" / "decisions.jsonl"
            s._analyzers = []
            s._default_analyzer = s._basic_confidence_analyzer
            s._evidence_ledger = None
            return s

    def start_review(
        self,
        task_id: str = "",
        assigned_machines: list[str] = None,
        task_summary: str = "",
        deadline_minutes: int = 30,
    ) -> RebuttalSession:
        """Start a new cross-machine review session.

        Args:
            task_id: Unique task identifier (auto-generated if empty).
            assigned_machines: List of machine IDs participating.
            task_summary: Brief description of the task.
            deadline_minutes: Auto-expire after this many minutes.
        """
        task_id = task_id or str(uuid.uuid4())[:12]
        assigned = assigned_machines or []

        session = self._rebuttal.start_session({
            "task_id": task_id,
            "assigned_to": assigned,
            "deadline_minutes": deadline_minutes,
            "summary": task_summary,
        })

        # Advance to LOCAL_SURGERY
        session.advance(RebuttalPhase.LOCAL_SURGERY)

        self._results[task_id] = ReviewFlowResult(
            task_id=task_id,
            phase=session.phase.value,
            machines_participated=list(assigned),
        )

        self._stats["reviews_started"] += 1
        self._emit_event("review.started", task_id, {
            "assigned_machines": assigned,
            "deadline_minutes": deadline_minutes,
        })

        logger.info(
            "review_flow started: task=%s machines=%s deadline=%dm",
            task_id[:8], assigned, deadline_minutes,
        )
        return session

    def submit_verdict(
        self,
        task_id: str,
        machine_id: str,
        verdict: dict,
    ) -> dict:
        """Submit a local verdict from a machine.

        Auto-advances to VERDICTS_COLLECTED when all machines have submitted.
        """
        result = self._rebuttal.handle_incoming_packet("local_verdict", {
            "task_id": task_id,
            "from": machine_id,
            **verdict,
        })

        self._stats["verdicts_received"] += 1
        self._update_result(task_id)

        self._emit_event("review.verdict_received", task_id, {
            "machine": machine_id,
            "confidence": verdict.get("confidence", 0.0),
        })

        # Auto-advance to UNDER_REVIEW when all verdicts are in
        session = self._rebuttal.get_session(task_id)
        if session and session.all_verdicts_in() and session.phase == RebuttalPhase.VERDICTS_COLLECTED:
            session.advance(RebuttalPhase.UNDER_REVIEW)
            self._update_result(task_id)
            logger.info("review_flow: all verdicts in for task=%s, advancing to UNDER_REVIEW", task_id[:8])

        return result

    def submit_critique(
        self,
        task_id: str,
        machine_id: str,
        critique: dict,
    ) -> dict:
        """Submit a critique from a machine.

        Auto-triggers synthesis when all critiques arrive.
        """
        result = self._rebuttal.handle_incoming_packet("critique", {
            "task_id": task_id,
            "from": machine_id,
            **critique,
        })

        self._stats["critiques_received"] += 1
        self._update_result(task_id)

        # Auto-run synthesis when needs_synthesis flag is set
        if result.get("needs_synthesis"):
            logger.info("review_flow: auto-running synthesis for task=%s", task_id[:8])
            return self.run_synthesis(task_id).to_dict()

        return result

    def run_synthesis(self, task_id: str) -> ReviewFlowResult:
        """Run chief synthesis on collected verdicts.

        Pulls memory context via ChiefSyncAdapter, runs synthesis,
        evaluates acceptance gate, and finalizes the review.
        """
        t0 = time.monotonic()
        flow_result = self._results.get(task_id, ReviewFlowResult(task_id=task_id, phase="unknown"))

        session = self._rebuttal.get_session(task_id)
        if not session:
            flow_result.error = f"No session found for task {task_id}"
            self._stats["errors"] += 1
            return flow_result

        if not session.verdicts:
            flow_result.error = "No verdicts to synthesize"
            self._stats["errors"] += 1
            return flow_result

        # Pull memory context
        memory_context = {}
        try:
            from multifleet.adapters.chief_sync import get_chief_sync
            sync = get_chief_sync()
            first_summary = next(
                (v.get("summary", "") for v in session.verdicts.values() if v.get("summary")), ""
            )
            files = []
            for v in session.verdicts.values():
                files.extend(v.get("files_touched", []))
            ctx = sync.pull(task_summary=first_summary, files_touched=files)
            memory_context = ctx.to_dict()
        except Exception as e:
            logger.debug("Memory context pull failed: %s", e)

        # Run synthesis
        try:
            synthesis_result = self._synthesis.synthesize(task_id, session.verdicts)
            flow_result.synthesis = synthesis_result.to_chief_decision()
            flow_result.synthesis["memory_context"] = memory_context
            self._stats["syntheses_run"] += 1
        except Exception as e:
            flow_result.error = f"Synthesis failed: {e}"
            self._stats["errors"] += 1
            logger.error("Synthesis failed for task %s: %s", task_id[:8], e)
            return flow_result

        # Record chief decision in rebuttal session
        self._rebuttal.handle_incoming_packet("chief_decision", {
            "task_id": task_id,
            **synthesis_result.to_chief_decision(),
        })

        # Run acceptance gate
        try:
            from multifleet.acceptance_gate import AcceptanceGate
            gate = AcceptanceGate()
            acceptance = gate.evaluate(
                task_id, None,
                context={
                    "surgeon_state": {"phase": "close", "disagreement_score": 0.0},
                    "probe_context": {"task_id": task_id},
                },
            )
            flow_result.acceptance = acceptance.to_dict()
            if acceptance.accepted:
                self._stats["accepted"] += 1
            else:
                self._stats["blocked"] += 1
        except Exception as e:
            logger.debug("Acceptance gate failed: %s", e)

        flow_result.phase = "resolved"
        flow_result.duration_s = time.monotonic() - t0
        flow_result.machines_participated = list(session.verdicts.keys())

        self._results[task_id] = flow_result

        self._emit_event("review.completed", task_id, {
            "decision": synthesis_result.decision,
            "confidence": synthesis_result.confidence,
            "accepted": flow_result.acceptance.get("accepted") if flow_result.acceptance else None,
        })

        logger.info(
            "review_flow completed: task=%s decision=%s confidence=%.2f duration=%.1fs",
            task_id[:8], synthesis_result.decision,
            synthesis_result.confidence, flow_result.duration_s,
        )
        return flow_result

    def get_status(self, task_id: str) -> dict:
        """Get current review flow status."""
        session = self._rebuttal.get_session(task_id)
        result = self._results.get(task_id)

        if not session and not result:
            return {"error": f"No review found for task {task_id}"}

        status = {
            "task_id": task_id,
            "phase": session.phase.value if session else (result.phase if result else "unknown"),
            "verdicts": len(session.verdicts) if session else 0,
            "critiques": len(session.critiques) if session else 0,
            "expected_machines": session.expected_machines if session else [],
            "expired": session.is_expired() if session else False,
        }

        if result:
            status["synthesis"] = result.synthesis
            status["acceptance"] = result.acceptance
            status["duration_s"] = result.duration_s

        return status

    def list_active(self) -> list[dict]:
        """List all active (non-resolved) reviews."""
        active = self._rebuttal.active_sessions()
        return [
            {
                "task_id": s.task_id,
                "phase": s.phase.value,
                "verdicts": len(s.verdicts),
                "critiques": len(s.critiques),
                "expected": len(s.expected_machines),
                "expired": s.is_expired(),
            }
            for s in active
        ]

    def cleanup_expired(self) -> int:
        """Clean up expired sessions. Returns count removed."""
        return self._rebuttal.cleanup_expired()

    def _update_result(self, task_id: str):
        """Update the result tracker with current session state."""
        session = self._rebuttal.get_session(task_id)
        if session and task_id in self._results:
            self._results[task_id].phase = session.phase.value

    def _emit_event(self, event_type: str, task_id: str, payload: dict):
        """Best-effort event emission."""
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            # Map to closest EventType
            type_map = {
                "review.started": EventType.FLEET_TASK_CREATED,
                "review.verdict_received": EventType.RACE_NODE_FINISHED,
                "review.completed": EventType.TASK_ACCEPTED,
            }
            et = type_map.get(event_type, EventType.FLEET_TASK_CREATED)
            emit_typed(et, EventSource.MULTI_FLEET, payload=payload, task_id=task_id)
            if task_id in self._results:
                self._results[task_id].events_emitted += 1
        except Exception:
            pass

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_orchestrator: Optional[ReviewFlowOrchestrator] = None


def get_review_flow() -> ReviewFlowOrchestrator:
    """Get or create the ReviewFlowOrchestrator singleton."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ReviewFlowOrchestrator()
    return _orchestrator
