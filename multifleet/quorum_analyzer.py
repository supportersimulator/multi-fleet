"""Quorum Analyzer — pluggable analyzer that wires quorum sensing into chief synthesis.

Connects the quorum system (signal_class, stress_score, quorum_registry,
node_reputation) to the chief synthesis engine. Three responsibilities:

1. QuorumAnalyzer: queries quorum state during synthesis (analyzer interface)
2. Synthesis -> Quorum feedback: decisions create/support quorum proposals
3. Stress-aware gating: high stress requires quorum consensus before accepting

Biology analogy: this is the Las/Rhl cross-link. In P. aeruginosa,
Las activates Rhl which feeds back into Las. Chief synthesis (Las-like
strategic authority) and quorum sensing (population density threshold)
become bidirectionally coupled.
"""

import hashlib
import logging
import os
import time
from typing import Optional

from multifleet.synthesis import SynthesisResult
from multifleet.quorum.signal_class import SignalClass, classify_message
from multifleet.quorum.stress_score import (
    compute_stress_score,
    get_stress_level,
    apply_stress_policy,
    STRESS_CRITICAL,
    STRESS_DEGRADED,
)
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.node_reputation import compute_node_reputation

logger = logging.getLogger("fleet-nerve.quorum-analyzer")


class QuorumAnalyzer:
    """Pluggable analyzer for ChiefSynthesis that consults quorum state.

    Registered via chief.register_analyzer(analyzer), where analyzer
    is a callable(task_id, verdicts) -> SynthesisResult | None.

    The analyzer does NOT replace the default confidence scorer. It runs
    alongside other analyzers and the best (highest confidence) wins.
    Its value is injecting quorum context into the synthesis decision.
    """

    def __init__(
        self,
        registry: Optional[QuorumRegistry] = None,
        node_id: Optional[str] = None,
        stress_kwargs: Optional[dict] = None,
    ):
        self.registry = registry or QuorumRegistry()
        self.node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "chief")
        self._stress_kwargs = stress_kwargs or {}

    def __call__(self, task_id: str, verdicts: dict[str, dict]) -> Optional[SynthesisResult]:
        """Analyzer interface: (task_id, verdicts) -> SynthesisResult | None."""
        analysis = self.analyze(task_id, verdicts)

        # If stress is critical and no quorum consensus exists, gate the decision
        if analysis["stress_level"] == STRESS_CRITICAL and not analysis["has_quorum_consensus"]:
            return SynthesisResult(
                task_id=task_id,
                decision="reject",
                winner_branch=None,
                confidence=0.0,
                reasoning=(
                    f"Stress-gated: stress={analysis['stress_score']:.2f} (critical), "
                    f"no quorum consensus. Requires fleet quorum before synthesis can proceed."
                ),
                metadata={"quorum_analysis": analysis},
            )

        # Otherwise return None — let other analyzers (or default) handle scoring.
        # Quorum context is attached via post-synthesis feedback loop instead.
        return None

    def analyze(self, task_id: str, verdicts: dict[str, dict]) -> dict:
        """Produce structured quorum analysis for a synthesis task.

        Returns:
            {
                proposals_relevant: list of relevant proposal dicts,
                has_quorum_consensus: bool,
                stress_score: float,
                stress_level: str,
                stress_policy: dict,
                node_reputations: {node_id: float},
                recommendation: str,
            }
        """
        # Find relevant proposals by matching task_id against proposal topics
        pending = self.registry.pending_proposals()
        relevant = [
            p for p in pending
            if _topic_matches(task_id, p.get("topic", ""))
        ]

        # Check if any relevant proposal has reached quorum
        has_quorum = any(
            self.registry.has_quorum(p["proposal_id"]) for p in relevant
        )

        # Compute stress
        stress = compute_stress_score(**self._stress_kwargs)
        stress_level = get_stress_level(stress)
        policy = apply_stress_policy(stress)

        # Compute node reputations from verdicts
        reputations = {}
        for machine_id, verdict in verdicts.items():
            rep = compute_node_reputation(
                successful_tasks=verdict.get("successful_tasks", 0),
                failed_tasks=verdict.get("failed_tasks", 0),
                avg_chief_sync_age_sec=verdict.get("chief_sync_age", 0.0),
                p3plus_usage_ratio=verdict.get("p3plus_ratio", 0.0),
                context_quality=verdict.get("context_quality", 1.0),
            )
            reputations[machine_id] = round(rep, 4)

        # Build recommendation
        recommendation = _build_recommendation(
            stress_level, has_quorum, len(relevant), reputations
        )

        return {
            "proposals_relevant": relevant,
            "has_quorum_consensus": has_quorum,
            "stress_score": round(stress, 4),
            "stress_level": stress_level,
            "stress_policy": policy,
            "node_reputations": reputations,
            "recommendation": recommendation,
        }

    __name__ = "quorum_analyzer"  # For logging in ChiefSynthesis


