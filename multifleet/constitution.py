"""Constitutional Physics — machine-enforceable principles for Context DNA.

Seven principles derived from CLAUDE.md, expressed as executable checks.
Hard-gate principles block actions. Advisory principles log warnings.

Usage:
    from multifleet.constitution import validate_before_action, get_constitution

    result = validate_before_action("merge", {"has_checkpoint": True})
    if not result.passed:
        raise RuntimeError(result.summary)
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet-nerve.constitution")


# ---------------------------------------------------------------------------
# Principle dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principle:
    """A single constitutional principle."""
    id: str              # e.g. "CP-1"
    name: str            # e.g. "Preserve Determinism"
    description: str     # One-line description
    check: Callable      # (context: dict) -> CheckResult
    severity: str        # "hard-gate" or "advisory"


@dataclass
class CheckResult:
    """Outcome of evaluating one principle against a context."""
    principle_id: str
    passed: bool
    message: str
    severity: str  # inherited from Principle

    def to_dict(self) -> dict:
        return {
            "principle_id": self.principle_id,
            "passed": self.passed,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class GateResult:
    """Aggregate outcome of constitutional validation."""
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def hard_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == "hard-gate"]

    @property
    def advisories(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == "advisory"]

    @property
    def summary(self) -> str:
        total = len(self.checks)
        passed_count = sum(1 for c in self.checks if c.passed)
        if self.passed:
            advs = self.advisories
            if advs:
                names = ", ".join(a.principle_id for a in advs)
                return (f"CONSTITUTION PASSED ({passed_count}/{total}). "
                        f"Advisories: {names}")
            return f"CONSTITUTION PASSED ({passed_count}/{total})"
        names = ", ".join(f.principle_id for f in self.hard_failures)
        return f"CONSTITUTION BLOCKED: {names}"

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "duration_ms": round(self.duration_ms, 1),
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_determinism(context: dict) -> CheckResult:
    """CP-1: Same snapshot + inputs must yield same payload hash.

    Context keys:
        inputs: any hashable or serializable input
        output1: first output (or its hash)
        output2: second output (or its hash)

    If output1 and output2 are both provided, they must match.
    If only one output is provided, passes (nothing to compare).
    """
    pid = "CP-1"
    o1 = context.get("output1")
    o2 = context.get("output2")

    if o1 is None or o2 is None:
        return CheckResult(pid, True, "Single output — determinism trivially satisfied", "hard-gate")

    def _hash(obj: Any) -> str:
        if isinstance(obj, bytes):
            return hashlib.sha256(obj).hexdigest()
        return hashlib.sha256(str(obj).encode()).hexdigest()

    h1, h2 = _hash(o1), _hash(o2)
    if h1 == h2:
        return CheckResult(pid, True, f"Outputs match (sha256={h1[:12]}...)", "hard-gate")
    return CheckResult(pid, False,
                       f"Determinism violation: hash1={h1[:12]}... != hash2={h2[:12]}...",
                       "hard-gate")


def check_injection_purity(context: dict) -> CheckResult:
    """CP-2: Injection = retrieval + assembly only. No learning/promotion.

    Context keys:
        operation_log: list[str] — operations performed during injection.
            Forbidden ops: "learn", "promote", "train", "discover", "write_memory"
    """
    pid = "CP-2"
    ops = context.get("operation_log")
    if not ops:
        return CheckResult(pid, True, "No operations logged — injection purity trivially satisfied", "hard-gate")

    forbidden = {"learn", "promote", "train", "discover", "write_memory"}
    violations = [op for op in ops if any(f in op.lower() for f in forbidden)]

    if not violations:
        return CheckResult(pid, True, f"Injection pure ({len(ops)} ops, 0 forbidden)", "hard-gate")
    return CheckResult(pid, False,
                       f"Injection impurity: {', '.join(violations[:5])}",
                       "hard-gate")


def check_sop_integrity(context: dict) -> CheckResult:
    """CP-3: Don't improve SOPs without outcome-backed evidence.

    Context keys:
        sop_changed: bool — whether an SOP is being modified
        evidence_count: int — number of outcome-backed evidence items
    """
    pid = "CP-3"
    if not context.get("sop_changed", False):
        return CheckResult(pid, True, "No SOP changes", "advisory")

    evidence = context.get("evidence_count", 0)
    if evidence > 0:
        return CheckResult(pid, True, f"SOP change backed by {evidence} evidence items", "advisory")
    return CheckResult(pid, False,
                       "SOP modification without outcome-backed evidence",
                       "advisory")


def check_acontext_logic(context: dict) -> CheckResult:
    """CP-4: Always reference acontext. Full expansion only after repeated failure.

    Context keys:
        acontext_referenced: bool — whether acontext was consulted
        failure_count: int — how many prior failures on this path
        full_expansion: bool — whether full expansion is being used
    """
    pid = "CP-4"
    referenced = context.get("acontext_referenced", True)
    if not referenced:
        return CheckResult(pid, False, "acontext not referenced before action", "advisory")

    full_exp = context.get("full_expansion", False)
    failures = context.get("failure_count", 0)
    if full_exp and failures < 2:
        return CheckResult(pid, False,
                           f"Full expansion used after only {failures} failures (need >=2)",
                           "advisory")
    return CheckResult(pid, True, "acontext logic respected", "advisory")


def check_evidence_backing(context: dict) -> CheckResult:
    """CP-5: Outcomes determine truth, confidence tracks empirical rates.

    Context keys:
        claim: str — the claim being made
        evidence_count: int — number of supporting evidence items
        confidence: float — stated confidence (0.0-1.0)
    """
    pid = "CP-5"
    evidence = context.get("evidence_count", 0)
    confidence = context.get("confidence", 0.5)

    if evidence == 0 and confidence > 0.7:
        return CheckResult(pid, False,
                           f"High confidence ({confidence:.0%}) with zero evidence",
                           "hard-gate")
    if evidence == 0:
        return CheckResult(pid, True,
                           f"Low confidence ({confidence:.0%}) appropriate for zero evidence",
                           "hard-gate")
    return CheckResult(pid, True,
                       f"Claim backed by {evidence} evidence items (confidence={confidence:.0%})",
                       "hard-gate")


def check_reversibility(context: dict) -> CheckResult:
    """CP-6: Checkpoints before risk. Prefer reversible actions.

    Context keys:
        action: str — description of the action
        has_checkpoint: bool — whether a checkpoint/backup exists
        destructive: bool — whether the action is destructive/irreversible
    """
    pid = "CP-6"
    destructive = context.get("destructive", False)
    has_checkpoint = context.get("has_checkpoint", False)

    if not destructive:
        return CheckResult(pid, True, "Action is non-destructive", "hard-gate")
    if has_checkpoint:
        return CheckResult(pid, True, "Destructive action has checkpoint", "hard-gate")
    return CheckResult(pid, False,
                       "Destructive action without checkpoint — create backup first",
                       "hard-gate")


def check_minimalism(context: dict) -> CheckResult:
    """CP-7: Maximum value density. Scalpel not axe.

    Context keys:
        files_changed: int — number of files being modified
        lines_changed: int — total lines changed
        scope: str — "minimal", "moderate", "broad"
    """
    pid = "CP-7"
    scope = context.get("scope", "minimal")
    files = context.get("files_changed", 0)
    lines = context.get("lines_changed", 0)

    if scope == "broad" and files > 20:
        return CheckResult(pid, False,
                           f"Broad scope across {files} files — consider narrower approach",
                           "advisory")
    if lines > 1000 and scope != "minimal":
        return CheckResult(pid, False,
                           f"{lines} lines changed — ensure each earns its place",
                           "advisory")
    return CheckResult(pid, True, f"Scope={scope}, {files} files, {lines} lines", "advisory")


# ---------------------------------------------------------------------------
# Constitution registry
# ---------------------------------------------------------------------------

_PRINCIPLES: list[Principle] = [
    Principle(
        id="CP-1",
        name="Preserve Determinism",
        description="Same snapshot + inputs must produce same payload hash. Violation triggers SAFE MODE.",
        check=check_determinism,
        severity="hard-gate",
    ),
    Principle(
        id="CP-2",
        name="No Discovery at Injection",
        description="Injection = retrieval + assembly only. No learning or promotion during injection.",
        check=check_injection_purity,
        severity="hard-gate",
    ),
    Principle(
        id="CP-3",
        name="Respect SOP Integrity",
        description="Never improve SOPs without outcome-backed evidence.",
        check=check_sop_integrity,
        severity="advisory",
    ),
    Principle(
        id="CP-4",
        name="Respect acontext Logic",
        description="Always reference acontext. Full expansion only after repeated failure.",
        check=check_acontext_logic,
        severity="advisory",
    ),
    Principle(
        id="CP-5",
        name="Evidence Over Confidence",
        description="Outcomes determine truth. Confidence must track empirical rates.",
        check=check_evidence_backing,
        severity="hard-gate",
    ),
    Principle(
        id="CP-6",
        name="Prefer Reversible Actions",
        description="Checkpoints before risk. Canary before rollout.",
        check=check_reversibility,
        severity="hard-gate",
    ),
    Principle(
        id="CP-7",
        name="Minimalism",
        description="Maximum value density. Scalpel not axe. Every token earns its place.",
        check=check_minimalism,
        severity="advisory",
    ),
]

# Index by ID for fast lookup
_PRINCIPLES_BY_ID: dict[str, Principle] = {p.id: p for p in _PRINCIPLES}


def get_constitution() -> list[Principle]:
    """Return all 7 constitutional principles."""
    return list(_PRINCIPLES)


def get_principle(principle_id: str) -> Optional[Principle]:
    """Look up a single principle by ID (e.g. 'CP-1')."""
    return _PRINCIPLES_BY_ID.get(principle_id)


# ---------------------------------------------------------------------------
# Explanations (human-readable rationale)
# ---------------------------------------------------------------------------

_EXPLANATIONS: dict[str, str] = {
    "CP-1": (
        "Determinism is the foundation of trust. If the same inputs can produce "
        "different outputs, every downstream consumer — fleet nodes, webhooks, "
        "dashboards — loses the ability to verify correctness. When violated, "
        "the system enters SAFE MODE because non-determinism means something "
        "is injecting uncontrolled state."
    ),
    "CP-2": (
        "Injection is the hot path — it runs on every prompt. If injection "
        "performs learning or promotion, it creates a feedback loop: each prompt "
        "mutates the system, changing future injections unpredictably. Retrieval "
        "and assembly are pure operations; learning belongs in background passes."
    ),
    "CP-3": (
        "SOPs encode hard-won operational knowledge. Changing them without "
        "evidence-backed outcomes risks replacing proven procedures with "
        "plausible-sounding but untested alternatives. Evidence requirement "
        "ensures improvements are real, not just different."
    ),
    "CP-4": (
        "acontext provides compressed, relevant context efficiently. It should "
        "always be the first source consulted. Full expansion (loading entire "
        "files, running expensive queries) is justified only after the compressed "
        "form has proven insufficient through repeated failures."
    ),
    "CP-5": (
        "LLMs can state claims with high confidence that have zero empirical "
        "backing. This principle forces confidence to track actual outcomes — "
        "a claim with no evidence must carry low confidence. This prevents "
        "confident confabulation from driving decisions."
    ),
    "CP-6": (
        "Irreversible actions compound errors. A checkpoint before a destructive "
        "operation costs seconds; recovering from an uncheckpointed mistake costs "
        "hours. Canary deployments, backups, and dry-runs are the safety net "
        "that makes velocity sustainable."
    ),
    "CP-7": (
        "Context windows are finite. Every token, file, and abstraction must "
        "earn its place. Broad changes across many files increase blast radius "
        "and review burden. The goal is maximum impact with minimum surface area — "
        "Bruce Lee's two-inch punch, not a haymaker."
    ),
}


def explain_principle(principle_id: str) -> str:
    """Human-readable explanation of why a principle exists."""
    explanation = _EXPLANATIONS.get(principle_id)
    if not explanation:
        p = get_principle(principle_id)
        if p:
            return f"{p.name}: {p.description}"
        return f"Unknown principle: {principle_id}"
    p = get_principle(principle_id)
    return f"{p.name} ({p.severity})\n\n{explanation}"


# ---------------------------------------------------------------------------
# Constitutional Gate
# ---------------------------------------------------------------------------

class ConstitutionalGate:
    """Validates actions against all constitutional principles.

    Hard-gate failures block. Advisory failures log warnings.
    """

    def __init__(self, principles: Optional[list[Principle]] = None):
        self.principles = principles or list(_PRINCIPLES)

    def validate(self, action: str, context: dict) -> GateResult:
        """Run all principle checks against the given context.

        Args:
            action: Description of the action being validated.
            context: Dict of evidence for check functions.

        Returns:
            GateResult with pass/fail and per-principle details.
        """
        t0 = time.monotonic()
        checks: list[CheckResult] = []

        for principle in self.principles:
            try:
                result = principle.check(context)
                checks.append(result)
                if not result.passed:
                    level = "ERROR" if result.severity == "hard-gate" else "WARNING"
                    logger.log(
                        logging.ERROR if result.severity == "hard-gate" else logging.WARNING,
                        "Constitution %s [%s] %s: %s",
                        principle.id, result.severity, action, result.message,
                    )
            except Exception as e:
                # Zero silent failures — check errors are hard failures
                checks.append(CheckResult(
                    principle_id=principle.id,
                    passed=False,
                    message=f"Check raised exception: {e}",
                    severity=principle.severity,
                ))
                logger.error("Constitution %s check crashed: %s", principle.id, e)

        hard_failures = [c for c in checks if not c.passed and c.severity == "hard-gate"]
        passed = len(hard_failures) == 0
        duration_ms = (time.monotonic() - t0) * 1000

        result = GateResult(passed=passed, checks=checks, duration_ms=duration_ms)

        # Emit gate event (best-effort)
        try:
            from .event_bus import IDEEventBus
            bus = IDEEventBus.get()
            bus.emit("gate.constitutional.checked", {
                "rule": "ConstitutionalGate",
                "gate": "ConstitutionalGate",
                "action": action,
                "passed": passed,
                "checks_run": len(checks),
                "failures": [c.principle_id for c in hard_failures],
                "advisories": [c.principle_id for c in self._advisories(checks)],
                "timestamp": time.time(),
            })
        except Exception:
            pass
        try:
            from .evidence_ledger import EvidenceLedger
            import os
            ledger = EvidenceLedger()
            ledger.record(
                "gate_activation",
                os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                "ConstitutionalGate",
                {"passed": passed, "action": action,
                 "checks_run": len(checks),
                 "failures": [c.principle_id for c in hard_failures]},
            )
        except Exception:
            pass

        return result

    @staticmethod
    def _advisories(checks: list) -> list:
        return [c for c in checks if not c.passed and c.severity == "advisory"]


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------

_DEFAULT_GATE = ConstitutionalGate()


def validate_before_action(action_type: str, context: dict) -> GateResult:
    """Quick check against all constitutional principles.

    Used by invariance gates, fleet operations, merge decisions.

    Args:
        action_type: E.g. "merge", "repair", "inject", "deploy"
        context: Evidence dict consumed by principle check functions.

    Returns:
        GateResult — caller should block on hard-gate failures.
    """
    return _DEFAULT_GATE.validate(action_type, context)
