"""Human Arbiter — presents fleet disagreements to Aaron for decision.

When machines disagree (low-confidence synthesis, unresolved rebuttals,
contested leases), the arbiter queues the decision for human review.
Aaron sees: recommendation + alternatives + dissent + evidence.
One click resolves it.
"""

import json
import logging
import os
import tempfile
import time
import uuid
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
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

    def __init__(self, daemon_port: int = 8855, max_age_s: int = 3600, persist_path: str | None = None):
        self._port = daemon_port
        self._max_age_s = max_age_s
        self._pending: dict[str, PendingDecision] = {}
        self._decided: list[dict] = []
        self._persist_path = Path(persist_path) if persist_path else Path.home() / ".fleet-nerve" / "arbiter-pending.json"
        self._load_persisted()

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
        self._save_persisted()
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
        self._save_persisted()

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
        if expired_ids:
            self._save_persisted()
        return len(expired_ids)

    # ── Persistence ────────────────────────────────────────────────

    def _load_persisted(self):
        """Load pending decisions from JSON file on startup."""
        try:
            if self._persist_path.exists():
                data = json.loads(self._persist_path.read_text())
                for item in data:
                    d = PendingDecision(
                        id=item["id"],
                        decision_type=item["decision_type"],
                        task_description=item["task_description"],
                        recommendation=item["recommendation"],
                        alternatives=item["alternatives"],
                        dissent=item["dissent"],
                        evidence_chain=item["evidence_chain"],
                        source_id=item["source_id"],
                        created_at=item.get("created_at", time.time()),
                        status=item.get("status", "pending"),
                        decision=item.get("decision", {}),
                    )
                    self._pending[d.id] = d
                logger.info("Arbiter: restored %d pending decisions from %s", len(self._pending), self._persist_path)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Arbiter: corrupt persist file %s — starting fresh: %s", self._persist_path, e)
            self._pending = {}
        except Exception as e:
            logger.debug("Arbiter: no persisted state loaded: %s", e)

    def _save_persisted(self):
        """Atomically write pending decisions to JSON file."""
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps([self._to_dict(d) for d in self._pending.values()], indent=2)
            # Atomic write: write to .tmp then rename
            tmp_path = self._persist_path.with_suffix(".tmp")
            tmp_path.write_text(data)
            tmp_path.rename(self._persist_path)
        except Exception as e:
            logger.warning("Arbiter: failed to persist state: %s", e)

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
