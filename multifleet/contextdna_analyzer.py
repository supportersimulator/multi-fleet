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


def _query_prior_failures(query_fn, files_touched: list) -> list:
    """Check memory for prior failures related to specific files.

    Returns list of failure descriptions found in memory for these files.
    """
    if not files_touched:
        return []
    failures = []
    # Query with file paths to find file-specific failures
    file_query = " ".join(files_touched[:5]) + " fail error regression"
    try:
        results = query_fn(file_query, limit=3)
        for r in results:
            text = r if isinstance(r, str) else str(r)
            if any(kw in text.lower() for kw in _FAILURE_KEYWORDS):
                failures.append(text[:200])
    except Exception as e:
        logger.debug("Prior failure check failed: %s", e)
    return failures


def _query_arch_decisions(query_fn, summary: str) -> list:
    """Check memory for architectural decisions affecting this approach.

    Returns list of relevant architectural decision descriptions.
    """
    if not summary:
        return []
    decisions = []
    arch_query = summary + " architecture decision"
    try:
        results = query_fn(arch_query, limit=3)
        for r in results:
            text = r if isinstance(r, str) else str(r)
            # Architectural decisions typically reference patterns, decisions, rationale
            if any(kw in text.lower() for kw in (
                "decision", "architecture", "pattern", "approach", "design",
                "rationale", "alternative", "consequence",
            )):
                decisions.append(text[:200])
    except Exception as e:
        logger.debug("Arch decision check failed: %s", e)
    return decisions


def _query_professor(professor_fn, summary: str) -> Optional[str]:
    """Consult the professor for distilled wisdom about this task.

    Returns professor guidance string or None.
    """
    if not summary:
        return None
    try:
        guidance = professor_fn(task=summary)
        if guidance and isinstance(guidance, str) and len(guidance.strip()) > 10:
            return guidance.strip()[:500]
    except Exception as e:
        logger.debug("Professor consult failed: %s", e)
    return None


