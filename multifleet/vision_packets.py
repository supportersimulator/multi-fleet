"""Vision Packets — structured artifacts for fleet coordination.

The 5 v1 artifacts from the GhostScan chat that enable structured
brainstorming, surgical adjudication, and chief synthesis.

Packet flow:
  BrainstormPacket → SurgeonDecisionPacket → VisionContract
                                           → ChiefEscalationPacket → ChiefDecisionPacket
  VisionContract → ExecutionLedger (tracks progress)
"""

from dataclasses import dataclass, field, asdict
from typing import Literal, Optional
import time
import uuid


def _short_id() -> str:
    return str(uuid.uuid4())[:8]


# ── BrainstormPacket ──────────────────────────────────────────────────────


@dataclass
class BrainstormOption:
    """A single option within a brainstorm tree."""
    id: str
    title: str
    description: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    variants: list[dict] = field(default_factory=list)  # sub-options
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BrainstormOption":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data["description"],
            pros=data.get("pros", []),
            cons=data.get("cons", []),
            variants=data.get("variants", []),
            confidence=data.get("confidence", 0.5),
        )


@dataclass
class BrainstormPacket:
    """Structured option tree from Superpowers brainstorming phase."""
    id: str = field(default_factory=_short_id)
    task_id: str = ""
    node_id: str = ""
    options: list[BrainstormOption] = field(default_factory=list)
    recommended: str = ""  # option ID
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["options"] = [o.to_dict() for o in self.options]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BrainstormPacket":
        options = [BrainstormOption.from_dict(o) for o in data.get("options", [])]
        return cls(
            id=data.get("id", _short_id()),
            task_id=data.get("task_id", ""),
            node_id=data.get("node_id", ""),
            options=options,
            recommended=data.get("recommended", ""),
            created_at=data.get("created_at", time.time()),
        )

    def add_option(self, title: str, description: str, **kwargs) -> BrainstormOption:
        """Add an option and return it."""
        opt = BrainstormOption(
            id=_short_id(),
            title=title,
            description=description,
            **kwargs,
        )
        self.options.append(opt)
        return opt

    def get_option(self, option_id: str) -> Optional[BrainstormOption]:
        for o in self.options:
            if o.id == option_id:
                return o
        return None


# ── SurgeonDecisionPacket ─────────────────────────────────────────────────


SURGEON_ROLES = ("atlas", "cardiologist", "neurologist")


@dataclass
class SurgeonOpinion:
    """One surgeon's vote on brainstorm options."""
    surgeon: Literal["atlas", "cardiologist", "neurologist"]
    vote: str  # option ID or "abstain"
    confidence: float
    reasoning: str
    objections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SurgeonOpinion":
        return cls(
            surgeon=data["surgeon"],
            vote=data["vote"],
            confidence=data["confidence"],
            reasoning=data["reasoning"],
            objections=data.get("objections", []),
        )


@dataclass
class SurgeonDecisionPacket:
    """3-Surgeon adjudication result on brainstorm options."""
    id: str = field(default_factory=_short_id)
    brainstorm_id: str = ""
    task_id: str = ""
    opinions: list[SurgeonOpinion] = field(default_factory=list)
    consensus: bool = False
    selected_option: str = ""  # option ID
    unresolved_objections: list[str] = field(default_factory=list)
    requires_chief: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["opinions"] = [o.to_dict() for o in self.opinions]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "SurgeonDecisionPacket":
        opinions = [SurgeonOpinion.from_dict(o) for o in data.get("opinions", [])]
        return cls(
            id=data.get("id", _short_id()),
            brainstorm_id=data.get("brainstorm_id", ""),
            task_id=data.get("task_id", ""),
            opinions=opinions,
            consensus=data.get("consensus", False),
            selected_option=data.get("selected_option", ""),
            unresolved_objections=data.get("unresolved_objections", []),
            requires_chief=data.get("requires_chief", False),
            created_at=data.get("created_at", time.time()),
        )

    def add_opinion(self, surgeon: str, vote: str, confidence: float,
                    reasoning: str, objections: list[str] = None) -> SurgeonOpinion:
        """Record a surgeon's opinion."""
        op = SurgeonOpinion(
            surgeon=surgeon,
            vote=vote,
            confidence=confidence,
            reasoning=reasoning,
            objections=objections or [],
        )
        self.opinions.append(op)
        return op

    def check_consensus(self) -> bool:
        """Check if all surgeons agree. Updates consensus and selected_option."""
        if len(self.opinions) < 2:
            self.consensus = False
            return False
        votes = [o.vote for o in self.opinions if o.vote != "abstain"]
        if not votes:
            self.consensus = False
            return False
        self.consensus = len(set(votes)) == 1
        if self.consensus:
            self.selected_option = votes[0]
        return self.consensus


