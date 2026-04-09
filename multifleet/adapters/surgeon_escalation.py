"""3-Surgeons Escalation Adapter — build escalation packets on no-consensus.

When 3-surgeons cannot reach consensus, this adapter builds a
ChiefEscalationPacket and routes it through the fleet to the chief node.
The chief then uses ContextDNA memory to make an authoritative decision.

Closes the gap: "No structured escalation flow. Chief has no
'receive escalation, query memory, return weighted decision' pipeline."
"""

import logging
import os
import time
from typing import Optional

from multifleet.vision_packets import (
    ChiefEscalationPacket,
    ChiefDecisionPacket,
    SurgeonDecisionPacket,
)

logger = logging.getLogger("fleet-nerve.adapter.surgeon-escalation")


class SurgeonEscalationAdapter:
    """Builds escalation packets when surgeon consensus fails.

    Usage:
        adapter = SurgeonEscalationAdapter()
        packet = adapter.escalate_no_consensus(
            surgeon_decision=decision_packet,
            task_id="fix-auth",
        )
        # packet is a ChiefEscalationPacket ready for fleet dispatch
    """

    def __init__(self, node_id: str = ""):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._stats = {"escalations": 0, "decisions_received": 0, "errors": 0}

    def escalate_no_consensus(
        self,
        surgeon_decision: SurgeonDecisionPacket,
        task_id: str = "",
        probe_findings: list[str] = None,
        evidence_refs: list[str] = None,
    ) -> ChiefEscalationPacket:
        """Build an escalation packet when surgeons cannot agree.

        Args:
            surgeon_decision: The SurgeonDecisionPacket with disagreement.
            task_id: The task being escalated.
            probe_findings: Any probe findings relevant to the decision.
            evidence_refs: Evidence ledger references.
        """
        # Determine urgency from disagreement intensity
        objection_count = len(surgeon_decision.unresolved_objections)
        urgency = "medium"
        if objection_count >= 3:
            urgency = "high"
        elif objection_count >= 5:
            urgency = "critical"

        packet = ChiefEscalationPacket(
            task_id=task_id or surgeon_decision.task_id,
            from_node=self._node_id,
            reason="no_consensus",
            surgeon_decision_id=surgeon_decision.id,
            brainstorm_id=surgeon_decision.brainstorm_id,
            evidence_refs=evidence_refs or [],
            probe_findings=probe_findings or [],
            urgency=urgency,
        )

        self._stats["escalations"] += 1

        # Emit event
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(
                EventType.SURGEON_CONSENSUS_BLOCKED,
                EventSource.THREE_SURGEONS,
                payload={
                    "escalation_id": packet.id,
                    "reason": packet.reason,
                    "urgency": packet.urgency,
                    "objection_count": objection_count,
                },
                task_id=packet.task_id,
            )
        except Exception as e:
            logger.debug("Escalation event emit failed: %s", e)

        logger.info(
            "escalation built: task=%s reason=%s urgency=%s objections=%d",
            packet.task_id[:8], packet.reason, packet.urgency, objection_count,
        )
        return packet

    def escalate_high_risk(
        self,
        task_id: str,
        risk_description: str,
        probe_findings: list[str] = None,
        surgeon_decision_id: str = "",
    ) -> ChiefEscalationPacket:
        """Build an escalation for high-risk changes detected by probes."""
        packet = ChiefEscalationPacket(
            task_id=task_id,
            from_node=self._node_id,
            reason="high_risk",
            surgeon_decision_id=surgeon_decision_id,
            probe_findings=probe_findings or [risk_description],
            urgency="high",
        )
        self._stats["escalations"] += 1
        logger.info("high_risk escalation: task=%s", task_id[:8])
        return packet

    def receive_decision(
        self,
        decision: ChiefDecisionPacket,
    ) -> dict:
        """Process a chief decision received in response to an escalation.

        Returns a summary dict for the caller to act on.
        """
        self._stats["decisions_received"] += 1

        result = {
            "decision": decision.decision,
            "reasoning": decision.reasoning,
            "memory_refs": decision.memory_refs,
            "modifications": decision.modifications,
            "action_required": decision.decision != "approve",
        }

        logger.info(
            "chief_decision received: escalation=%s decision=%s",
            decision.escalation_id[:8], decision.decision,
        )
        return result

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_adapter: Optional[SurgeonEscalationAdapter] = None


def get_surgeon_escalation() -> SurgeonEscalationAdapter:
    global _adapter
    if _adapter is None:
        _adapter = SurgeonEscalationAdapter()
    return _adapter
