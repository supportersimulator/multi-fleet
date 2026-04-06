"""ContextDNA memory-backed synthesis analyzer.

Registered as a pluggable analyzer in ChiefSynthesis. Compares fleet
verdicts against ContextDNA's evidence store, prior learnings, and
architectural decisions to produce evidence-informed merge recommendations.

This is the bridge between multi-fleet (transport) and ContextDNA (memory).
Per the architecture decision: multi-fleet provides the framework,
ContextDNA provides the semantic intelligence.
"""

import logging
from typing import Optional

logger = logging.getLogger("fleet-nerve.contextdna-analyzer")

# Memory boost/penalty constants
PRIOR_SUCCESS_BOOST = 0.10
PRIOR_FAILURE_PENALTY = 0.15
ARCH_DECISION_BOOST = 0.05
NO_MEMORY_MATCH = 0.0

# Keywords signalling prior failures in memory results
_FAILURE_KEYWORDS = frozenset({
    "fail", "broke", "regression", "revert", "rollback", "bug",
    "crash", "error", "never", "don't", "avoid", "wrong",
})

# Keywords signalling prior successes
_SUCCESS_KEYWORDS = frozenset({
    "success", "works", "fixed", "solved", "improved", "confirmed",
    "pass", "healthy", "stable", "verified",
})


def _score_memory_text(text: str) -> float:
    """Score a memory string: positive = success signal, negative = failure signal."""
    if not text:
        return NO_MEMORY_MATCH
    words = set(text.lower().split())
    fail_hits = len(words & _FAILURE_KEYWORDS)
    success_hits = len(words & _SUCCESS_KEYWORDS)
    if fail_hits > success_hits:
        return -PRIOR_FAILURE_PENALTY
    if success_hits > fail_hits:
        return PRIOR_SUCCESS_BOOST
    return NO_MEMORY_MATCH


def _build_memory_reasons(verdict_adjustments: dict) -> list:
    """Build human-readable list of memory-based reasoning."""
    reasons = []
    for mid, adj in verdict_adjustments.items():
        delta = adj["delta"]
        if delta > 0:
            reasons.append(f"{mid}: +{delta:.2f} (prior success patterns)")
        elif delta < 0:
            reasons.append(f"{mid}: {delta:.2f} (prior failure patterns)")
    return reasons


def create_contextdna_analyzer():
    """Create a ContextDNA-backed analyzer for ChiefSynthesis.

    Returns a callable that accepts (task_id, verdicts) and returns
    SynthesisResult or None. Gracefully degrades if ContextDNA is not
    available.
    """
    # Try to import ContextDNA memory modules
    query_fn = None

    try:
        from memory.query import query_learnings
        query_fn = query_learnings
    except Exception:
        pass

    if query_fn is None:
        try:
            from memory.brain import Brain
            brain = Brain()
            query_fn = lambda q, **kw: brain.context(q)  # noqa: E731
        except Exception:
            pass

    if query_fn is None:
        logger.info("ContextDNA memory not available — using confidence-only scoring")
        return None

    logger.info("ContextDNA memory available — analyzer ready")

    def analyzer(task_id: str, verdicts: dict) -> Optional["SynthesisResult"]:
        """Compare verdicts against ContextDNA memory for evidence-backed decisions."""
        from multifleet.synthesis import SynthesisResult

        if not verdicts:
            return None

        # Collect adjustments per machine
        adjustments: dict[str, dict] = {}

        for mid, v in verdicts.items():
            summary = v.get("summary", "")
            files_touched = v.get("files_touched", [])

            # Build query from summary + files
            query_parts = [summary] if summary else []
            if files_touched:
                query_parts.append(" ".join(files_touched[:5]))
            query_text = " ".join(query_parts).strip()

            if not query_text:
                adjustments[mid] = {"delta": NO_MEMORY_MATCH, "reason": "no queryable content"}
                continue

            # Query memory
            try:
                memory_results = query_fn(query_text, limit=5)
            except Exception as e:
                logger.warning("Memory query failed for %s: %s", mid, e)
                adjustments[mid] = {"delta": NO_MEMORY_MATCH, "reason": f"query error: {e}"}
                continue

            if not memory_results:
                adjustments[mid] = {"delta": NO_MEMORY_MATCH, "reason": "no memory matches"}
                continue

            # Score memory results
            combined_text = " ".join(
                r if isinstance(r, str) else str(r) for r in memory_results
            )
            delta = _score_memory_text(combined_text)
            adjustments[mid] = {
                "delta": delta,
                "reason": combined_text[:200],
                "match_count": len(memory_results),
            }

        # If no adjustments have non-zero delta, let the default analyzer handle it
        if all(a["delta"] == NO_MEMORY_MATCH for a in adjustments.values()):
            return None

        # Re-score verdicts with memory adjustments
        scores: dict[str, float] = {}
        for mid, v in verdicts.items():
            base_confidence = float(v.get("confidence", 0.0))
            risks = v.get("risks", [])
            dissents = v.get("dissent", [])

            risk_penalty = len(risks) * 0.05
            dissent_penalty = len(dissents) * 0.1
            memory_delta = adjustments.get(mid, {}).get("delta", 0.0)

            scores[mid] = base_confidence - risk_penalty - dissent_penalty + memory_delta

        # Winner = highest score
        winner_id = max(scores, key=scores.get)  # type: ignore[arg-type]
        winner_verdict = verdicts[winner_id]
        winner_score = max(0.0, min(1.0, scores[winner_id]))

        # Collect dissent
        all_dissent: list[dict] = []
        for mid, v in verdicts.items():
            for d in v.get("dissent", []):
                entry = dict(d) if isinstance(d, dict) else {"claim": str(d)}
                entry.setdefault("machine", mid)
                all_dissent.append(entry)

        # Cherry-pick from non-winners
        cherry_picks: list[dict] = []
        for mid, v in verdicts.items():
            if mid == winner_id:
                continue
            for s in v.get("strengths", []):
                cherry_picks.append({
                    "machine": mid,
                    "commit": v.get("commit", v.get("branch", "")),
                    "reason": s if isinstance(s, str) else str(s),
                })

        # Decision type
        if len(verdicts) == 1:
            decision = "merge"
        elif cherry_picks:
            decision = "cherry_pick"
        elif len(all_dissent) > len(verdicts):
            decision = "needs_rebuttal"
        else:
            decision = "merge"

        # Build reasoning with memory influence
        memory_reasons = _build_memory_reasons(adjustments)
        score_summary = ", ".join(
            f"{m}: {s:.3f}" for m, s in sorted(scores.items(), key=lambda x: -x[1])
        )
        reasoning = f"Memory-informed scores: [{score_summary}]."
        if memory_reasons:
            reasoning += f" Memory: {'; '.join(memory_reasons)}."

        return SynthesisResult(
            task_id=task_id,
            decision=decision,
            winner_branch=winner_verdict.get("branch", winner_id),
            confidence=winner_score,
            reasoning=reasoning,
            cherry_pick_from=cherry_picks,
            followups=winner_verdict.get("followups", []),
            dissent_preserved=all_dissent,
        )

    return analyzer
