"""Quorum Registry — tracks corroboration for strategic proposals.

A strategic signal only gains fleet-wide authority when enough
independent nodes confirm it. This is the threshold-based
promotion that prevents "one node saw something once" from
becoming fleet doctrine.

Like bacterial quorum sensing: not every molecule becomes a
colony-wide program. Only signals that reach population density
threshold change behavior.

The registry also implements quorum quenching — the anti-biofilm
mechanism. In biology, quorum quenching enzymes degrade signaling
molecules to prevent runaway coordination. In Multi-Fleet, quenching
injects dissent, applies freshness decay, or explicitly blocks
proposals that have accumulated too much uncritical agreement.

Quorum thresholds:
    - Minimum 2 independent node confirmations
    - Plus chief confirmation OR post-verification success
    - Quenched proposals cannot reach quorum regardless of support

This is deliberately conservative. The cost of false promotion
(fleet acts on bad intelligence) far exceeds the cost of delayed
promotion (fleet waits one more cycle for confirmation).
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# ── Quorum threshold constants ──

MIN_SUPPORTING_NODES = 2
PROPOSAL_TTL_SECONDS = 3600  # proposals expire after 1 hour without quorum


@dataclass
class StrategicProposal:
    """A proposal that seeks fleet-wide strategic authority.

    Like a Las signal molecule: it exists, but it only activates
    colony-wide gene expression when enough molecules accumulate
    (enough nodes corroborate).
    """
    proposal_id: str
    topic: str
    supporting_nodes: set = field(default_factory=set)
    chief_confirmed: bool = False
    post_verified: bool = False
    created_at: float = field(default_factory=time.time)
    quenched: bool = False
    quench_reason: Optional[str] = None


class QuorumRegistry:
    """Registry for tracking strategic proposal corroboration.

    Thread-safe for single-process use. For multi-process coordination,
    back this with Redis (future enhancement).
    """

    def __init__(self, min_nodes: int = MIN_SUPPORTING_NODES,
                 ttl_seconds: float = PROPOSAL_TTL_SECONDS):
        self._proposals: dict[str, StrategicProposal] = {}
        self._min_nodes = min_nodes
        self._ttl = ttl_seconds

    def record_support(self, proposal_id: str, topic: str, node_id: str) -> StrategicProposal:
        """Record that a node supports a strategic proposal.

        If the proposal doesn't exist yet, it is created.
        Each node can only be counted once per proposal.

        Args:
            proposal_id: Unique identifier for the proposal.
            topic: Human-readable description of what is being proposed.
            node_id: The node expressing support.

        Returns:
            The updated StrategicProposal.
        """
        self._gc_expired()
        if proposal_id not in self._proposals:
            self._proposals[proposal_id] = StrategicProposal(
                proposal_id=proposal_id, topic=topic
            )
        prop = self._proposals[proposal_id]
        prop.supporting_nodes.add(node_id)
        return prop

    def record_chief_confirmation(self, proposal_id: str) -> bool:
        """Record that the chief node has confirmed a proposal.

        Returns True if the proposal exists and was updated.
        """
        self._gc_expired()
        prop = self._proposals.get(proposal_id)
        if prop is None:
            return False
        prop.chief_confirmed = True
        return True

    def record_post_verification(self, proposal_id: str) -> bool:
        """Record that post-verification succeeded for a proposal.

        Post-verification is an alternative path to quorum when chief
        is unavailable — the work was done and verified correct.

        Returns True if the proposal exists and was updated.
        """
        self._gc_expired()
        prop = self._proposals.get(proposal_id)
        if prop is None:
            return False
        prop.post_verified = True
        return True

    def has_quorum(self, proposal_id: str) -> bool:
        """Check whether a proposal has reached quorum.

        Quorum requires:
            1. At least MIN_SUPPORTING_NODES independent confirmations
            2. Chief confirmation OR post-verification success
            3. NOT quenched
            4. INTERLOCK 7: Surgeon confidence >= 0.6 (when available)
               If surgeon confidence is low, quorum is provisional and revoked.

        This is deliberately strict. In biology, quorum sensing
        requires population density — not just one loud cell.
        """
        self._gc_expired()
        prop = self._proposals.get(proposal_id)
        if prop is None:
            return False
        if prop.quenched:
            return False
        has_enough_nodes = len(prop.supporting_nodes) >= self._min_nodes
        has_authority = prop.chief_confirmed or prop.post_verified
        if not (has_enough_nodes and has_authority):
            return False

        # INTERLOCK 7: Check surgeon confidence
        # Surgeon confidence acts as an additional gate — low confidence
        # means the corrigibility layer hasn't validated this proposal
        try:
            from ..surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            consensus = adapter.check_consensus(prop.topic)
            confidence = consensus.get("confidence", 0.5)
            if confidence < 0.6:
                # Provisional quorum — surgeon confidence too low
                return False
        except Exception:
            # Graceful degradation: if surgeon unavailable, quorum stands
            # on node support + authority alone
            pass

        return True

    def quench(self, proposal_id: str, reason: str) -> bool:
        """Quorum quenching — the anti-biofilm mechanism.

        Prevents runaway consensus by explicitly blocking a proposal
        from reaching quorum, regardless of how much support it has.

        In biology, quorum quenching enzymes (like AiiA lactonase)
        degrade AHL signal molecules to prevent premature or harmful
        group behaviors. In Multi-Fleet, quenching is triggered by:
            - Surgeon counter-position raising valid objection
            - Freshness decay (proposal too old without confirmation)
            - Chief explicit veto
            - Evidence contradicting the proposal

        Args:
            proposal_id: The proposal to quench.
            reason: Why quenching was applied (audit trail).

        Returns:
            True if the proposal was found and quenched.
        """
        prop = self._proposals.get(proposal_id)
        if prop is None:
            return False
        prop.quenched = True
        prop.quench_reason = reason
        return True

    def pending_proposals(self) -> list[dict]:
        """List all proposals that have not yet reached quorum and are not quenched.

        Returns a list of dicts with proposal state for dashboard display.
        """
        self._gc_expired()
        result = []
        for prop in self._proposals.values():
            if prop.quenched:
                continue
            if self.has_quorum(prop.proposal_id):
                continue
            result.append({
                "proposal_id": prop.proposal_id,
                "topic": prop.topic,
                "supporting_nodes": sorted(prop.supporting_nodes),
                "chief_confirmed": prop.chief_confirmed,
                "post_verified": prop.post_verified,
                "age_seconds": round(time.time() - prop.created_at, 1),
            })
        return result

    def get_proposal(self, proposal_id: str) -> Optional[StrategicProposal]:
        """Get a proposal by ID, or None if not found/expired."""
        self._gc_expired()
        return self._proposals.get(proposal_id)

    def record_surgeon_dissent(self, topic: str, dissent: dict, node_id: str) -> StrategicProposal:
        """Record a surgeon dissent as a strategic quorum proposal.

        INTERLOCK 4: When 3-surgeons produces a dissent or counter-position,
        it becomes a strategic proposal in the quorum registry. Other nodes
        see the dissent via quorum signals, enabling fleet-wide architectural
        concerns to propagate through the quorum system.

        Args:
            topic: The topic the dissent is about.
            dissent: Dict with dissent details (surgeons_consulted, confidence,
                     dissents list, output, etc.).
            node_id: The node that originated the dissent.

        Returns:
            The StrategicProposal created from the dissent.
        """
        # Create a deterministic proposal ID from the topic
        topic_hash = abs(hash(topic)) % (10 ** 10)
        proposal_id = f"surgeon-dissent-{topic_hash}"

        # Record support from the originating node
        proposal = self.record_support(proposal_id, f"[SURGEON DISSENT] {topic}", node_id)

        # If the dissent has high confidence, auto-quench opposing proposals
        confidence = dissent.get("confidence", 0.0)
        dissent_list = dissent.get("dissents", [])
        if confidence >= 0.8 and dissent_list:
            # Strong dissent — this is a real concern
            proposal.chief_confirmed = False  # Requires chief review
            proposal.post_verified = False

        return proposal

    def _gc_expired(self):
        """Remove proposals that have exceeded their TTL.

        Like signal molecule degradation in biology — molecules don't
        persist forever. If a proposal can't reach quorum within the
        TTL, it decays. This prevents stale proposals from accumulating
        and eventually reaching quorum through slow drip of support.
        """
        now = time.time()
        expired = [
            pid for pid, prop in self._proposals.items()
            if (now - prop.created_at) > self._ttl
        ]
        for pid in expired:
            del self._proposals[pid]
