"""Superpowers <-> 3-Surgeons Bridge — connects brainstorming to surgical review.

When Superpowers completes brainstorming/planning, the output is packaged
as a BrainstormPacket and sent to 3-Surgeons for architectural review.
When 3-Surgeons produces a verdict, it's returned as a SurgeonDecisionPacket.

The bridge also produces VisionContracts when consensus is reached,
locking done-criteria so execution can begin with a clear target.

Zero Silent Failures: every bridge error is logged + counted.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from multifleet.vision_packets import (
    BrainstormPacket,
    BrainstormOption,
    SurgeonDecisionPacket,
    SurgeonOpinion,
    VisionContract,
)
from multifleet.surgeon_adapter import SurgeonAdapter

logger = logging.getLogger("fleet-nerve.superpowers-bridge")

# Observability counters (Zero Silent Failures)
_COUNTERS: Dict[str, int] = {
    "submits": 0,
    "decisions_returned": 0,
    "contracts_created": 0,
    "surgeon_unavailable": 0,
    "errors": 0,
}


class HandoffStore:
    """In-memory store for pending and completed handoffs.

    Bounded to prevent unbounded growth. Oldest entries evicted when full.
    """

    def __init__(self, max_size: int = 500):
        self._max_size = max_size
        self._brainstorms: Dict[str, BrainstormPacket] = {}
        self._decisions: Dict[str, SurgeonDecisionPacket] = {}
        self._contracts: Dict[str, VisionContract] = {}
        # task_id -> brainstorm_id mapping for lookups
        self._task_index: Dict[str, str] = {}

    def store_brainstorm(self, bp: BrainstormPacket) -> None:
        if len(self._brainstorms) >= self._max_size:
            oldest_key = next(iter(self._brainstorms))
            del self._brainstorms[oldest_key]
        self._brainstorms[bp.id] = bp
        if bp.task_id:
            self._task_index[bp.task_id] = bp.id

    def store_decision(self, sdp: SurgeonDecisionPacket) -> None:
        if len(self._decisions) >= self._max_size:
            oldest_key = next(iter(self._decisions))
            del self._decisions[oldest_key]
        self._decisions[sdp.id] = sdp

    def store_contract(self, vc: VisionContract) -> None:
        if len(self._contracts) >= self._max_size:
            oldest_key = next(iter(self._contracts))
            del self._contracts[oldest_key]
        self._contracts[vc.id] = vc

    def get_brainstorm(self, brainstorm_id: str) -> Optional[BrainstormPacket]:
        return self._brainstorms.get(brainstorm_id)

    def get_decision_for_brainstorm(self, brainstorm_id: str) -> Optional[SurgeonDecisionPacket]:
        for sdp in self._decisions.values():
            if sdp.brainstorm_id == brainstorm_id:
                return sdp
        return None

    def get_contract_for_task(self, task_id: str) -> Optional[VisionContract]:
        for vc in self._contracts.values():
            if vc.task_id == task_id:
                return vc
        return None

    def brainstorm_id_for_task(self, task_id: str) -> Optional[str]:
        return self._task_index.get(task_id)

    def list_pending(self) -> List[Dict[str, Any]]:
        """Brainstorms without a decision yet."""
        pending = []
        for bp in self._brainstorms.values():
            if self.get_decision_for_brainstorm(bp.id) is None:
                pending.append({
                    "brainstorm_id": bp.id,
                    "task_id": bp.task_id,
                    "options_count": len(bp.options),
                    "created_at": bp.created_at,
                })
        return pending

    def list_decided(self) -> List[Dict[str, Any]]:
        """Brainstorms that have a decision."""
        decided = []
        for sdp in self._decisions.values():
            bp = self._brainstorms.get(sdp.brainstorm_id)
            decided.append({
                "brainstorm_id": sdp.brainstorm_id,
                "decision_id": sdp.id,
                "task_id": sdp.task_id,
                "consensus": sdp.consensus,
                "selected_option": sdp.selected_option,
                "opinions_count": len(sdp.opinions),
                "created_at": sdp.created_at,
            })
        return decided

    def __len__(self) -> int:
        return len(self._brainstorms)


# Singleton store
_store = HandoffStore()


def get_store() -> HandoffStore:
    """Get the module-level handoff store."""
    return _store


class SuperpowersBridge:
    """Connects Superpowers brainstorming output to 3-Surgeons review pipeline.

    Flow:
      1. submit_for_review() — package brainstorm, send to surgeon adapter
      2. Surgeon adapter queries real 3-surgeon protocol
      3. Decision returned as SurgeonDecisionPacket
      4. If consensus reached, create_contract() locks a VisionContract
    """

    def __init__(self, store: Optional[HandoffStore] = None,
                 surgeon: Optional[SurgeonAdapter] = None):
        self._store = _store if store is None else store
        self._surgeon = SurgeonAdapter() if surgeon is None else surgeon

    @property
    def store(self) -> HandoffStore:
        return self._store

    def submit_for_review(self, brainstorm: BrainstormPacket) -> SurgeonDecisionPacket:
        """Send brainstorm output to 3-surgeons for review.

        Packages the brainstorm as a review request, queries the surgeon
        adapter, and translates the response into a SurgeonDecisionPacket.

        Returns a SurgeonDecisionPacket (may have consensus=False if
        surgeons disagree or are unavailable).
        """
        _COUNTERS["submits"] += 1
        self._store.store_brainstorm(brainstorm)

        # Build review prompt from brainstorm content
        prompt = self._build_review_prompt(brainstorm)

        # Query surgeon adapter
        if not self._surgeon.is_available():
            _COUNTERS["surgeon_unavailable"] += 1
            logger.warning("Surgeon adapter unavailable — returning degraded decision")
            return self._degraded_decision(brainstorm)

        try:
            raw = self._surgeon.request_review(prompt)
            sdp = self._translate_review(brainstorm, raw)
            self._store.store_decision(sdp)
            _COUNTERS["decisions_returned"] += 1
            return sdp
        except Exception as exc:
            _COUNTERS["errors"] += 1
            logger.error("Surgeon review failed: %s", exc)
            return self._degraded_decision(brainstorm)

    def check_decision(self, task_id: str) -> Optional[SurgeonDecisionPacket]:
        """Check if 3-surgeons has rendered a decision for a task.

        Looks up the brainstorm by task_id, then checks for a matching decision.
        Returns None if no decision exists yet.
        """
        bid = self._store.brainstorm_id_for_task(task_id)
        if bid is None:
            return None
        return self._store.get_decision_for_brainstorm(bid)

    def create_contract(self, brainstorm: BrainstormPacket,
                        decision: SurgeonDecisionPacket,
                        done_criteria: List[str],
                        node_id: str = "") -> VisionContract:
        """Create and lock a VisionContract from a decided brainstorm.

        Only call after consensus is reached. The contract locks the
        selected approach and done-criteria so execution has a clear target.
        """
        if not decision.consensus:
            raise ValueError("Cannot create contract without surgeon consensus")

        # Resolve selected approach name
        approach = decision.selected_option
        opt = brainstorm.get_option(decision.selected_option)
        if opt:
            approach = opt.title

        vc = VisionContract(
            task_id=brainstorm.task_id,
            description=f"Vision contract for {brainstorm.task_id}",
            done_criteria=done_criteria,
            selected_approach=approach,
            surgeon_decision_id=decision.id,
        )
        if node_id:
            vc.lock(node_id)

        self._store.store_contract(vc)
        _COUNTERS["contracts_created"] += 1
        return vc

    def get_counters(self) -> Dict[str, int]:
        """Return observability counters."""
        return dict(_COUNTERS)

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _build_review_prompt(bp: BrainstormPacket) -> str:
        """Build a text prompt from brainstorm options for surgeon review."""
        lines = [f"Task: {bp.task_id}"]
        for i, opt in enumerate(bp.options, 1):
            lines.append(f"\nOption {i}: {opt.title}")
            lines.append(f"  Description: {opt.description}")
            if opt.pros:
                lines.append(f"  Pros: {', '.join(opt.pros)}")
            if opt.cons:
                lines.append(f"  Cons: {', '.join(opt.cons)}")
            lines.append(f"  Confidence: {opt.confidence}")
        if bp.recommended:
            lines.append(f"\nRecommended: {bp.recommended}")
        return "\n".join(lines)

    @staticmethod
    def _degraded_decision(bp: BrainstormPacket) -> SurgeonDecisionPacket:
        """Return a degraded decision when surgeons are unavailable."""
        sdp = SurgeonDecisionPacket(
            brainstorm_id=bp.id,
            task_id=bp.task_id,
            consensus=False,
            unresolved_objections=["surgeon_unavailable"],
        )
        sdp.add_opinion(
            surgeon="atlas",
            vote="abstain",
            confidence=0.0,
            reasoning="Surgeon adapter unavailable — degraded mode",
        )
        return sdp

    @staticmethod
    def _translate_review(bp: BrainstormPacket,
                          raw: Dict[str, Any]) -> SurgeonDecisionPacket:
        """Translate a raw surgeon adapter review into a SurgeonDecisionPacket."""
        sdp = SurgeonDecisionPacket(
            brainstorm_id=bp.id,
            task_id=bp.task_id,
        )

        output = raw.get("output", "")
        status = raw.get("status", "unknown")
        review_type = raw.get("review_type", "unknown")

        # Parse what we can from the review output
        if status == "error" or status == "degraded":
            sdp.add_opinion(
                surgeon="atlas",
                vote="abstain",
                confidence=0.0,
                reasoning=f"Review {status}: {output}",
            )
            sdp.unresolved_objections.append(f"review_{status}")
            return sdp

        # Successful review — record as atlas opinion
        # Use recommended option if available, else abstain
        vote = bp.recommended if bp.recommended else "abstain"
        confidence = 0.7 if review_type == "cardio_review" else 0.5

        sdp.add_opinion(
            surgeon="atlas",
            vote=vote,
            confidence=confidence,
            reasoning=str(output)[:500] if output else "Review completed",
        )

        # Check consensus (will be false with only 1 opinion, but sets state)
        sdp.check_consensus()
        return sdp
