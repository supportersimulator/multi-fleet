"""Ghost Digest — ambient session digestion for ContextDNA.

When a node finishes a task, the ghost ingests:
- transcript summary
- diffs
- probe findings
- surgeon disagreements
- accepted/rejected outcome

And turns them into:
- evidence candidates (for promotion pipeline)
- unresolved objections (carried forward)
- future warnings (for next session injection)
- packet hints (for next EpistemicPacket)

"The ghost should not just be background automation. It should be the part
of the organism that refuses to let polished output silently become truth."
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-digest")


@dataclass
class EvidenceCandidate:
    """A potential evidence item extracted from a completed task."""

    candidate_id: str
    source_task_id: str
    source_node: str
    title: str
    category: str  # decision, finding, objection, warning, learning
    content: str
    confidence: float  # 0.0-1.0
    evidence_refs: list[str] = field(default_factory=list)
    requires_corroboration: bool = True
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "source_task_id": self.source_task_id,
            "source_node": self.source_node,
            "title": self.title,
            "category": self.category,
            "content": self.content,
            "confidence": round(self.confidence, 3),
            "evidence_refs": self.evidence_refs,
            "requires_corroboration": self.requires_corroboration,
            "created_at": self.created_at,
        }


@dataclass
class DigestResult:
    """Result of digesting a completed task."""

    task_id: str
    evidence_candidates: list[EvidenceCandidate] = field(default_factory=list)
    unresolved_objections: list[str] = field(default_factory=list)
    future_warnings: list[str] = field(default_factory=list)
    packet_hints: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "evidence_candidates": [c.to_dict() for c in self.evidence_candidates],
            "unresolved_objections": self.unresolved_objections,
            "future_warnings": self.future_warnings,
            "packet_hints": self.packet_hints,
            "duration_ms": round(self.duration_ms, 1),
        }

    def summary(self) -> str:
        return (
            f"Digest {self.task_id[:8]}: "
            f"{len(self.evidence_candidates)} candidates, "
            f"{len(self.unresolved_objections)} objections, "
            f"{len(self.future_warnings)} warnings"
        )


class GhostDigest:
    """Digests completed tasks into evidence candidates for ContextDNA.

    This is the bridge between "work happened" and "the system learned."
    Without digestion, completed tasks are just diffs. With digestion,
    they become evidence that influences future decisions.
    """

    def __init__(self, evidence_dir: Optional[str] = None, node_id: str = "unknown"):
        self._evidence_dir = Path(
            evidence_dir or str(Path.home() / ".fleet-nerve" / "evidence-candidates")
        )
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id

    def digest(
        self,
        task_id: str,
        task_intent: str = "",
        synthesis_result: Optional[dict] = None,
        probe_results: Optional[list[dict]] = None,
        acceptance_decision: Optional[dict] = None,
        diff_summary: str = "",
    ) -> DigestResult:
        """Digest a completed task into evidence candidates.

        Args:
            task_id: The task that completed.
            task_intent: What the task was trying to do.
            synthesis_result: ChiefSynthesis output (decision, winner, dissent).
            probe_results: Probe findings from the task.
            acceptance_decision: AcceptanceGate decision.
            diff_summary: Summary of code changes.
        """
        t0 = time.monotonic()
        candidates = []
        objections = []
        warnings = []
        hints = []

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Extract evidence from synthesis
        if synthesis_result:
            candidates.extend(
                self._extract_from_synthesis(task_id, synthesis_result, ts)
            )
            objections.extend(
                self._extract_objections(synthesis_result)
            )

        # Extract evidence from probes
        if probe_results:
            candidates.extend(
                self._extract_from_probes(task_id, probe_results, ts)
            )

        # Extract warnings from acceptance
        if acceptance_decision:
            warnings.extend(
                self._extract_warnings(acceptance_decision)
            )
            hints.extend(
                self._extract_hints(acceptance_decision)
            )

        # Record task completion as evidence
        candidates.append(EvidenceCandidate(
            candidate_id=str(uuid.uuid4())[:12],
            source_task_id=task_id,
            source_node=self._node_id,
            title=f"Task completed: {task_intent[:80]}",
            category="decision",
            content=diff_summary[:500] if diff_summary else "No diff summary",
            confidence=0.7,
            requires_corroboration=False,
            created_at=ts,
        ))

        result = DigestResult(
            task_id=task_id,
            evidence_candidates=candidates,
            unresolved_objections=objections,
            future_warnings=warnings,
            packet_hints=hints,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

        # Persist candidates to disk
        self._persist(result)

        logger.info("ghost_digest %s", result.summary())
        return result

    def _extract_from_synthesis(
        self, task_id: str, synthesis: dict, ts: str
    ) -> list[EvidenceCandidate]:
        """Extract evidence candidates from ChiefSynthesis result."""
        candidates = []

        decision = synthesis.get("decision", "")
        winner = synthesis.get("winner_branch", synthesis.get("winner", ""))
        reasoning = synthesis.get("reasoning", "")

        if decision and reasoning:
            candidates.append(EvidenceCandidate(
                candidate_id=str(uuid.uuid4())[:12],
                source_task_id=task_id,
                source_node=self._node_id,
                title=f"Synthesis decision: {decision} (winner: {winner})",
                category="decision",
                content=reasoning[:500],
                confidence=synthesis.get("confidence", 0.5),
                evidence_refs=[f"synthesis:{task_id}"],
                requires_corroboration=decision == "needs_rebuttal",
                created_at=ts,
            ))

        # Cherry-picks are learning opportunities
        cherry_picks = synthesis.get("cherry_pick_from", [])
        for cp in cherry_picks[:3]:
            candidates.append(EvidenceCandidate(
                candidate_id=str(uuid.uuid4())[:12],
                source_task_id=task_id,
                source_node=self._node_id,
                title=f"Cherry-pick suggestion from {cp.get('machine', '?')}",
                category="learning",
                content=cp.get("reason", "")[:300],
                confidence=0.6,
                evidence_refs=[f"cherry:{cp.get('commit', '?')}"],
                created_at=ts,
            ))

        return candidates

    def _extract_objections(self, synthesis: dict) -> list[str]:
        """Extract unresolved objections (dissent) that must be carried forward."""
        dissent = synthesis.get("dissent_preserved", [])
        return [
            f"{d.get('machine', '?')}: {d.get('claim', str(d))[:200]}"
            for d in dissent[:5]
        ]

    def _extract_from_probes(
        self, task_id: str, probes: list[dict], ts: str
    ) -> list[EvidenceCandidate]:
        """Extract evidence from probe findings."""
        candidates = []
        for probe in probes:
            findings = probe.get("findings", [])
            for f in findings:
                if not isinstance(f, dict):
                    continue
                severity = probe.get("severity", "info")
                if severity in ("high", "critical"):
                    candidates.append(EvidenceCandidate(
                        candidate_id=str(uuid.uuid4())[:12],
                        source_task_id=task_id,
                        source_node=self._node_id,
                        title=f"Probe finding: {f.get('title', '?')[:100]}",
                        category="finding",
                        content=json.dumps(f.get("evidence", []))[:300],
                        confidence=probe.get("confidence", 0.5),
                        evidence_refs=f.get("evidence_refs", f.get("evidence", [])),
                        created_at=ts,
                    ))
        return candidates[:10]

    def _extract_warnings(self, acceptance: dict) -> list[str]:
        """Extract warnings from acceptance decision for future injection."""
        warnings = []
        if not acceptance.get("accepted", True):
            warnings.append(f"BLOCKED: {acceptance.get('reason', '?')}")
        for issue in acceptance.get("blocking_issues", [])[:3]:
            warnings.append(f"Blocker: {issue}")
        for followup in acceptance.get("required_followups", [])[:3]:
            warnings.append(f"Followup needed: {followup}")
        return warnings

    def _extract_hints(self, acceptance: dict) -> list[str]:
        """Extract packet hints for next EpistemicPacket."""
        hints = []
        if acceptance.get("blocking_issues"):
            hints.append("Include blocking probe context in next packet")
        if acceptance.get("unresolved_objection_ids"):
            hints.append("Carry forward unresolved objections")
        return hints

    def _persist(self, result: DigestResult) -> None:
        """Write digest result to disk for persistence across sessions."""
        try:
            output_path = self._evidence_dir / f"{result.task_id[:12]}.json"
            output_path.write_text(json.dumps(result.to_dict(), indent=2))
        except Exception as e:
            logger.warning("Failed to persist digest: %s", e)
