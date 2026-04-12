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

from typing import Optional


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


def compute_stress_score(
    fallback_p3plus_count: int = 0,
    stale_peers: int = 0,
    chief_sync_failures: int = 0,
    surgeon_deadlocks: int = 0,
    context_staleness: float = 0.0,
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

    Returns:
        Float in [0.0, 1.0] representing fleet stress level.
    """
    score = 0.0
    score += min(fallback_p3plus_count * _FALLBACK_WEIGHT, _FALLBACK_CAP)
    score += min(stale_peers * _STALE_PEER_WEIGHT, _STALE_PEER_CAP)
    score += min(chief_sync_failures * _CHIEF_SYNC_WEIGHT, _CHIEF_SYNC_CAP)
    score += min(surgeon_deadlocks * _DEADLOCK_WEIGHT, _DEADLOCK_CAP)
    score += min(context_staleness * _STALENESS_WEIGHT, _STALENESS_CAP)
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