# ── VisionContract ────────────────────────────────────────────────────────


@dataclass
class VisionContract:
    """What must be true for a task to be 'done'. Locked after adjudication."""
    id: str = field(default_factory=_short_id)
    task_id: str = ""
    description: str = ""
    done_criteria: list[str] = field(default_factory=list)
    selected_approach: str = ""  # from brainstorm
    locked: bool = False
    locked_at: float = 0.0
    locked_by: str = ""  # node_id
    surgeon_decision_id: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VisionContract":
        return cls(
            id=data.get("id", _short_id()),
            task_id=data.get("task_id", ""),
            description=data.get("description", ""),
            done_criteria=data.get("done_criteria", []),
            selected_approach=data.get("selected_approach", ""),
            locked=data.get("locked", False),
            locked_at=data.get("locked_at", 0.0),
            locked_by=data.get("locked_by", ""),
            surgeon_decision_id=data.get("surgeon_decision_id", ""),
            created_at=data.get("created_at", time.time()),
        )

    def lock(self, node_id: str) -> None:
        """Lock the contract — no further changes."""
        if self.locked:
            raise ValueError(f"Contract {self.id} already locked by {self.locked_by}")
        self.locked = True
        self.locked_at = time.time()
        self.locked_by = node_id

    def is_satisfied(self, completed_criteria: list[str]) -> bool:
        """Check if all done_criteria are met."""
        return all(c in completed_criteria for c in self.done_criteria)


# ── ExecutionLedger ───────────────────────────────────────────────────────


