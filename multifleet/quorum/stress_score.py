"""Stress Score — the IQS-like environmental pressure sensor.

Computes fleet stress from observable degradation signals:
    - fallback channel usage (P3+ means ideal channels are broken)
    - stale peers (heartbeat age beyond threshold)
    - chief sync failures (coordination layer degraded)
    - surgeon deadlocks (corrigibility layer stuck)
    - context staleness (memory layer degraded)

When stress rises, behavior changes automatically:
    0.00-0.24  NORMAL    full autonomy, speculative work allowed
    0.25-0.49  GUARDED   reduce speculative work, prefer proven paths
    0.50-0.74  DEGRADED  prefer DAO guidance, no irreversible actions
    0.75-1.00  CRITICAL  chief review required, compress packets, survival mode

This is the IQS-like layer. In P. aeruginosa, IQS activates under
phosphate limitation and shifts which signaling system dominates.
In Multi-Fleet, stress shifts the hierarchy: under critical stress,
tactical autonomy yields to strategic (chief) authority.

The deepest biological lesson: hierarchy is not rigid. It is
environmentally responsive. The system that dominates at rest
is not necessarily the system that dominates under pressure.
"""

import logging
from typing import Optional

logger = logging.getLogger("fleet-nerve.stress")


# ── Stress level constants ──

STRESS_NORMAL = "normal"
STRESS_GUARDED = "guarded"
STRESS_DEGRADED = "degraded"
STRESS_CRITICAL = "critical"

# ── Thresholds ──

_LEVEL_THRESHOLDS = [
    (0.75, STRESS_CRITICAL),
    (0.50, STRESS_DEGRADED),
    (0.25, STRESS_GUARDED),
    (0.00, STRESS_NORMAL),
]

# ── Contribution weights and caps ──
# Each stress factor has a per-unit weight and a maximum contribution.
# This prevents any single factor from dominating the score.
# The caps ensure the score stays meaningful: even if NATS is completely
# dead (high fallback count), the score won't hit critical unless
# multiple systems are degraded simultaneously.

_FALLBACK_WEIGHT = 0.10
_FALLBACK_CAP = 0.30

_STALE_PEER_WEIGHT = 0.10
_STALE_PEER_CAP = 0.20

_CHIEF_SYNC_WEIGHT = 0.15
_CHIEF_SYNC_CAP = 0.25

_DEADLOCK_WEIGHT = 0.10
_DEADLOCK_CAP = 0.15

_STALENESS_WEIGHT = 0.10
_STALENESS_CAP = 0.10

# INTERLOCK 5: Surgeon availability contributes to stress score
_SURGEON_UNAVAILABLE_PENALTY = 0.15


def compute_stress_score(
    fallback_p3plus_count: int = 0,
    stale_peers: int = 0,
    chief_sync_failures: int = 0,
    surgeon_deadlocks: int = 0,
    context_staleness: float = 0.0,
    surgeon_available: bool = True,
) -> float:
    """Compute fleet stress score from observable degradation signals.

    Args:
        fallback_p3plus_count: Number of recent messages that required P3+ fallback.
            Each P3+ usage means NATS and HTTP failed — the fleet is using
            chief relay, seed files, SSH, or git. More fallbacks = more stress.
        stale_peers: Number of peers whose heartbeat is beyond the staleness
            threshold (default 300s). Stale peers can't participate in quorum.
        chief_sync_failures: Number of recent chief sync attempts that failed.
            Chief sync is how strategic truth propagates — failures here mean
            the Las-like layer is degraded.
        surgeon_deadlocks: Number of recent 3-surgeon consensus attempts that
            timed out or deadlocked. This means the corrigibility layer is stuck.
        context_staleness: How stale the local context is, normalized to 0-1.
            1.0 means context is critically outdated. This affects the quality
            of every decision the node makes.
        surgeon_available: Whether the 3-surgeon protocol is reachable.
            INTERLOCK 5: Surgeon availability IS infrastructure health.
            When surgeon is unavailable, the fleet loses its corrigibility
            layer — stress increases by 0.15.

    Returns:
        Float in [0.0, 1.0] representing fleet stress level.
    """
    score = 0.0
    score += min(fallback_p3plus_count * _FALLBACK_WEIGHT, _FALLBACK_CAP)
    score += min(stale_peers * _STALE_PEER_WEIGHT, _STALE_PEER_CAP)
    score += min(chief_sync_failures * _CHIEF_SYNC_WEIGHT, _CHIEF_SYNC_CAP)
    score += min(surgeon_deadlocks * _DEADLOCK_WEIGHT, _DEADLOCK_CAP)
    score += min(context_staleness * _STALENESS_WEIGHT, _STALENESS_CAP)
    # INTERLOCK 5: Surgeon unavailability adds stress
    if not surgeon_available:
        score += _SURGEON_UNAVAILABLE_PENALTY
    return min(score, 1.0)


def get_stress_level(score: float) -> str:
    """Convert a numeric stress score to a named level.

    Returns one of: "normal", "guarded", "degraded", "critical".
    """
    for threshold, level in _LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return STRESS_NORMAL


