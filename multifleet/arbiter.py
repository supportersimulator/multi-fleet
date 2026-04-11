"""Human Arbiter — presents fleet disagreements to Aaron for decision.

When machines disagree (low-confidence synthesis, unresolved rebuttals,
contested leases), the arbiter queues the decision for human review.
Aaron sees: recommendation + alternatives + dissent + evidence.
One click resolves it.
"""

import json
import logging
import time
import uuid
import urllib.request
from dataclasses import dataclass, field
from typing import Literal, Optional

logger = logging.getLogger("fleet-nerve.arbiter")


@dataclass
class PendingDecision:
    id: str
    decision_type: Literal["race", "rebuttal", "lease", "escalation"]
    task_description: str
    recommendation: dict  # {approach, confidence, reasoning, evidence_refs}
    alternatives: list[dict]  # [{approach, node, summary, strengths, probe_score}]
    dissent: list[dict]  # [{surgeon, concern, severity, evidence}]
    evidence_chain: list[dict]
    source_id: str  # race_id, rebuttal_id, etc.
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending, decided, expired
    decision: dict = field(default_factory=dict)  # filled when decided


class HumanArbiter:
    """Queues fleet disagreements for human arbitration.

    Lifecycle:
        queue_decision() -> pending -> decide() -> executed
        expire_stale() cleans up decisions older than max_age_s.
    """

    def __init__(self, daemon_port: int = 8855, max_age_s: int = 3600):
        self._port = daemon_port
        self._max_age_s = max_age_s
        self._pending: dict[str, PendingDecision] = {}
        self._decided: list[dict] = []

    # ── Public API ──────────────────────────────────────────────────

    def queue_decision(
        self,
        decision_type: str,
        task: str,
        recommendation: dict,
        alternatives: list,
        dissent: list,
        evidence: list,
        source_id: str = "",
    ) -> PendingDecision:
        """Queue a decision for human review.

        Args:
            decision_type: One of race, rebuttal, lease, escalation.
            task: Human-readable task description.
            recommendation: Best option with confidence + reasoning.
            alternatives: Other viable approaches.
            dissent: Unresolved objections from surgeons/nodes.
            evidence: Supporting evidence chain.
            source_id: ID of the originating race/rebuttal/lease.

        Returns:
            PendingDecision with unique ID.
        """
        decision = PendingDecision(
            id=str(uuid.uuid4())[:8],
            decision_type=decision_type,
            task_description=task,
            recommendation=recommendation,
            alternatives=alternatives,
            dissent=dissent,
            evidence_chain=evidence,
            source_id=source_id,
        )
        self._pending[decision.id] = decision
        self._emit_pending(decision)
        logger.info(
            "Arbiter: queued %s decision %s — %s",
            decision_type, decision.id, task[:60],
        )
        return decision

    def decide(
        self,
        decision_id: str,
        action: str,
        custom_reason: str = "",
    ) -> dict:
        """Record human decision and execute action.

        Actions:
            accept_recommendation — go with the top pick
            accept_alternative_N — pick alternative N (1-indexed)
            request_info — ask fleet for more data before deciding
            override_custom — human provides custom direction

        Returns:
            Result dict with decided=True on success or error key.
        """
        decision = self._pending.get(decision_id)
        if not decision:
            return {"error": f"Unknown decision: {decision_id}"}

        decision.status = "decided"
        decision.decision = {
            "action": action,
            "custom_reason": custom_reason,
            "decided_at": time.time(),
            "decided_by": "human",
        }

        # Move from pending to decided
        self._decided.append(self._to_dict(decision))
        del self._pending[decision_id]

        # Record in evidence ledger
        self._record_decision(decision)

        # Execute the action
        result = self._execute_action(decision)

        logger.info(
            "Arbiter: decided %s — action=%s",
            decision_id, action,
        )
        return {
            "decided": True,
            "decision_id": decision_id,
            "action": action,
            "result": result,
        }

    def get_pending(self) -> list[dict]:
        """Get all pending decisions as dicts."""
        return [self._to_dict(d) for d in self._pending.values()]

    def get_decision(self, decision_id: str) -> Optional[dict]:
        """Get a specific pending decision by ID."""
        d = self._pending.get(decision_id)
        return self._to_dict(d) if d else None

    def get_decided(self) -> list[dict]:
        """Get history of decided items."""
        return list(self._decided)

    def expire_stale(self) -> int:
        """Expire decisions older than max_age_s. Returns count expired."""
        now = time.time()
        expired_ids = [
            did for did, d in self._pending.items()
            if (now - d.created_at) > self._max_age_s
        ]
        for did in expired_ids:
            d = self._pending.pop(did)
            d.status = "expired"
            self._decided.append(self._to_dict(d))
            logger.info("Arbiter: expired stale decision %s", did)
        return len(expired_ids)

    # ── Serialization ──────────────────────────────────────────────

    @staticmethod
    def _to_dict(d: PendingDecision) -> dict:
        """Convert PendingDecision to dict."""
        return {
            "id": d.id,
            "decision_type": d.decision_type,
            "task_description": d.task_description,
            "recommendation": d.recommendation,
            "alternatives": d.alternatives,
            "dissent": d.dissent,
            "evidence_chain": d.evidence_chain,
            "source_id": d.source_id,
            "created_at": d.created_at,
            "status": d.status,
            "decision": d.decision,
        }

    # ── Event Emission ─────────────────────────────────────────────

    def _emit_pending(self, decision: PendingDecision):
        """Notify fleet of new pending decision via daemon message endpoint."""
        try:
            payload = json.dumps({
                "type": "alert",
                "to": "all",
                "payload": {
                    "subject": f"ARBITER: Decision needed — {decision.task_description[:80]}",
                    "body": f"Decision {decision.id} ({decision.decision_type}) awaiting human review",
                },
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/message",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            logger.debug("Arbiter emit failed (daemon may be offline): %s", e)

    def _record_decision(self, decision: PendingDecision):
        """Record decided outcome in evidence ledger via daemon."""
        try:
            payload = json.dumps({
                "action": "record",
                "event_type": "human_decision",
                "node_id": "human",
                "subject": f"Arbiter decision on {decision.source_id or decision.id}",
                "payload": json.dumps(decision.decision),
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/evidence",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception as e:
            logger.debug("Arbiter evidence record failed: %s", e)

    # ── Action Execution ───────────────────────────────────────────

    def _execute_action(self, decision: PendingDecision) -> dict:
        """Execute the chosen action and return result summary."""
        action = decision.decision.get("action", "")

        if action == "accept_recommendation":
            return {
                "executed": "recommendation accepted",
                "approach": decision.recommendation.get("approach"),
            }

        if action.startswith("accept_alternative_"):
            try:
                idx = int(action.split("_")[-1]) - 1
            except (ValueError, IndexError):
                return {"executed": "invalid alternative index"}
            if 0 <= idx < len(decision.alternatives):
                alt = decision.alternatives[idx]
                return {
                    "executed": "alternative accepted",
                    "approach": alt.get("approach"),
                }
            return {"executed": "alternative index out of range"}

        if action == "request_info":
            return {"executed": "info requested", "sent_to": "fleet"}

        if action == "override_custom":
            return {
                "executed": "custom override",
                "reason": decision.decision.get("custom_reason", ""),
            }

        return {"executed": "unknown action", "action": action}
