"""Node Reputation — the PQS-like colony state signal.

Each node has a reputation score based on observable behavior:
    - task success rate (primary signal)
    - chief sync freshness (coordination health)
    - fallback channel usage (transport health)
    - context quality (memory health)

Reputation influences routing: higher reputation nodes are preferred
for important tasks. But reputation does NOT command — it modulates.
This is the PQS distinction: PQS in P. aeruginosa does not directly
activate genes the way Las or Rhl do. Instead, it cross-links the
signaling network and stabilizes colony-wide state. A node with low
reputation isn't banned; it just receives less critical work until
its health improves.

Reputation is computed, not declared. No node can claim high reputation.
It must be earned through consistent behavior visible to the colony.
"""


def compute_node_reputation(
    successful_tasks: int = 0,
    failed_tasks: int = 0,
    avg_chief_sync_age_sec: float = 0.0,
    p3plus_usage_ratio: float = 0.0,
    context_quality: float = 1.0,
) -> float:
    """Compute a node's reputation score from observable metrics.

    Args:
        successful_tasks: Number of tasks completed successfully.
        failed_tasks: Number of tasks that failed or timed out.
        avg_chief_sync_age_sec: Average age of chief sync in seconds.
            Fresh sync (< 60s) = no penalty. Stale sync (> 3600s) = max penalty.
        p3plus_usage_ratio: Fraction of recent messages that required P3+
            fallback channels (0.0 = all ideal, 1.0 = all fallback).
        context_quality: Quality of the node's context/memory, 0.0-1.0.
            1.0 = fresh, complete context. 0.0 = severely degraded.

    Returns:
        Float in [0.0, 1.0] representing node reputation.
        0.8+ = excellent (preferred for strategic tasks)
        0.5-0.8 = adequate (normal task routing)
        0.2-0.5 = degraded (avoid critical tasks)
        <0.2 = poor (healing needed before task assignment)
    """
    # Base: task success rate
    total_tasks = successful_tasks + failed_tasks
    if total_tasks == 0:
        # No track record = neutral reputation (benefit of the doubt)
        success_rate = 0.5
    else:
        success_rate = successful_tasks / total_tasks

    # Freshness penalty: stale chief sync means this node may be
    # operating on outdated strategic truth. Cap at 0.2 penalty.
    # Normalize: 0s = 0 penalty, 3600s+ = full 0.2 penalty.
    freshness_penalty = min(avg_chief_sync_age_sec / 3600.0, 1.0) * 0.2

    # Fallback penalty: heavy P3+ usage means this node's transport
    # layer is degraded. Cap at 0.2 penalty.
    fallback_penalty = min(p3plus_usage_ratio, 1.0) * 0.2

    # Context quality bonus/penalty: good context lifts reputation,
    # poor context drags it down. Range: -0.1 to +0.1.
    context_adjustment = (context_quality - 0.5) * 0.2

    reputation = success_rate - freshness_penalty - fallback_penalty + context_adjustment
    return max(0.0, min(1.0, reputation))
