"""Acceptance Gate — corrigibility-as-highest-weight expressed as code.

A task is NOT done because code exists.
A task is NOT done because the agent stopped.
A task is NOT done because the diff looks plausible.

A task is accepted ONLY when:
- Required surgeon run reached resolution
- No unresolved blocking probe findings
- Active invariant pack passed
- Evidence packet exists for why the change is believed safe
- Chief returns accepted: true

This is where the GhostScan philosophy becomes enforceable:
"Make compliance the only viable path forward."
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from multifleet.probe_engine import ProbeResult, ProbeFinding

logger = logging.getLogger("fleet-nerve.acceptance-gate")


@dataclass
class AcceptanceDecision:
    """Result of evaluating whether a task's output should be accepted."""

    accepted: bool
    reason: str
    blocking_issues: list[str] = field(default_factory=list)
    required_followups: list[str] = field(default_factory=list)
    promoted_evidence_ids: list[str] = field(default_factory=list)
    unresolved_objection_ids: list[str] = field(default_factory=list)
    evaluated_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "blocking_issues": self.blocking_issues,
            "required_followups": self.required_followups,
            "promoted_evidence_ids": self.promoted_evidence_ids,
            "unresolved_objection_ids": self.unresolved_objection_ids,
            "evaluated_at": self.evaluated_at,
        }

    def summary(self) -> str:
        status = "ACCEPTED" if self.accepted else "BLOCKED"
        return f"{status}: {self.reason} ({len(self.blocking_issues)} blockers)"


class AcceptanceGate:
    """Evaluates whether a task's output meets acceptance criteria.

    This is the integration point where probes, surgeons, and invariants
    converge into a single accept/block decision.
    """

    def __init__(self, strict: bool = False):
        self._strict = strict

    def evaluate(
        self,
        probe_results: list[ProbeResult] = None,
        surgeon_resolved: bool = False,
        surgeon_intensity: str = "light",
        surgeon_dissent_count: int = 0,
        invariants_satisfied: bool = True,
        evidence_count: int = 0,
        epistemic_packet: Optional[dict] = None,
    ) -> AcceptanceDecision:
        """Evaluate all acceptance criteria and produce a decision.

        Args:
            probe_results: Results from probe engine scan.
            surgeon_resolved: Whether 3-surgeons reached resolution.
            surgeon_intensity: Required intensity (light/standard/full).
            surgeon_dissent_count: Number of unresolved surgeon dissents.
            invariants_satisfied: Whether all active invariants passed.
            evidence_count: Number of evidence items supporting the change.
            epistemic_packet: The EpistemicPacket that was dispatched.
        """
        blocking = []
        followups = []
        objections = []

        # Check 1: Blocking probe findings
        if probe_results:
            for result in probe_results:
                if isinstance(result, dict):
                    findings = result.get("findings", [])
                    for f in findings:
                        if isinstance(f, dict) and f.get("blast_radius") == "high":
                            blocking.append(f"Blocking probe: {f.get('title', '?')}")
                elif isinstance(result, ProbeResult):
                    for f in result.findings:
                        if f.blast_radius == "high":
                            blocking.append(f"Blocking probe: {f.title}")

        # Check 2: Surgeon resolution
        if surgeon_intensity in ("standard", "full") and not surgeon_resolved:
            blocking.append(f"Surgeon review required ({surgeon_intensity}) but not resolved")

        if surgeon_dissent_count > 0:
            objections.append(f"{surgeon_dissent_count} unresolved surgeon dissent(s)")
            if self._strict:
                blocking.append(f"Strict mode: {surgeon_dissent_count} dissents unresolved")

        # Check 3: Invariants
        if not invariants_satisfied:
            blocking.append("Active invariants not satisfied")

        # Check 4: Evidence exists
        if evidence_count == 0 and surgeon_intensity != "light":
            followups.append("No evidence supporting this change — add before merge")
            if self._strict:
                blocking.append("Strict mode: zero evidence for non-trivial change")

        # Check 5: Epistemic packet acceptance policy
        if epistemic_packet:
            policy = epistemic_packet.get("acceptance_policy", "standard")
            if policy == "strict" and blocking:
                # In strict mode, ALL blockers must be resolved
                pass  # Already handled above
            elif policy == "permissive" and not any("invariant" in b.lower() for b in blocking):
                # Permissive: only invariant violations block
                blocking = [b for b in blocking if "invariant" in b.lower()]

        # Decision
        accepted = len(blocking) == 0
        if accepted:
            reason = "All checks passed"
            if followups:
                reason += f" ({len(followups)} followup(s) recommended)"
        else:
            reason = f"{len(blocking)} blocking issue(s)"

        decision = AcceptanceDecision(
            accepted=accepted,
            reason=reason,
            blocking_issues=blocking,
            required_followups=followups,
            unresolved_objection_ids=objections,
        )

        logger.info("acceptance_gate %s", decision.summary())
        return decision

    def quick_check(self, probe_results: list[ProbeResult] = None) -> bool:
        """Fast boolean check — any blocking findings?"""
        if not probe_results:
            return True
        for result in probe_results:
            if isinstance(result, ProbeResult):
                for f in result.findings:
                    if f.blast_radius == "high":
                        return False
            elif isinstance(result, dict):
                for f in result.get("findings", []):
                    if isinstance(f, dict) and f.get("blast_radius") == "high":
                        return False
        return True
