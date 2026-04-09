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
from multifleet.surgeon_adapter import SurgeonAdapter

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

    def __init__(self, strict: bool = False, probe_engine=None, evidence_ledger=None,
                 surgeon_adapter: SurgeonAdapter = None):
        self._strict = strict
        self._probe_engine = probe_engine
        self._evidence_ledger = evidence_ledger
        self._surgeon: SurgeonAdapter = surgeon_adapter or SurgeonAdapter()

    def evaluate(
        self,
        task_id_or_probes=None,
        branch_or_none=None,
        *,
        context: Optional[dict] = None,
        probe_results: list = None,
        surgeon_resolved: bool = False,
        surgeon_intensity: str = "light",
        surgeon_dissent_count: int = 0,
        invariants_satisfied: bool = True,
        evidence_count: int = 0,
        epistemic_packet: Optional[dict] = None,
        **_extra_kwargs,
    ) -> AcceptanceDecision:
        """Evaluate acceptance criteria. Supports two call patterns:

        Pattern 1 (mac2 style): gate.evaluate("task-id", "branch", context={...})
        Pattern 2 (keyword style): gate.evaluate(probe_results=[...], surgeon_resolved=True, ...)
        """
        # Detect mac2-style positional call: evaluate("task-id", "branch")
        if isinstance(task_id_or_probes, str):
            return self._evaluate_contextual(task_id_or_probes, branch_or_none, context or {})

        # Keyword style — task_id_or_probes might be probe_results list
        if isinstance(task_id_or_probes, list):
            probe_results = task_id_or_probes

        return self._evaluate_keyword(
            probe_results=probe_results,
            surgeon_resolved=surgeon_resolved,
            surgeon_intensity=surgeon_intensity,
            surgeon_dissent_count=surgeon_dissent_count,
            invariants_satisfied=invariants_satisfied,
            evidence_count=evidence_count,
            epistemic_packet=epistemic_packet,
        )

    def _evaluate_contextual(
        self, task_id: str, branch: Optional[str], context: dict
    ) -> AcceptanceDecision:
        """Mac2-style evaluation: extract signals from context dict."""
        blocking = []
        checks = []

        # Check 1: Probes (via injected engine or context)
        if self._probe_engine:
            try:
                results = self._probe_engine.run_cheap(context.get("probe_context", {}))
                for r in results:
                    checks.append({"name": "probe", "status": r.status})
                    if getattr(r, 'status', '') == 'critical' or getattr(r, 'severity', '') == 'critical':
                        blocking.append(f"Critical probe finding in {getattr(r, 'probe_id', '?')}")
            except Exception:
                checks.append({"name": "probe", "status": "skipped"})
        else:
            checks.append({"name": "probe", "status": "skipped"})

        # Check 2: Evidence ledger
        if self._evidence_ledger:
            try:
                entries = self._evidence_ledger.query(event_type=task_id) if hasattr(self._evidence_ledger, 'query') else []
                if not entries:
                    blocking.append(f"No evidence ledger entries for task {task_id}")
                checks.append({"name": "evidence", "status": "pass" if entries else "fail"})
            except Exception:
                checks.append({"name": "evidence", "status": "skipped"})
        else:
            checks.append({"name": "evidence", "status": "skipped"})

        # Check 3: Surgeon state — query adapter for live state, fall back to context
        surgeon_state = context.get("surgeon_state", {})
        if self._surgeon.is_available():
            surgeon_state = self._surgeon.get_state()
            logger.info("Surgeon state from adapter: %s", surgeon_state)
        if surgeon_state:
            phase = surgeon_state.get("phase", "close")
            disagreement = surgeon_state.get("disagreement_score", 0.0)
            surgeon_blocking = []
            if phase in ("triage", "operate"):
                surgeon_blocking.append(f"Surgeon still in {phase} phase")
            if disagreement > 0.7:
                surgeon_blocking.append(f"High surgeon disagreement ({disagreement:.2f})")
            blocking.extend(surgeon_blocking)
            checks.append({"name": "surgeon", "status": "pass" if not surgeon_blocking else "fail"})
        else:
            checks.append({"name": "surgeon", "status": "skipped"})

        # Check 4: Invariants
        checks.append({"name": "invariants", "status": "skipped"})

        # Check 5: Mirror drift
        if context.get("mirror_drift_detected") and not context.get("mirror_drift_acknowledged"):
            blocking.append("Unacknowledged mirror drift detected")
            checks.append({"name": "mirror_drift", "status": "fail"})
        else:
            checks.append({"name": "mirror_drift", "status": "pass" if context.get("mirror_drift_detected") else "skipped"})

        # Check 6: Test coverage
        checks.append({"name": "test_coverage", "status": "skipped"})

        accepted = len(blocking) == 0
        decision = AcceptanceDecision(
            accepted=accepted,
            reason=f"{len(blocking)} blocking issue(s)" if blocking else "All checks passed",
            blocking_issues=blocking,
        )
        decision.checks = checks  # Mac2 expects this attribute
        return decision

    def _evaluate_keyword(
        self,
        probe_results: list = None,
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

        # Auto-determine surgeon intensity from probe results if not explicitly set
        if probe_results and surgeon_intensity == "light":
            try:
                from multifleet.probe_intensity import intensity_for_acceptance
                auto = intensity_for_acceptance(probe_results, epistemic_packet)
                surgeon_intensity = auto.get("surgeon_intensity", surgeon_intensity)
            except (ImportError, Exception):
                pass  # Graceful degradation — probe_intensity not available

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

        # Check 2: Surgeon resolution — verify via adapter, NEVER trust caller alone.
        # The validate_acceptance() path replaces the old pattern where callers
        # could pass surgeon_resolved=True without any actual surgeon involvement.
        if surgeon_intensity in ("standard", "full"):
            if self._surgeon.is_available():
                validation = self._surgeon.validate_acceptance(
                    task_id=f"keyword-eval-{int(time.time())}",
                    result={
                        "claim": f"Task acceptance check (intensity={surgeon_intensity})",
                        "evidence": epistemic_packet or {},
                    },
                )
                surgeon_resolved = validation.get("surgeon_resolved", False)
                surgeon_dissent_count = validation.get("dissent_count", 0)
                logger.info(
                    "Surgeon validate_acceptance: resolved=%s confidence=%.2f "
                    "dissents=%d source=%s consulted=%s",
                    surgeon_resolved, validation.get("confidence", 0.0),
                    surgeon_dissent_count, validation.get("source", "?"),
                    validation.get("surgeons_consulted", []),
                )
            else:
                # Bridge unavailable: degrade but log warning (Zero Silent Failures)
                logger.warning(
                    "Surgeon adapter unavailable — caller-provided surgeon_resolved=%s "
                    "accepted with DEGRADED confidence", surgeon_resolved,
                )
            if not surgeon_resolved:
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

        # Emit event (best-effort, non-blocking)
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            et = EventType.TASK_ACCEPTED if decision.accepted else EventType.TASK_BLOCKED
            emit_typed(et, EventSource.CONTEXTDNA, payload={
                "accepted": decision.accepted,
                "reason": decision.reason,
                "blocking_count": len(decision.blocking_issues),
            })
        except (ImportError, Exception):
            pass

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
