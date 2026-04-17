"""Rebuttal Protocol — cross-machine preserved dissent.

Core Truth #1: dissent is preserved, never overridden.

When mac1 proposes a merge decision, mac2 and mac3 can REBUT with evidence.
The chief weighs rebuttals and records the final decision WITH all dissenting
opinions preserved in both the evidence ledger (hash-chained) and the
corrigibility ledger (tracks when rebuttals change decisions).

NATS subjects:
  fleet.all.rebuttal      -- broadcast proposals + rebuttals to all nodes
  fleet.<node>.rebuttal   -- direct rebuttal to a specific node

Evidence types recorded:
  rebuttal_proposal       -- initial merge proposal with evidence refs
  rebuttal_dissent        -- a node's counter-evidence against a proposal
  rebuttal_resolution     -- chief's final decision with preserved dissent

Corrigibility tracking:
  When a rebuttal changes the chief's decision (proposed != final), the
  corrigibility ledger records the before/after as proof the system listens.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet-nerve.rebuttal-protocol")


class ProposalState(Enum):
    """Lifecycle of a merge proposal."""
    OPEN = "open"                # Proposal broadcast, awaiting rebuttals
    REBUTTAL_PERIOD = "rebuttal_period"  # Actively collecting rebuttals
    RESOLVING = "resolving"      # Chief weighing evidence
    RESOLVED = "resolved"        # Final decision made, dissent preserved


# Seconds to wait for rebuttals before auto-resolving
DEFAULT_REBUTTAL_WINDOW_S = 120


@dataclass
class Rebuttal:
    """A single rebuttal from a node against a proposal."""
    rebuttal_id: str
    task_id: str
    node_id: str
    counter_evidence: list[str]  # Evidence refs that contradict proposal
    reasoning: str
    confidence: float            # 0.0-1.0 how strongly this node disagrees
    timestamp: float
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rebuttal_id": self.rebuttal_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "counter_evidence": self.counter_evidence,
            "reasoning": self.reasoning,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rebuttal":
        return cls(
            rebuttal_id=d.get("rebuttal_id", str(uuid.uuid4())),
            task_id=d["task_id"],
            node_id=d["node_id"],
            counter_evidence=d.get("counter_evidence", []),
            reasoning=d.get("reasoning", ""),
            confidence=d.get("confidence", 0.5),
            timestamp=d.get("timestamp", time.time()),
            metadata=d.get("metadata", {}),
        )


@dataclass
class Proposal:
    """A merge proposal that can be rebutted."""
    task_id: str
    proposer: str                # Node that proposed
    decision: str                # merge | cherry_pick | reject
    evidence_refs: list[str]     # Evidence supporting the proposal
    reasoning: str
    confidence: float
    state: ProposalState
    created_at: float
    rebuttal_window_s: float
    rebuttals: dict[str, Rebuttal] = field(default_factory=dict)  # node_id -> Rebuttal
    resolution: Optional[dict] = None  # Final decision dict

    def is_rebuttal_window_open(self) -> bool:
        """True if still accepting rebuttals."""
        if self.state == ProposalState.RESOLVED:
            return False
        return (time.time() - self.created_at) < self.rebuttal_window_s

    def has_dissent(self) -> bool:
        """True if any node rebutted."""
        return len(self.rebuttals) > 0

    def dissent_summary(self) -> list[dict]:
        """Summary of all dissenting opinions for preservation."""
        return [
            {
                "node": r.node_id,
                "reasoning": r.reasoning,
                "confidence": r.confidence,
                "evidence_count": len(r.counter_evidence),
                "counter_evidence": r.counter_evidence,
            }
            for r in self.rebuttals.values()
        ]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "proposer": self.proposer,
            "decision": self.decision,
            "evidence_refs": self.evidence_refs,
            "reasoning": self.reasoning,
            "confidence": round(self.confidence, 4),
            "state": self.state.value,
            "created_at": self.created_at,
            "rebuttal_window_s": self.rebuttal_window_s,
            "rebuttals": {k: v.to_dict() for k, v in self.rebuttals.items()},
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        rebuttals = {}
        for k, v in d.get("rebuttals", {}).items():
            rebuttals[k] = Rebuttal.from_dict(v)
        return cls(
            task_id=d["task_id"],
            proposer=d["proposer"],
            decision=d["decision"],
            evidence_refs=d.get("evidence_refs", []),
            reasoning=d.get("reasoning", ""),
            confidence=d.get("confidence", 0.5),
            state=ProposalState(d.get("state", "open")),
            created_at=d.get("created_at", time.time()),
            rebuttal_window_s=d.get("rebuttal_window_s", DEFAULT_REBUTTAL_WINDOW_S),
            rebuttals=rebuttals,
            resolution=d.get("resolution"),
        )


class RebuttalProtocol:
    """Cross-machine rebuttal protocol with preserved dissent.

    Integrates with:
    - Evidence Ledger: hash-chained recording of proposals + rebuttals
    - Corrigibility Ledger: tracks when rebuttals change decisions
    - NATS: broadcasts proposals/rebuttals via fleet.all.rebuttal

    Usage:
        proto = RebuttalProtocol(node_id="mac1")
        proposal = proto.propose("task-123", "merge", ["ev-001", "ev-002"])
        # Other nodes call:
        proto.rebut("task-123", ["ev-003"], "Branch has regression in test X")
        # Chief resolves:
        result = proto.resolve("task-123")
    """

    def __init__(
        self,
        node_id: str,
        evidence_ledger=None,
        corrigibility_ledger=None,
        nats_publish: Optional[Callable] = None,
        rebuttal_window_s: float = DEFAULT_REBUTTAL_WINDOW_S,
    ):
        self._node_id = node_id
        self._evidence_ledger = evidence_ledger
        self._corrigibility_ledger = corrigibility_ledger
        self._nats_publish = nats_publish  # async fn(subject, payload_dict)
        self._rebuttal_window_s = rebuttal_window_s
        self._proposals: dict[str, Proposal] = {}

    # ── Core API ─────────────────────────────────────────────────────────

    def propose(
        self,
        task_id: str,
        decision: str,
        evidence_refs: list[str],
        reasoning: str = "",
        confidence: float = 0.8,
        rebuttal_window_s: Optional[float] = None,
    ) -> Proposal:
        """Broadcast a merge proposal to the fleet.

        Creates a proposal, records it to the evidence ledger, and publishes
        to NATS for other nodes to review and potentially rebut.

        Args:
            task_id: Unique task identifier
            decision: Proposed decision (merge, cherry_pick, reject)
            evidence_refs: List of evidence entry IDs supporting this proposal
            reasoning: Human-readable reasoning
            confidence: 0.0-1.0 proposer's confidence
            rebuttal_window_s: Override default rebuttal window

        Returns:
            The created Proposal object
        """
        window = rebuttal_window_s if rebuttal_window_s is not None else self._rebuttal_window_s
        proposal = Proposal(
            task_id=task_id,
            proposer=self._node_id,
            decision=decision,
            evidence_refs=evidence_refs,
            reasoning=reasoning,
            confidence=confidence,
            state=ProposalState.OPEN,
            created_at=time.time(),
            rebuttal_window_s=window,
        )
        self._proposals[task_id] = proposal
        logger.info(
            "Proposal created: task=%s decision=%s by %s (window=%ds)",
            task_id[:8], decision, self._node_id, window,
        )

        # Record to evidence ledger
        self._record_evidence("rebuttal_proposal", task_id, {
            "proposer": self._node_id,
            "decision": decision,
            "evidence_refs": evidence_refs,
            "reasoning": reasoning,
            "confidence": confidence,
        })

        # Broadcast via NATS
        self._publish("fleet.all.rebuttal", {
            "type": "rebuttal_proposal",
            "task_id": task_id,
            "from": self._node_id,
            "proposal": proposal.to_dict(),
        })

        return proposal

    def rebut(
        self,
        task_id: str,
        counter_evidence: list[str],
        reasoning: str,
        confidence: float = 0.7,
        metadata: Optional[dict] = None,
    ) -> Optional[Rebuttal]:
        """Record a rebuttal against a proposal with evidence.

        The rebuttal is preserved regardless of the final decision.
        This is Core Truth #1: dissent is never overridden.

        Args:
            task_id: The proposal being rebutted
            counter_evidence: Evidence refs that contradict the proposal
            reasoning: Why this node disagrees
            confidence: 0.0-1.0 strength of disagreement
            metadata: Optional extra context (surgeon findings, probe results)

        Returns:
            The Rebuttal object, or None if proposal not found / window closed
        """
        proposal = self._proposals.get(task_id)
        if not proposal:
            logger.warning("Cannot rebut task %s: proposal not found", task_id[:8])
            return None

        if proposal.state == ProposalState.RESOLVED:
            logger.warning("Cannot rebut task %s: already resolved", task_id[:8])
            return None

        if not proposal.is_rebuttal_window_open():
            logger.warning("Cannot rebut task %s: rebuttal window closed", task_id[:8])
            return None

        rebuttal = Rebuttal(
            rebuttal_id=str(uuid.uuid4()),
            task_id=task_id,
            node_id=self._node_id,
            counter_evidence=counter_evidence,
            reasoning=reasoning,
            confidence=confidence,
            timestamp=time.time(),
            metadata=metadata or {},
        )

        proposal.rebuttals[self._node_id] = rebuttal
        if proposal.state == ProposalState.OPEN:
            proposal.state = ProposalState.REBUTTAL_PERIOD

        logger.info(
            "Rebuttal recorded: task=%s by %s (confidence=%.2f, evidence=%d)",
            task_id[:8], self._node_id, confidence, len(counter_evidence),
        )

        # Record to evidence ledger — hash-chained with proposal
        self._record_evidence("rebuttal_dissent", task_id, {
            "rebuttal_id": rebuttal.rebuttal_id,
            "rebutter": self._node_id,
            "counter_evidence": counter_evidence,
            "reasoning": reasoning,
            "confidence": confidence,
            "original_proposal_decision": proposal.decision,
            "original_proposer": proposal.proposer,
        })

        # Broadcast to fleet
        self._publish("fleet.all.rebuttal", {
            "type": "rebuttal_dissent",
            "task_id": task_id,
            "from": self._node_id,
            "rebuttal": rebuttal.to_dict(),
        })

        return rebuttal

    def resolve(
        self,
        task_id: str,
        final_decision: Optional[str] = None,
        final_reasoning: Optional[str] = None,
    ) -> Optional[dict]:
        """Chief weighs rebuttals and records final decision WITH preserved dissent.

        If rebuttals exist and change the decision, this is recorded in the
        corrigibility ledger as proof the system listens to dissent.

        Args:
            task_id: The proposal to resolve
            final_decision: Override decision (if None, uses original proposal)
            final_reasoning: Override reasoning

        Returns:
            Resolution dict with decision + preserved dissent, or None if not found
        """
        proposal = self._proposals.get(task_id)
        if not proposal:
            logger.warning("Cannot resolve task %s: not found", task_id[:8])
            return None

        # Determine final decision
        original_decision = proposal.decision
        decision = final_decision if final_decision is not None else original_decision
        reasoning = final_reasoning if final_reasoning is not None else proposal.reasoning
        decision_changed = (decision != original_decision)

        # Build resolution with preserved dissent (Core Truth #1)
        dissent = proposal.dissent_summary()
        resolution = {
            "task_id": task_id,
            "original_decision": original_decision,
            "final_decision": decision,
            "decision_changed": decision_changed,
            "reasoning": reasoning,
            "proposer": proposal.proposer,
            "resolver": self._node_id,
            "rebuttal_count": len(proposal.rebuttals),
            "dissent_preserved": dissent,
            "resolved_at": time.time(),
        }

        proposal.state = ProposalState.RESOLVED
        proposal.resolution = resolution

        logger.info(
            "Proposal resolved: task=%s decision=%s (changed=%s, rebuttals=%d)",
            task_id[:8], decision, decision_changed, len(proposal.rebuttals),
        )

        # Record to evidence ledger
        self._record_evidence("rebuttal_resolution", task_id, resolution)

        # Record to corrigibility ledger when rebuttals changed the decision
        if decision_changed and self._corrigibility_ledger:
            try:
                dissent_text = "; ".join(
                    f"{d['node']}: {d['reasoning']}" for d in dissent
                )
                self._corrigibility_ledger.record_decision_point(
                    context=f"rebuttal_resolution:{task_id}",
                    proposed_action=original_decision,
                    surgeon_input=dissent_text,
                    final_action=decision,
                    changed=True,
                    source="rebuttal_protocol",
                )
                logger.info(
                    "Corrigibility: rebuttal changed decision %s -> %s for task %s",
                    original_decision, decision, task_id[:8],
                )
            except Exception as e:
                logger.warning("Corrigibility ledger write failed: %s", e)

        # Broadcast resolution
        self._publish("fleet.all.rebuttal", {
            "type": "rebuttal_resolution",
            "task_id": task_id,
            "from": self._node_id,
            "resolution": resolution,
        })

        return resolution

    def get_rebuttals(self, task_id: str) -> list[dict]:
        """Return all rebuttals for a proposal.

        Returns list of rebuttal dicts, empty if no proposal found.
        """
        proposal = self._proposals.get(task_id)
        if not proposal:
            return []
        return [r.to_dict() for r in proposal.rebuttals.values()]

    def get_proposal(self, task_id: str) -> Optional[Proposal]:
        """Get a proposal by task_id."""
        return self._proposals.get(task_id)

    def get_proposal_dict(self, task_id: str) -> Optional[dict]:
        """Get a proposal as a dict (for serialization / UI)."""
        p = self._proposals.get(task_id)
        return p.to_dict() if p else None

    def active_proposals(self) -> list[Proposal]:
        """Return all non-resolved proposals."""
        return [
            p for p in self._proposals.values()
            if p.state != ProposalState.RESOLVED
        ]

    def all_proposals(self) -> list[Proposal]:
        """Return all proposals including resolved."""
        return list(self._proposals.values())

    # ── Incoming Message Handler ─────────────────────────────────────────

    def handle_incoming(self, payload: dict) -> dict:
        """Handle an incoming rebuttal NATS message.

        Routes proposal, dissent, and resolution messages. Returns action taken.
        Called by the fleet daemon's message handler when subject matches
        fleet.*.rebuttal.

        Args:
            payload: Decoded NATS message payload

        Returns:
            Dict with action taken and relevant state
        """
        msg_type = payload.get("type", "")
        task_id = payload.get("task_id", "")
        sender = payload.get("from", "")

        # Don't process our own messages
        if sender == self._node_id:
            return {"action": "ignored", "reason": "own_message"}

        if msg_type == "rebuttal_proposal":
            return self._handle_remote_proposal(payload)
        elif msg_type == "rebuttal_dissent":
            return self._handle_remote_rebuttal(payload)
        elif msg_type == "rebuttal_resolution":
            return self._handle_remote_resolution(payload)
        else:
            return {"action": "error", "error": f"unknown rebuttal type: {msg_type}"}

    def _handle_remote_proposal(self, payload: dict) -> dict:
        """Ingest a proposal from another node."""
        proposal_data = payload.get("proposal", {})
        task_id = proposal_data.get("task_id", "")
        if not task_id:
            return {"action": "error", "error": "missing task_id in proposal"}

        proposal = Proposal.from_dict(proposal_data)
        self._proposals[task_id] = proposal
        logger.info(
            "Remote proposal ingested: task=%s from %s decision=%s",
            task_id[:8], proposal.proposer, proposal.decision,
        )
        return {
            "action": "proposal_ingested",
            "task_id": task_id,
            "proposer": proposal.proposer,
            "decision": proposal.decision,
        }

    def _handle_remote_rebuttal(self, payload: dict) -> dict:
        """Ingest a rebuttal from another node."""
        rebuttal_data = payload.get("rebuttal", {})
        task_id = rebuttal_data.get("task_id", "")
        if not task_id:
            return {"action": "error", "error": "missing task_id in rebuttal"}

        proposal = self._proposals.get(task_id)
        if not proposal:
            return {"action": "error", "error": f"no proposal for task {task_id}"}

        rebuttal = Rebuttal.from_dict(rebuttal_data)
        proposal.rebuttals[rebuttal.node_id] = rebuttal
        if proposal.state == ProposalState.OPEN:
            proposal.state = ProposalState.REBUTTAL_PERIOD

        logger.info(
            "Remote rebuttal ingested: task=%s from %s",
            task_id[:8], rebuttal.node_id,
        )
        return {
            "action": "rebuttal_ingested",
            "task_id": task_id,
            "rebutter": rebuttal.node_id,
            "total_rebuttals": len(proposal.rebuttals),
        }

    def _handle_remote_resolution(self, payload: dict) -> dict:
        """Ingest a resolution from the chief."""
        resolution = payload.get("resolution", {})
        task_id = resolution.get("task_id", "")
        if not task_id:
            return {"action": "error", "error": "missing task_id in resolution"}

        proposal = self._proposals.get(task_id)
        if not proposal:
            # Create a minimal proposal to hold the resolution
            proposal = Proposal(
                task_id=task_id,
                proposer=resolution.get("proposer", "unknown"),
                decision=resolution.get("original_decision", "unknown"),
                evidence_refs=[],
                reasoning="",
                confidence=0.0,
                state=ProposalState.OPEN,
                created_at=time.time(),
                rebuttal_window_s=0,
            )
            self._proposals[task_id] = proposal

        proposal.state = ProposalState.RESOLVED
        proposal.resolution = resolution

        logger.info(
            "Remote resolution ingested: task=%s decision=%s changed=%s dissent=%d",
            task_id[:8],
            resolution.get("final_decision"),
            resolution.get("decision_changed"),
            len(resolution.get("dissent_preserved", [])),
        )
        return {
            "action": "resolution_ingested",
            "task_id": task_id,
            "final_decision": resolution.get("final_decision"),
            "decision_changed": resolution.get("decision_changed"),
            "dissent_count": len(resolution.get("dissent_preserved", [])),
        }

    # ── Auto-resolve expired proposals ───────────────────────────────────

    def expire_stale(self) -> int:
        """Auto-resolve proposals whose rebuttal window has closed.

        Proposals with no rebuttals auto-accept. Proposals with rebuttals
        remain open for the chief to resolve explicitly.

        Returns count of auto-resolved proposals.
        """
        count = 0
        for proposal in list(self._proposals.values()):
            if proposal.state == ProposalState.RESOLVED:
                continue
            if proposal.is_rebuttal_window_open():
                continue

            # Window closed, no rebuttals -> auto-accept
            if not proposal.has_dissent():
                self.resolve(
                    proposal.task_id,
                    final_decision=proposal.decision,
                    final_reasoning="auto-accepted: no rebuttals within window",
                )
                count += 1
                logger.info(
                    "Auto-accepted proposal %s (no rebuttals)",
                    proposal.task_id[:8],
                )
            else:
                # Has rebuttals but window closed — move to resolving state
                # Chief must explicitly resolve
                if proposal.state != ProposalState.RESOLVING:
                    proposal.state = ProposalState.RESOLVING
                    logger.info(
                        "Proposal %s moved to RESOLVING (%d rebuttals pending chief)",
                        proposal.task_id[:8], len(proposal.rebuttals),
                    )
        return count

    # ── Stats ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Rebuttal protocol stats for health endpoint."""
        proposals = list(self._proposals.values())
        total = len(proposals)
        active = sum(1 for p in proposals if p.state != ProposalState.RESOLVED)
        with_dissent = sum(1 for p in proposals if p.has_dissent())
        changed = sum(
            1 for p in proposals
            if p.resolution and p.resolution.get("decision_changed")
        )
        return {
            "total_proposals": total,
            "active": active,
            "with_dissent": with_dissent,
            "decisions_changed_by_rebuttal": changed,
            "by_state": {
                s.value: sum(1 for p in proposals if p.state == s)
                for s in ProposalState
            },
        }

    # ── Internal helpers ─────────────────────────────────────────────────

    def _record_evidence(self, event_type: str, task_id: str, payload: dict) -> None:
        """Record to evidence ledger if available."""
        if not self._evidence_ledger:
            return
        try:
            self._evidence_ledger.record(
                event_type=event_type,
                node_id=self._node_id,
                subject=f"rebuttal:{task_id[:20]}",
                payload=payload,
            )
        except Exception as e:
            logger.warning("Evidence ledger write failed: %s", e)

    def _publish(self, subject: str, payload: dict) -> None:
        """Publish to NATS if callback available."""
        if not self._nats_publish:
            return
        try:
            self._nats_publish(subject, payload)
        except Exception as e:
            logger.warning("NATS publish failed for %s: %s", subject, e)
