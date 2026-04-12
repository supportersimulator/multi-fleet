"""Signal Classification — the semantic layer on top of transport.

Every fleet message gets classified into one of 4 signal classes.
This determines its behavioral authority, routing priority, and
promotion requirements.

Inspired by P. aeruginosa quorum sensing:
- Las (strategic): fleet-shaping truth, requires corroboration
- Rhl (tactical): fast execution chatter, immediate flow
- PQS (state): colony-wide context, influences routing
- IQS (stress): environmental override, changes behavior

The classifier does NOT route messages. It annotates them with semantic
meaning so that gates, buses, and policies can treat them differently.
Transport remains unified — meaning becomes layered.
"""

from enum import Enum
from typing import Optional


class SignalClass(Enum):
    """The four quorum-sensing signal classes.

    Each maps to a biological signaling system in P. aeruginosa:
      STRATEGIC = Las-like  : rare, authoritative, requires quorum threshold
      TACTICAL  = Rhl-like  : frequent, local, execution-facing
      STATE     = PQS-like  : contextual broadcast, modulates colony behavior
      STRESS    = IQS-like  : environmental override, shifts hierarchy under pressure
    """
    STRATEGIC = "strategic"
    TACTICAL = "tactical"
    STATE = "state"
    STRESS = "stress"


# ── Classification tables ──
# These sets define which message types map to which signal class.
# Unknown types default to TACTICAL (Rhl-like: assume execution intent).

_STRATEGIC_TYPES = frozenset({
    "chief.decision",
    "chief_decision",
    "dao.promoted",
    "dao_promoted",
    "invariant.updated",
    "invariant_updated",
    "failure.pattern.promoted",
    "failure_pattern_promoted",
    "vision.rerouted",
    "vision_rerouted",
    "quorum.strategic.proposed",
    "quorum.strategic.confirmed",
    "quorum.strategic.promoted",
})

_TACTICAL_TYPES = frozenset({
    "task.dispatch",
    "task_dispatch",
    "task.subtask",
    "task_subtask",
    "execution.phase_changed",
    "execution_phase_changed",
    "execution.blocker",
    "execution_blocker",
    "worker.result",
    "worker_result",
    "fleet.task.created",
    "fleet.task.dispatched",
    "fleet.task.delivered",
    "fleet.repair.requested",
    "fleet.repair.succeeded",
})

_STATE_TYPES = frozenset({
    "node.freshness",
    "node_freshness",
    "node.reputation",
    "node_reputation",
    "transport.degraded",
    "transport_degraded",
    "chief.sync_age",
    "chief_sync_age",
    "surgeon.disagreement",
    "surgeon_disagreement",
    "fleet.node.heartbeat",
    "fleet.node.degraded",
    "fleet.transport.degraded",
    "quorum.state.node_freshness",
    "quorum.state.node_reputation",
    "quorum.state.transport_degraded",
})

_STRESS_TYPES = frozenset({
    "stress.entered",
    "stress_entered",
    "stress.level_changed",
    "stress_level_changed",
    "quota.low",
    "quota_low",
    "chief.sync_failed",
    "chief_sync_failed",
    "fallback.p3plus",
    "fallback_p3plus",
    "quorum.stress.entered",
    "quorum.stress.level_changed",
    "quorum.stress.override_applied",
})


def classify_message(msg_type: str, payload: Optional[dict] = None) -> SignalClass:
    """Classify a fleet message by its semantic authority level.

    Args:
        msg_type: The message type string (e.g., "chief.decision", "task.dispatch").
        payload: Optional payload dict. Currently unused but reserved for
                 future content-based classification (e.g., a tactical message
                 that contains repeated-failure evidence could be reclassified).

    Returns:
        The appropriate SignalClass for this message type.

    The default is TACTICAL — if we don't recognize the type, treat it as
    execution chatter. This is the Rhl-like "keep moving" default, which is
    safe: tactical messages flow immediately and don't claim fleet-wide authority.
    """
    # Payload-based escalation checked FIRST: if a message carries stress
    # or quorum indicators, they override type-based classification.
    # This mirrors how IQS activates under environmental pressure
    # regardless of which system originally produced the signal.
    # Biology: phosphate limitation makes IQS dominate even over Las.
    if payload:
        if payload.get("stress_override"):
            return SignalClass.STRESS
        if payload.get("requires_quorum"):
            return SignalClass.STRATEGIC

    if msg_type in _STRATEGIC_TYPES:
        return SignalClass.STRATEGIC
    if msg_type in _STATE_TYPES:
        return SignalClass.STATE
    if msg_type in _STRESS_TYPES:
        return SignalClass.STRESS
    if msg_type in _TACTICAL_TYPES:
        return SignalClass.TACTICAL

    return SignalClass.TACTICAL