def apply_stress_policy(score: float) -> dict:
    """Derive behavioral policy from stress score.

    Returns a dict of policy flags that gates, dispatchers, and buses
    should consult before acting. The policy progressively restricts
    autonomy as stress rises — this is the IQS-like hierarchy shift.

    Policy fields:
        allow_speculative_work: Can nodes start exploratory/brainstorm tasks?
        require_chief_review: Must chief approve before execution?
        prefer_dao_guidance: Should DAO-backed paths be preferred over novel ones?
        max_packet_size: How much context to include in packets?
        allow_irreversible_actions: Can nodes take irreversible actions?
        stress_level: The named level string for logging/display.
    """
    level = get_stress_level(score)

    if level == STRESS_CRITICAL:
        return {
            "allow_speculative_work": False,
            "require_chief_review": True,
            "prefer_dao_guidance": True,
            "max_packet_size": "small",
            "allow_irreversible_actions": False,
            "stress_level": STRESS_CRITICAL,
        }
    if level == STRESS_DEGRADED:
        return {
            "allow_speculative_work": False,
            "require_chief_review": False,
            "prefer_dao_guidance": True,
            "max_packet_size": "medium",
            "allow_irreversible_actions": False,
            "stress_level": STRESS_DEGRADED,
        }
    if level == STRESS_GUARDED:
        return {
            "allow_speculative_work": True,
            "require_chief_review": False,
            "prefer_dao_guidance": False,
            "max_packet_size": "medium",
            "allow_irreversible_actions": True,
            "stress_level": STRESS_GUARDED,
        }
    # NORMAL
    return {
        "allow_speculative_work": True,
        "require_chief_review": False,
        "prefer_dao_guidance": False,
        "max_packet_size": "large",
        "allow_irreversible_actions": True,
        "stress_level": STRESS_NORMAL,
    }


def emit_stress_event(
    previous_score: float,
    current_score: float,
    node_id: str = "unknown",
) -> Optional[dict]:
    """Emit event when stress level transitions, especially into critical.

    INTERLOCK 5: When stress crosses into CRITICAL, emits an event that
    downstream listeners (gates, surgeon adapter) can act on. This is
    the bridge between the environmental pressure sensor and the
    corrigibility layer.

    Args:
        previous_score: Stress score before the change.
        current_score: Stress score after the change.
        node_id: Which node is reporting.

    Returns:
        Event dict if a level transition occurred, None otherwise.
    """
    prev_level = get_stress_level(previous_score)
    curr_level = get_stress_level(current_score)

    if prev_level == curr_level:
        return None

    event = {
        "type": "stress_level_change",
        "from_level": prev_level,
        "to_level": curr_level,
        "previous_score": round(previous_score, 3),
        "current_score": round(current_score, 3),
        "node_id": node_id,
        "requires_surgeon": curr_level == STRESS_CRITICAL,
    }

    # Best-effort event bus emission
    try:
        from ..event_bus import IDEEventBus
        bus = IDEEventBus.get()
        bus.emit("stress.level_change", event)
        if curr_level == STRESS_CRITICAL:
            bus.emit("stress.critical", event)
            logger.warning(
                "CRITICAL stress on %s (%.2f→%.2f) — surgeon protocol triggered",
                node_id, previous_score, current_score,
            )
    except Exception:
        pass  # Event emission must never break stress computation

    return event


def evaluate_stress_and_trigger_surgeon(
    topic: str,
    fallback_p3plus_count: int = 0,
    stale_peers: int = 0,
    chief_sync_failures: int = 0,
    surgeon_deadlocks: int = 0,
    context_staleness: float = 0.0,
) -> dict:
    """Compute stress and trigger surgeon protocol if critical.

    INTERLOCK 5: When stress crosses into CRITICAL, automatically
    triggers a surgeon cross-exam on the given topic. This bridges
    the environmental pressure sensor to the corrigibility layer.

    Args:
        topic: Description of what's being evaluated.
        (remaining args: same as compute_stress_score)

    Returns:
        {stress_score, stress_level, surgeon_triggered, surgeon_result}
    """
    # Check surgeon availability first
    surgeon_available = True
    try:
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        surgeon_available = adapter.is_available()
    except Exception:
        surgeon_available = False

    score = compute_stress_score(
        fallback_p3plus_count=fallback_p3plus_count,
        stale_peers=stale_peers,
        chief_sync_failures=chief_sync_failures,
        surgeon_deadlocks=surgeon_deadlocks,
        context_staleness=context_staleness,
        surgeon_available=surgeon_available,
    )
    level = get_stress_level(score)

    result = {
        "stress_score": score,
        "stress_level": level,
        "surgeon_triggered": False,
        "surgeon_result": None,
    }

    if level != STRESS_CRITICAL:
        return result

    # Critical stress -> trigger surgeon cross-exam
    try:
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        cross_exam = adapter.request_cross_exam(
            topic=f"CRITICAL STRESS ({score:.2f}): {topic}",
            evidence={
                "stress_score": f"{score:.2f}",
                "stress_level": level,
                "fallback_p3plus_count": str(fallback_p3plus_count),
                "stale_peers": str(stale_peers),
                "chief_sync_failures": str(chief_sync_failures),
            },
        )
        result["surgeon_triggered"] = True
        result["surgeon_result"] = cross_exam
    except Exception as exc:
        logger.warning("Surgeon cross-exam failed during critical stress: %s", exc)
        result["surgeon_triggered"] = False
        result["surgeon_result"] = {"error": str(exc)}

    return result