def apply_synthesis_to_quorum(
    result: SynthesisResult,
    registry: QuorumRegistry,
    node_id: Optional[str] = None,
) -> dict:
    """Feedback loop: synthesis decision -> quorum system.

    After chief synthesis produces a decision:
    1. Classify the decision using signal_class
    2. If strategic (Las): record as a strategic proposal
    3. If high confidence: auto-support from chief node
    4. Returns metadata about what was recorded

    Args:
        result: The SynthesisResult from chief synthesis.
        registry: The QuorumRegistry to record proposals in.
        node_id: The chief node ID.

    Returns:
        Dict with feedback actions taken.
    """
    node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "chief")

    # Classify the decision
    signal = classify_message(
        "chief.decision",
        payload={"decision": result.decision, "confidence": result.confidence},
    )

    feedback = {
        "signal_class": signal.value,
        "proposal_created": False,
        "auto_supported": False,
        "proposal_id": None,
    }

    # Only strategic (Las-like) decisions become quorum proposals
    if signal != SignalClass.STRATEGIC:
        return feedback

    # Create a proposal from the synthesis decision
    proposal_id = _decision_proposal_id(result)
    topic = f"synthesis:{result.task_id} decision:{result.decision}"

    registry.record_support(proposal_id, topic, node_id)
    feedback["proposal_created"] = True
    feedback["proposal_id"] = proposal_id

    # High confidence -> chief auto-confirms
    if result.confidence >= 0.7:
        registry.record_chief_confirmation(proposal_id)
        feedback["auto_supported"] = True

    return feedback


def enrich_result_with_quorum(
    result: SynthesisResult,
    registry: QuorumRegistry,
    stress_kwargs: Optional[dict] = None,
) -> SynthesisResult:
    """Post-synthesis enrichment: attach quorum context and apply stress gating.

    Called after synthesis produces a result but before it's logged.
    Modifies result in-place and returns it.

    Stress thresholds:
        >= 0.75 (critical): require quorum consensus, reject if missing
        >= 0.50 (degraded): add warning to reasoning, reduce confidence
        < 0.50 (normal/guarded): proceed normally, attach context only
    """
    stress_kw = stress_kwargs or {}
    stress = compute_stress_score(**stress_kw)
    stress_level = get_stress_level(stress)

    # Attach quorum metadata
    result.metadata["quorum_stress"] = {
        "score": round(stress, 4),
        "level": stress_level,
    }

    # Check ALL proposals (pending + quorum-reached) for relevance
    # pending_proposals() excludes quorum-reached ones, so also check directly
    all_proposal_ids = list(registry._proposals.keys())
    relevant_ids = [
        pid for pid in all_proposal_ids
        if _topic_matches(result.task_id, registry._proposals[pid].topic)
    ]
    has_quorum = any(registry.has_quorum(pid) for pid in relevant_ids)

    result.metadata["quorum_proposals"] = {
        "relevant_count": len(relevant_ids),
        "has_consensus": has_quorum,
    }

    # Stress-aware gating
    if stress_level == STRESS_CRITICAL and not has_quorum:
        result.decision = "reject"
        result.confidence = 0.0
        result.reasoning = (
            f"**Stress-gated (critical, score={stress:.2f})**: "
            f"Quorum consensus required but not reached. "
            f"Original: {result.reasoning}"
        )
    elif stress_level == STRESS_DEGRADED:
        penalty = 0.1
        result.confidence = max(0.0, result.confidence - penalty)
        result.reasoning = (
            f"**Stress warning (degraded, score={stress:.2f})**: "
            f"Fleet under stress, confidence reduced by {penalty}. "
            f"Original: {result.reasoning}"
        )

    return result


# ── Helpers ──


def _topic_matches(task_id: str, topic: str) -> bool:
    """Check if a task_id is relevant to a proposal topic.

    Simple substring match. Could be upgraded to semantic matching.
    """
    task_lower = task_id.lower()
    topic_lower = topic.lower()
    return task_lower in topic_lower or topic_lower in task_lower


def _decision_proposal_id(result: SynthesisResult) -> str:
    """Generate a deterministic proposal ID from a synthesis result."""
    key = f"{result.task_id}:{result.decision}:{result.winner_branch or ''}"
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"synthesis-{h}"


def _build_recommendation(
    stress_level: str,
    has_quorum: bool,
    relevant_count: int,
    reputations: dict[str, float],
) -> str:
    """Build a human-readable recommendation string."""
    parts = []

    if stress_level == STRESS_CRITICAL:
        parts.append("CRITICAL stress: require quorum consensus before proceeding")
    elif stress_level == STRESS_DEGRADED:
        parts.append("DEGRADED stress: proceed with caution, prefer proven paths")

    if has_quorum:
        parts.append("Quorum consensus reached on relevant proposals")
    elif relevant_count > 0:
        parts.append(f"{relevant_count} relevant proposal(s) pending quorum")

    # Flag low-reputation nodes
    low_rep = [nid for nid, rep in reputations.items() if rep < 0.3]
    if low_rep:
        parts.append(f"Low-reputation nodes: {', '.join(low_rep)}")

    return "; ".join(parts) if parts else "Normal operation, no quorum concerns"


def create_quorum_analyzer(
    registry: Optional[QuorumRegistry] = None,
    node_id: Optional[str] = None,
    stress_kwargs: Optional[dict] = None,
) -> QuorumAnalyzer:
    """Factory function matching the pattern used by contextdna_analyzer."""
    return QuorumAnalyzer(
        registry=registry,
        node_id=node_id,
        stress_kwargs=stress_kwargs,
    )