def create_contextdna_analyzer():
    """Create a ContextDNA-backed analyzer for ChiefSynthesis.

    Returns a callable that accepts (task_id, verdicts) and returns
    SynthesisResult or None. Gracefully degrades if ContextDNA is not
    available.
    """
    # Try to import ContextDNA memory modules
    query_fn = None
    professor_fn = None

    try:
        from memory.query import query_learnings
        query_fn = query_learnings
    except Exception:
        pass

    if query_fn is None:
        try:
            from memory.brain import Brain
            brain = Brain()
            if hasattr(brain, 'context'):
                query_fn = lambda q, **kw: brain.context(q)  # noqa: E731
        except Exception:
            pass

    # Fallback: use evidence ledger as local query source
    if query_fn is None:
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            _ledger = EvidenceLedger()

            def _ledger_query(q, limit=5, **kw):
                """Query evidence ledger subjects/payloads for keyword matches."""
                all_entries = _ledger.query(limit=100)
                keywords = set(q.lower().split())
                matches = []
                for entry in all_entries:
                    text = f"{entry.get('subject', '')} {entry.get('payload', '')}".lower()
                    if any(kw in text for kw in keywords if len(kw) > 2):
                        matches.append(entry.get('subject', str(entry.get('payload', '')))[:300])
                return matches[:limit]

            query_fn = _ledger_query
            logger.info("Using evidence ledger as local memory fallback")
        except Exception:
            pass

    # Try to import professor for distilled wisdom
    try:
        from memory.professor import consult as _consult
        professor_fn = _consult
    except Exception:
        pass

    if query_fn is None:
        logger.info("ContextDNA memory not available — using confidence-only scoring")
        return None

    logger.info(
        "ContextDNA memory available — analyzer ready (professor=%s)",
        "yes" if professor_fn else "no",
    )

    def analyzer(task_id: str, verdicts: dict) -> Optional["SynthesisResult"]:
        """Compare verdicts against ContextDNA memory for evidence-backed decisions."""
        from multifleet.synthesis import SynthesisResult

        if not verdicts:
            return None

        # Pull latest fleet evidence from Context DNA (bidirectional sync)
        contextdna_evidence: list[dict] = []
        try:
            import signal

            def _timeout_handler(signum, frame):
                raise TimeoutError("product_bridge.memory_to_fleet timed out")

            from multifleet.product_bridge import get_bridge
            task_summary = next(
                (v.get("summary", "") for v in verdicts.values() if v.get("summary")), ""
            )
            if task_summary:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(3)  # 3s timeout for network call
                try:
                    cdna_result = get_bridge().memory_to_fleet(task_summary)
                    contextdna_evidence = cdna_result.get("results", [])
                    if contextdna_evidence:
                        logger.debug("contextdna->analyzer pull: %d entries for task %s",
                                     len(contextdna_evidence), task_id)
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
        except Exception as e:
            logger.debug("contextdna->analyzer pull (non-blocking): %s", e)

        # Query evidence ledger for recent probe findings to inform scoring
        probe_evidence: list[dict] = []
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            recent = ledger.query(event_type="probe_finding", limit=20)
            probe_evidence = recent
            # Merge Context DNA evidence into probe evidence for richer scoring
            if contextdna_evidence:
                probe_evidence.extend(contextdna_evidence)
        except Exception as e:
            logger.debug("Evidence ledger probe query failed: %s", e)

        # Collect adjustments per machine
        adjustments: dict[str, dict] = {}
        # Collect memory reasons explaining WHY memory influenced the decision
        memory_reasons_detail: list[dict] = []

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

            # 1. Query general memory for the summary
            try:
                memory_results = query_fn(query_text, limit=5)
            except Exception as e:
                logger.warning("Memory query failed for %s: %s", mid, e)
                adjustments[mid] = {"delta": NO_MEMORY_MATCH, "reason": f"query error: {e}"}
                continue

            if not memory_results:
                adjustments[mid] = {"delta": NO_MEMORY_MATCH, "reason": "no memory matches"}
                # Still check for file-specific failures and arch decisions
                prior_failures = _query_prior_failures(query_fn, files_touched)
                arch_decisions = _query_arch_decisions(query_fn, summary)
                if prior_failures or arch_decisions:
                    delta = -PRIOR_FAILURE_PENALTY if prior_failures else ARCH_DECISION_BOOST
                    adjustments[mid] = {
                        "delta": delta,
                        "reason": "; ".join(prior_failures or arch_decisions)[:200],
                        "match_count": len(prior_failures) + len(arch_decisions),
                    }
                    memory_reasons_detail.append({
                        "machine": mid,
                        "prior_failures": prior_failures,
                        "arch_decisions": arch_decisions,
                        "source": "file_and_arch_check",
                    })
                continue

            # Score general memory results
            combined_text = " ".join(
                r if isinstance(r, str) else str(r) for r in memory_results
            )
            delta = _score_memory_text(combined_text)

            # 2. Check for prior failures on the specific files touched
            prior_failures = _query_prior_failures(query_fn, files_touched)
            if prior_failures and delta >= 0:
                # File-specific failures override general success signal
                delta = -PRIOR_FAILURE_PENALTY

            # 3. Check for architectural decisions affecting the approach
            arch_decisions = _query_arch_decisions(query_fn, summary)
            if arch_decisions and delta == NO_MEMORY_MATCH:
                # Arch decisions give a small boost (approach aligns with prior decisions)
                delta = ARCH_DECISION_BOOST

            # 4. Cross-reference probe evidence for files this verdict touches
            if probe_evidence and files_touched:
                touched_set = set(files_touched)
                for pe in probe_evidence:
                    payload = pe.get("payload", {})
                    if isinstance(payload, dict):
                        probe_files = set()
                        for ft in payload.get("finding_titles", []):
                            # Finding titles may reference files
                            for f in files_touched:
                                if f in str(ft):
                                    probe_files.add(f)
                        if probe_files:
                            sev = payload.get("severity", "")
                            if sev == "critical":
                                delta -= PRIOR_FAILURE_PENALTY
                                memory_reasons_detail.append({
                                    "machine": mid,
                                    "source": "probe_evidence",
                                    "severity": sev,
                                    "detail": f"Critical probe finding on {probe_files}",
                                })
                            elif sev == "high":
                                delta -= PRIOR_FAILURE_PENALTY * 0.5
                                memory_reasons_detail.append({
                                    "machine": mid,
                                    "source": "probe_evidence",
                                    "severity": sev,
                                    "detail": f"High probe finding on {probe_files}",
                                })

            adjustments[mid] = {
                "delta": delta,
                "reason": combined_text[:200],
                "match_count": len(memory_results),
            }

            # Build detailed reason entry
            reason_entry = {
                "machine": mid,
                "memory_matches": len(memory_results),
                "delta": delta,
                "source": "general_query",
            }
            if prior_failures:
                reason_entry["prior_failures"] = prior_failures
                reason_entry["source"] = "general_query+file_failures"
            if arch_decisions:
                reason_entry["arch_decisions"] = arch_decisions
                reason_entry["source"] = reason_entry.get("source", "") + "+arch"
            memory_reasons_detail.append(reason_entry)

        # 4. Consult professor for overall task wisdom (once, using first non-empty summary)
        professor_wisdom = None
        if professor_fn:
            for v in verdicts.values():
                s = v.get("summary", "")
                if s:
                    professor_wisdom = _query_professor(professor_fn, s)
                    if professor_wisdom:
                        memory_reasons_detail.append({
                            "source": "professor",
                            "wisdom": professor_wisdom[:300],
                        })
                    break

        # Query strategic planner for blind spots (high-value, zero-caller module)
        try:
            from memory.strategic_planner import StrategicPlanner
            planner = StrategicPlanner()
            task_context = next((v.get("summary", "") for v in verdicts.values() if v.get("summary")), "")
            if task_context:
                reminder = planner.get_big_picture_reminder(current_context=task_context)
                if reminder:
                    memory_reasons_detail.append({
                        "source": "strategic_analyst",
                        "findings": [reminder[:300]],
                    })
        except Exception:
            pass

        # If no adjustments have non-zero delta, let the default analyzer handle it
        if all(a["delta"] == NO_MEMORY_MATCH for a in adjustments.values()):
            # Even if all deltas are zero, if we have professor wisdom, still return
            if not professor_wisdom:
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
        if professor_wisdom:
            reasoning += f" Professor: {professor_wisdom[:150]}."

        # Write analysis back to Context DNA (close the learning loop)
        try:
            from multifleet.product_bridge import get_bridge
            get_bridge().fleet_to_memory({
                "event_type": "contextdna_analysis",
                "node_id": "analyzer",
                "evidence": {
                    "task_id": task_id,
                    "decision": decision,
                    "memory_matches": sum(a.get("match_count", 0) for a in adjustments.values()),
                    "professor_consulted": professor_wisdom is not None,
                    "confidence": winner_score,
                },
                "tags": ["contextdna", "analysis", "learning_loop"],
            })
        except Exception:
            pass

        return SynthesisResult(
            task_id=task_id,
            decision=decision,
            winner_branch=winner_verdict.get("branch", winner_id),
            confidence=winner_score,
            reasoning=reasoning,
            cherry_pick_from=cherry_picks,
            followups=winner_verdict.get("followups", []),
            dissent_preserved=all_dissent,
            metadata={"memory_reasons": memory_reasons_detail},
        )

    return analyzer