@dataclass
class ExecutionStep:
    """A single step in an execution plan."""
    step_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "blocked", "skipped"] = "pending"
    started_at: float = 0.0
    completed_at: float = 0.0
    evidence_refs: list[str] = field(default_factory=list)
    probe_results: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionStep":
        return cls(
            step_id=data["step_id"],
            description=data["description"],
            status=data.get("status", "pending"),
            started_at=data.get("started_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
            evidence_refs=data.get("evidence_refs", []),
            probe_results=data.get("probe_results", []),
        )


@dataclass
class ExecutionLedger:
    """Tracks execution progress against a VisionContract."""
    id: str = field(default_factory=_short_id)
    task_id: str = ""
    vision_contract_id: str = ""
    node_id: str = ""
    steps: list[ExecutionStep] = field(default_factory=list)
    iteration: int = 1
    created_at: float = field(default_factory=time.time)

    @property
    def progress(self) -> float:
        """Fraction of steps completed (0.0 to 1.0)."""
        if not self.steps:
            return 0.0
        done = sum(1 for s in self.steps if s.status == "done")
        return done / len(self.steps)

    @property
    def is_blocked(self) -> bool:
        return any(s.status == "blocked" for s in self.steps)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [s.to_dict() for s in self.steps]
        # Include computed properties
        d["progress"] = self.progress
        d["is_blocked"] = self.is_blocked
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionLedger":
        steps = [ExecutionStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            id=data.get("id", _short_id()),
            task_id=data.get("task_id", ""),
            vision_contract_id=data.get("vision_contract_id", ""),
            node_id=data.get("node_id", ""),
            steps=steps,
            iteration=data.get("iteration", 1),
            created_at=data.get("created_at", time.time()),
        )

    def add_step(self, description: str, step_id: str = "") -> ExecutionStep:
        """Add a step to the plan."""
        step = ExecutionStep(
            step_id=step_id or _short_id(),
            description=description,
        )
        self.steps.append(step)
        return step

    def advance_step(self, step_id: str, status: str, evidence: str = "") -> bool:
        """Move a step to a new status. Returns True if found."""
        for s in self.steps:
            if s.step_id == step_id:
                s.status = status
                if status == "in_progress" and s.started_at == 0.0:
                    s.started_at = time.time()
                if status in ("done", "skipped"):
                    s.completed_at = time.time()
                if evidence:
                    s.evidence_refs.append(evidence)
                return True
        return False


# ── ChiefEscalationPacket ────────────────────────────────────────────────


ESCALATION_REASONS = ("no_consensus", "high_risk", "vision_conflict",
                      "repeated_failure", "dao_relevant")
URGENCY_LEVELS = ("low", "medium", "high", "critical")


@dataclass
class ChiefEscalationPacket:
    """Escalation to chief when local consensus fails or high-risk detected."""
    id: str = field(default_factory=_short_id)
    task_id: str = ""
    from_node: str = ""
    reason: Literal["no_consensus", "high_risk", "vision_conflict",
                     "repeated_failure", "dao_relevant"] = "no_consensus"
    surgeon_decision_id: str = ""
    brainstorm_id: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    probe_findings: list[str] = field(default_factory=list)
    urgency: Literal["low", "medium", "high", "critical"] = "medium"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChiefEscalationPacket":
        return cls(
            id=data.get("id", _short_id()),
            task_id=data.get("task_id", ""),
            from_node=data.get("from_node", ""),
            reason=data.get("reason", "no_consensus"),
            surgeon_decision_id=data.get("surgeon_decision_id", ""),
            brainstorm_id=data.get("brainstorm_id", ""),
            evidence_refs=data.get("evidence_refs", []),
            probe_findings=data.get("probe_findings", []),
            urgency=data.get("urgency", "medium"),
            created_at=data.get("created_at", time.time()),
        )


# ── ChiefDecisionPacket ──────────────────────────────────────────────────


CHIEF_DECISIONS = ("approve", "reject", "modify", "defer", "split")


@dataclass
class ChiefDecisionPacket:
    """Chief's response to an escalation — the authoritative decision."""
    id: str = field(default_factory=_short_id)
    escalation_id: str = ""
    task_id: str = ""
    decision: Literal["approve", "reject", "modify", "defer", "split"] = "approve"
    reasoning: str = ""
    memory_refs: list[str] = field(default_factory=list)  # DAO/wisdom references
    modifications: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChiefDecisionPacket":
        return cls(
            id=data.get("id", _short_id()),
            escalation_id=data.get("escalation_id", ""),
            task_id=data.get("task_id", ""),
            decision=data.get("decision", "approve"),
            reasoning=data.get("reasoning", ""),
            memory_refs=data.get("memory_refs", []),
            modifications=data.get("modifications", []),
            created_at=data.get("created_at", time.time()),
        )


# ── Convenience: all packet types ────────────────────────────────────────

VISION_PACKET_TYPES = {
    "brainstorm": BrainstormPacket,
    "surgeon_decision": SurgeonDecisionPacket,
    "vision_contract": VisionContract,
    "execution_ledger": ExecutionLedger,
    "chief_escalation": ChiefEscalationPacket,
    "chief_decision_v2": ChiefDecisionPacket,
}


def packet_from_dict(packet_type: str, data: dict):
    """Deserialize a vision packet by type name."""
    cls = VISION_PACKET_TYPES.get(packet_type)
    if cls is None:
        raise ValueError(f"Unknown vision packet type: {packet_type}")
    return cls.from_dict(data)
