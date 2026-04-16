"""Memory Influence Resolver — the "holy shit" demo moment.

During synthesis, queries past decisions from the evidence ledger to show
WHICH memories influenced the current verdict. Two tiers:

Tier 1: Local evidence (fast, always available)
Tier 2: Chief node escalation (when confidence < threshold or evidence sparse)

When confidence is low, evidence becomes the PRIMARY decision driver.
This is corrigibility-as-code: the system trusts its memory over its
current confidence.

"The real moat is not transport — it is the intelligence that accumulates."
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.memory-influence")

# Confidence threshold below which we escalate to chief
CONFIDENCE_THRESHOLD = 0.7
# Maximum past decisions to query
MAX_QUERY = 10


@dataclass
class MemoryInfluence:
    """Evidence-backed memory influence on a synthesis decision."""

    evidence_refs: list[dict] = field(default_factory=list)
    pattern_summary: str = ""
    decisions_queried: int = 0
    decisions_relevant: int = 0
    confidence_adjustment: float = 0.0
    authority: str = "local"  # "local" or "chief"
    escalated_to_chief: bool = False
    escalation_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "evidence_refs": self.evidence_refs,
            "pattern_summary": self.pattern_summary,
            "decisions_queried": self.decisions_queried,
            "decisions_relevant": self.decisions_relevant,
            "confidence_adjustment": round(self.confidence_adjustment, 3),
            "authority": self.authority,
            "escalated_to_chief": self.escalated_to_chief,
            "escalation_reason": self.escalation_reason,
        }

    def summary_line(self) -> str:
        """One-line summary for dashboard display."""
        if not self.evidence_refs:
            return "No past evidence found for this decision."
        src = "chief" if self.authority == "chief" else "local"
        return (
            f"{self.decisions_relevant} past decision(s) influenced this verdict ({src}). "
            f"{self.pattern_summary}"
        )


class MemoryInfluenceResolver:
    """Resolves memory influence for synthesis decisions.

    Queries evidence ledger + decisions JSONL for past decisions related
    to the current task. Also consults ContextDNA memory (professor wisdom
    and learnings) for richer evidence. When confidence is low, escalates
    to chief node for authoritative evidence.
    """

    def __init__(
        self,
        evidence_ledger=None,
        decisions_path: Optional[str] = None,
        node_id: str = "",
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ):
        self._ledger = evidence_ledger
        self._decisions_path = Path(
            decisions_path
            or str(Path.home() / ".fleet-nerve" / "decisions.jsonl")
        )
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._threshold = confidence_threshold

        # Wire ContextDNA memory for compounding intelligence
        self._query_fn = None
        self._professor_fn = None
        try:
            from memory.query import query_learnings
            self._query_fn = query_learnings
        except Exception:
            pass
        try:
            from memory.professor import consult as _consult
            self._professor_fn = _consult
        except Exception:
            pass

    def resolve(
        self,
        task_id: str,
        verdicts: dict[str, dict],
        current_confidence: float = 1.0,
    ) -> MemoryInfluence:
        """Resolve memory influence for a synthesis decision.

        Args:
            task_id: The task being synthesized.
            verdicts: Machine verdicts being evaluated.
            current_confidence: Current confidence score from scoring.

        Returns:
            MemoryInfluence with evidence refs, pattern summary, and adjustment.
        """
        t0 = time.monotonic()

        # Extract keywords from task context for matching
        keywords = self._extract_keywords(task_id, verdicts)

        # Tier 1: Local evidence (ledger + decisions JSONL)
        influence = self._query_local(keywords)

        # Tier 1.5: ContextDNA memory (learnings + professor wisdom)
        self._enrich_from_contextdna(influence, task_id, verdicts, keywords)

        # Tier 2: Chief escalation if needed
        if current_confidence < self._threshold or influence.decisions_relevant < 2:
            chief_influence = self._escalate_to_chief(task_id, keywords)
            if chief_influence and chief_influence.decisions_relevant > influence.decisions_relevant:
                influence = chief_influence
                influence.authority = "chief"
                influence.escalated_to_chief = True
                influence.escalation_reason = (
                    f"Low confidence ({current_confidence:.2f})"
                    if current_confidence < self._threshold
                    else "Sparse local evidence"
                )

        # Calculate confidence adjustment based on evidence (additive with ContextDNA)
        cdna_adj = influence.confidence_adjustment  # preserve ContextDNA adjustments
        influence.confidence_adjustment = cdna_adj + self._calculate_adjustment(
            influence, current_confidence
        )

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(
            "memory_influence resolved: %d refs, %d relevant, adj=%.3f, authority=%s (%.0fms)",
            len(influence.evidence_refs),
            influence.decisions_relevant,
            influence.confidence_adjustment,
            influence.authority,
            elapsed,
        )
        return influence

    def _extract_keywords(self, task_id: str, verdicts: dict) -> list[str]:
        """Extract searchable keywords from task context."""
        words = set()
        # From task_id
        words.update(re.findall(r"[a-z]{3,}", task_id.lower()))
        # From verdict summaries and branches
        for machine, verdict in verdicts.items():
            summary = verdict.get("summary", "")
            branch = verdict.get("branch", "")
            words.update(re.findall(r"[a-z]{3,}", summary.lower()))
            words.update(re.findall(r"[a-z]{3,}", branch.lower()))
        # Filter noise
        noise = {"the", "and", "for", "this", "that", "with", "from", "into", "has", "was", "are"}
        return [w for w in words if w not in noise][:20]

    def _query_local(self, keywords: list[str]) -> MemoryInfluence:
        """Tier 1: Query local evidence ledger + decisions JSONL."""
        evidence_refs = []
        all_decisions = []

        # Query evidence ledger (SQLite)
        if self._ledger:
            try:
                recent = self._ledger.query(
                    event_type="merge_decision", limit=MAX_QUERY
                )
                for entry in recent:
                    payload = entry.get("payload", "{}")
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {}
                    reasoning = payload.get("reasoning", entry.get("subject", ""))
                    if self._matches_keywords(reasoning, keywords):
                        evidence_refs.append({
                            "entry_id": entry.get("entry_id", ""),
                            "event_type": entry.get("event_type", ""),
                            "node_id": entry.get("node_id", ""),
                            "timestamp": entry.get("timestamp", 0),
                            "entry_hash": entry.get("entry_hash", ""),
                            "reasoning_snippet": reasoning[:200],
                        })
            except Exception as e:
                logger.debug("Evidence ledger query failed: %s", e)

        # Query decisions JSONL (file-based)
        if self._decisions_path.exists():
            try:
                lines = self._decisions_path.read_text().strip().splitlines()[-50:]
                for line in lines:
                    try:
                        decision = json.loads(line)
                        all_decisions.append(decision)
                        reasoning = decision.get("reasoning", "")
                        if self._matches_keywords(reasoning, keywords):
                            # Don't duplicate entries already from ledger
                            task = decision.get("task_id", "")
                            if not any(r.get("entry_id", "").startswith(task[:8]) for r in evidence_refs):
                                evidence_refs.append({
                                    "entry_id": task,
                                    "event_type": "decision_log",
                                    "node_id": self._node_id,
                                    "timestamp": decision.get("timestamp", 0),
                                    "entry_hash": "",  # JSONL doesn't have hash chain
                                    "reasoning_snippet": reasoning[:200],
                                })
                    except json.JSONDecodeError:
                        continue
            except Exception as e:
                logger.debug("Decisions JSONL query failed: %s", e)

        # Build pattern summary from all decisions
        pattern = self._build_pattern_summary(all_decisions, keywords)

        return MemoryInfluence(
            evidence_refs=evidence_refs[:10],
            pattern_summary=pattern,
            decisions_queried=len(all_decisions),
            decisions_relevant=len(evidence_refs),
        )

    def _enrich_from_contextdna(
        self,
        influence: MemoryInfluence,
        task_id: str,
        verdicts: dict[str, dict],
        keywords: list[str],
    ):
        """Enrich influence with ContextDNA learnings and professor wisdom.

        Queries the memory system for past learnings related to this task,
        and consults the professor for distilled wisdom. This compounds
        intelligence from the full memory system into synthesis decisions.
        """
        # Query ContextDNA learnings for relevant prior knowledge
        if self._query_fn:
            try:
                # Build a meaningful query from task context
                task_summary = next(
                    (v.get("summary", "") for v in verdicts.values() if v.get("summary")),
                    task_id,
                )
                if task_summary:
                    learnings = self._query_fn(task_summary, limit=5)
                    for learning_text in learnings:
                        text = learning_text if isinstance(learning_text, str) else str(learning_text)
                        text_lower = text.lower()
                        # Check if learning signals caution or support
                        has_failure_signal = any(
                            kw in text_lower for kw in (
                                "fail", "broke", "regression", "revert", "bug",
                                "crash", "error", "never", "avoid",
                            )
                        )
                        has_success_signal = any(
                            kw in text_lower for kw in (
                                "success", "works", "fixed", "solved", "improved",
                                "verified", "stable",
                            )
                        )
                        influence.evidence_refs.append({
                            "entry_id": f"cdna:{hash(text) & 0xFFFF:04x}",
                            "event_type": "contextdna_learning",
                            "node_id": self._node_id,
                            "timestamp": time.time(),
                            "entry_hash": "",
                            "reasoning_snippet": text[:200],
                        })
                        influence.decisions_relevant += 1
                        # Adjust confidence based on learning sentiment
                        if has_failure_signal:
                            influence.confidence_adjustment -= 0.03
                        elif has_success_signal:
                            influence.confidence_adjustment += 0.02
            except Exception as e:
                logger.debug("ContextDNA learnings query failed (non-blocking): %s", e)

        # Consult professor for distilled wisdom about this task
        if self._professor_fn:
            try:
                task_summary = next(
                    (v.get("summary", "") for v in verdicts.values() if v.get("summary")),
                    task_id,
                )
                if task_summary and len(task_summary) > 5:
                    guidance = self._professor_fn(task=task_summary)
                    if guidance and isinstance(guidance, str) and len(guidance.strip()) > 10:
                        # Professor wisdom enriches the pattern summary
                        # Extract the most critical insight (first 300 chars)
                        wisdom_snippet = guidance.strip()[:300]
                        if influence.pattern_summary:
                            influence.pattern_summary += f" Professor: {wisdom_snippet}"
                        else:
                            influence.pattern_summary = f"Professor: {wisdom_snippet}"
                        logger.debug(
                            "Professor wisdom enriched memory influence for task %s",
                            task_id,
                        )
            except Exception as e:
                logger.debug("Professor consult failed (non-blocking): %s", e)

    def _escalate_to_chief(self, task_id: str, keywords: list[str]) -> Optional[MemoryInfluence]:
        """Tier 2: Escalate to chief node for authoritative evidence."""
        try:
            from multifleet.product_bridge import ProductBridge
            bridge = ProductBridge()
            bridge.discover_products()

            result = bridge.fleet_to_memory({
                "action": "query_evidence",
                "task_id": task_id,
                "keywords": keywords,
                "limit": MAX_QUERY,
                "requesting_node": self._node_id,
            })

            if not result or "error" in result:
                return None

            # Parse chief's response
            refs = result.get("evidence_refs", [])
            return MemoryInfluence(
                evidence_refs=refs[:10],
                pattern_summary=result.get("pattern_summary", ""),
                decisions_queried=result.get("decisions_queried", 0),
                decisions_relevant=len(refs),
                authority="chief",
                escalated_to_chief=True,
            )
        except Exception as e:
            logger.debug("Chief escalation failed (degrading to local): %s", e)
            return None

    def _matches_keywords(self, text: str, keywords: list[str]) -> bool:
        """Check if text matches any of the keywords."""
        if not text or not keywords:
            return False
        text_lower = text.lower()
        matches = sum(1 for kw in keywords if kw in text_lower)
        return matches >= 2  # At least 2 keyword matches

    def _build_pattern_summary(self, decisions: list[dict], keywords: list[str]) -> str:
        """Build a human-readable pattern summary from past decisions."""
        if not decisions:
            return ""

        relevant = [d for d in decisions if self._matches_keywords(d.get("reasoning", ""), keywords)]
        if not relevant:
            return ""

        # Count decision types
        by_decision = {}
        for d in relevant:
            dec = d.get("decision", "unknown")
            by_decision[dec] = by_decision.get(dec, 0) + 1

        # Find common dissent themes
        dissent_themes = []
        for d in relevant:
            if d.get("dissent_count", 0) > 0:
                reasoning = d.get("reasoning", "")
                if reasoning:
                    dissent_themes.append(reasoning[:100])

        # Build summary
        parts = []
        total = len(relevant)
        for dec, count in sorted(by_decision.items(), key=lambda x: -x[1]):
            parts.append(f"{dec}: {count}/{total}")

        summary = f"Past decisions: {', '.join(parts)}."
        if dissent_themes:
            summary += f" Common concern: {dissent_themes[0][:80]}"

        return summary

    def _calculate_adjustment(self, influence: MemoryInfluence, current: float) -> float:
        """Calculate confidence adjustment based on evidence.

        Positive = evidence supports current direction.
        Negative = evidence contradicts (caution).
        """
        if not influence.evidence_refs:
            return 0.0

        # Count how many past decisions were rejections/rebuttals
        rejections = sum(
            1 for ref in influence.evidence_refs
            if any(w in ref.get("reasoning_snippet", "").lower()
                   for w in ("reject", "rebuttal", "needs_rebuttal", "failed", "broke"))
        )
        approvals = len(influence.evidence_refs) - rejections

        if rejections > approvals:
            # Evidence suggests caution
            return -0.1 * (rejections - approvals)
        elif approvals > rejections:
            # Evidence supports
            return 0.05 * (approvals - rejections)
        return 0.0
