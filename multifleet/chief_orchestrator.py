"""Chief Surgeon Orchestrator — 5-phase cross-machine surgical protocol.

Implements the full "3x3x3" pattern from the Multi-Fleet architecture:
3 surgeon cells (Atlas/Cardiologist/Neurologist) x 3 MacBooks x 3 phases
(investigate/challenge/synthesize), coordinated by a Chief Surgeon that
sits above the three machines.

The Chief does NOT micromanage code edits. It only:
- Assigns scope to each machine cell
- Receives synthesized local verdicts
- Requests rebuttals / cross-reviews
- Declares consensus or preserves dissent
- Issues merge guidance backed by ContextDNA memory

Phases (faithful to 3-Surgeons protocol):
  1. ASSIGN   — Chief sends task brief to all machines
  2. OPERATE  — Each machine runs local surgery independently
  3. PUBLISH  — Each head surgeon emits local verdict packet
  4. REVIEW   — Cross-machine critique (Mac1->Mac2, Mac2->Mac3, Mac3->Mac1)
  5. CLOSE    — Chief synthesis with memory-backed adjudication

Integrates with existing:
- ChiefSynthesis (synthesis.py) for verdict aggregation
- CrossRebuttal (cross_rebuttal.py) for state machine
- ReviewFlowOrchestrator (review_flow.py) for end-to-end piping
- SurgeonProxy (coordinator_surgeon_proxy.py) for local surgeon calls
- EvidenceLedger for audit trail
- NATS for real-time packet delivery
- FleetHierarchy for role validation

Usage:
    chief = ChiefOrchestrator(node_id="mac1")
    result = await chief.run_surgical_protocol(
        task="Fix auth token refresh",
        machines=["mac1", "mac2", "mac3"],
        approach_hints={"mac1": "straightforward", "mac2": "alternative", "mac3": "hardening"},
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet-nerve.chief-orchestrator")


# ── Protocol phases ─────────────────────────────────────────────────────────


class SurgicalPhase(Enum):
    """5-phase cross-machine surgical protocol."""
    ASSIGN = "assign"
    OPERATE = "operate"
    PUBLISH = "publish"
    REVIEW = "review"
    CLOSE = "close"


PHASE_ORDER = [
    SurgicalPhase.ASSIGN,
    SurgicalPhase.OPERATE,
    SurgicalPhase.PUBLISH,
    SurgicalPhase.REVIEW,
    SurgicalPhase.CLOSE,
]


# ── Packet types for 3s-LAN/v1 ─────────────────────────────────────────────


PROTOCOL_VERSION = "3s-lan/v1"


def _make_packet(
    ptype: str,
    from_node: str,
    to_node: str = "all",
    **payload: Any,
) -> dict:
    """Build a 3s-LAN/v1 packet."""
    return {
        "protocol": PROTOCOL_VERSION,
        "type": ptype,
        "id": str(uuid.uuid4()),
        "from_node": from_node,
        "to_node": to_node,
        "timestamp": time.time(),
        **payload,
    }


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class MachineAssignment:
    """What each machine is assigned to work on."""
    machine_id: str
    approach: str  # "straightforward", "alternative", "hardening", or custom
    task_brief: str
    branch: str = ""  # Filled once machine starts work
    workspace: str = ""

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "approach": self.approach,
            "task_brief": self.task_brief,
            "branch": self.branch,
            "workspace": self.workspace,
        }


@dataclass
class LocalVerdict:
    """Synthesized verdict from one machine's local surgeon cell."""
    machine_id: str
    summary: str
    branch: str
    confidence: float
    files_touched: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    dissent: list[dict] = field(default_factory=list)
    phase: str = "close"
    timestamp: float = field(default_factory=time.time)

    def to_packet(self) -> dict:
        return _make_packet(
            "local_verdict",
            from_node=self.machine_id,
            machine_id=self.machine_id,
            summary=self.summary,
            branch=self.branch,
            confidence=self.confidence,
            files_touched=self.files_touched,
            risks=self.risks,
            dissent=self.dissent,
            phase=self.phase,
        )

    @classmethod
    def from_dict(cls, data: dict) -> LocalVerdict:
        return cls(
            machine_id=data.get("machine_id", data.get("from_node", "unknown")),
            summary=data.get("summary", ""),
            branch=data.get("branch", ""),
            confidence=data.get("confidence", 0.0),
            files_touched=data.get("files_touched", []),
            risks=data.get("risks", []),
            dissent=data.get("dissent", []),
            phase=data.get("phase", "close"),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class CrossCritique:
    """One machine's critique of another machine's work."""
    from_machine: str
    target_machine: str
    summary: str
    confidence: float
    blocking_concerns: list[str] = field(default_factory=list)
    suggested_action: str = ""  # "merge", "revise", "reject"
    timestamp: float = field(default_factory=time.time)

    def to_packet(self) -> dict:
        return _make_packet(
            "critique",
            from_node=self.from_machine,
            to_node=self.target_machine,
            summary=self.summary,
            confidence=self.confidence,
            blocking_concerns=self.blocking_concerns,
            suggested_action=self.suggested_action,
            target_machine=self.target_machine,
        )

    @classmethod
    def from_dict(cls, data: dict) -> CrossCritique:
        return cls(
            from_machine=data.get("from_node", data.get("from_machine", "")),
            target_machine=data.get("target_machine", data.get("to_node", "")),
            summary=data.get("summary", ""),
            confidence=data.get("confidence", 0.0),
            blocking_concerns=data.get("blocking_concerns", []),
            suggested_action=data.get("suggested_action", ""),
            timestamp=data.get("timestamp", time.time()),
        )


@dataclass
class ChiefDecision:
    """Chief's final decision after cross-machine synthesis."""
    task_id: str
    decision: str  # merge | cherry_pick | reconciliation | reject | needs_rebuttal
    winner_branch: str
    confidence: float
    reasoning: str
    cherry_pick_from: list[dict] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    dissent_preserved: list[dict] = field(default_factory=list)
    memory_reasons: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_packet(self) -> dict:
        return _make_packet(
            "chief_decision",
            from_node="chief",
            task_id=self.task_id,
            decision=self.decision,
            winner_branch=self.winner_branch,
            confidence=self.confidence,
            reasoning=self.reasoning,
            cherry_pick_from=self.cherry_pick_from,
            followups=self.followups,
            dissent_preserved=self.dissent_preserved,
            memory_reasons=self.memory_reasons,
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "decision": self.decision,
            "winner_branch": self.winner_branch,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "cherry_pick_from": self.cherry_pick_from,
            "followups": self.followups,
            "dissent_preserved": self.dissent_preserved,
            "memory_reasons": self.memory_reasons,
            "timestamp": self.timestamp,
        }


@dataclass
class SurgicalProtocolResult:
    """Full result of a cross-machine surgical protocol execution."""
    task_id: str
    task: str
    phase: SurgicalPhase
    assignments: list[dict] = field(default_factory=list)
    verdicts: dict = field(default_factory=dict)     # machine_id -> LocalVerdict
    critiques: dict = field(default_factory=dict)     # machine_id -> CrossCritique
    decision: Optional[ChiefDecision] = None
    machines_participated: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task": self.task,
            "phase": self.phase.value,
            "assignments": self.assignments,
            "verdicts": {
                k: v.to_packet() if isinstance(v, LocalVerdict) else v
                for k, v in self.verdicts.items()
            },
            "critiques": {
                k: v.to_packet() if isinstance(v, CrossCritique) else v
                for k, v in self.critiques.items()
            },
            "decision": self.decision.to_dict() if self.decision else None,
            "machines_participated": self.machines_participated,
            "duration_s": round(self.duration_s, 1),
            "error": self.error,
        }


# ── Default approach strategies ─────────────────────────────────────────────


DEFAULT_APPROACHES = {
    0: "straightforward",   # Most direct implementation path
    1: "alternative",       # Cleaner abstraction or different approach
    2: "hardening",         # Robustness, edge-cases, simplification
}


def assign_approaches(
    machines: list[str],
    hints: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assign approach strategies to machines.

    Uses hints if provided, otherwise distributes default approaches.
    Every machine has the same surgeon protocol — only the approach differs.
    """
    result = {}
    for i, machine in enumerate(machines):
        if hints and machine in hints:
            result[machine] = hints[machine]
        else:
            result[machine] = DEFAULT_APPROACHES.get(i, f"approach-{i}")
    return result


def build_critique_pairs(machines: list[str]) -> list[tuple[str, str]]:
    """Build round-robin critique pairs: each machine critiques the next.

    For 3 machines: [(mac1, mac2), (mac2, mac3), (mac3, mac1)]
    """
    if len(machines) < 2:
        return []
    pairs = []
    for i, machine in enumerate(machines):
        target = machines[(i + 1) % len(machines)]
        pairs.append((machine, target))
    return pairs


# ── Chief Orchestrator ──────────────────────────────────────────────────────


class ChiefOrchestrator:
    """Orchestrates the 5-phase cross-machine surgical protocol.

    The Chief sits above all machines and coordinates the full protocol:
    Assign -> Operate -> Publish -> Review -> Close.

    Integrates with existing ChiefSynthesis for verdict aggregation and
    CrossRebuttal for state machine tracking. Uses NATS for real-time
    delivery and P7 git for guaranteed async delivery.
    """

    def __init__(
        self,
        node_id: str = "chief",
        nats_url: str | None = None,
        synthesis: Any = None,
        evidence_ledger: Any = None,
        memory_bridge: Any = None,
    ):
        self.node_id = node_id
        self._nats_url = nats_url
        self._synthesis = synthesis
        self._evidence = evidence_ledger
        self._memory = memory_bridge

        # Active protocols
        self._active: dict[str, SurgicalProtocolResult] = {}

        # Callbacks for custom dispatch/collection
        self._dispatch_fn: Optional[Callable] = None
        self._collect_fn: Optional[Callable] = None

        # Lazy-init synthesis
        if self._synthesis is None:
            try:
                from multifleet.synthesis import ChiefSynthesis
                self._synthesis = ChiefSynthesis()
            except (ImportError, Exception) as e:
                logger.debug("ChiefSynthesis not available: %s", e)

        # Lazy-init memory bridge
        if self._memory is None:
            try:
                from multifleet.adapters.chief_sync import pull_chief_memory
                self._memory = pull_chief_memory
            except (ImportError, Exception) as e:
                logger.debug("ChiefSync memory bridge not available: %s", e)

    def set_dispatch_fn(self, fn: Callable) -> None:
        """Set custom function for dispatching packets to machines.

        fn(packet: dict, target_node: str) -> bool
        """
        self._dispatch_fn = fn

    def set_collect_fn(self, fn: Callable) -> None:
        """Set custom function for collecting verdicts/critiques.

        fn(task_id: str, expected_machines: list[str], timeout_s: float) -> dict
        """
        self._collect_fn = fn

    # ── Phase 1: Assign ──

    def phase_assign(
        self,
        task_id: str,
        task: str,
        machines: list[str],
        approach_hints: dict[str, str] | None = None,
    ) -> SurgicalProtocolResult:
        """Phase 1: Assign task brief to all machines.

        Each machine gets the same task but a different approach strategy.
        This preserves the 3-Surgeons discipline: same protocol, different
        operating rooms, different implementation directions.
        """
        approaches = assign_approaches(machines, approach_hints)

        result = SurgicalProtocolResult(
            task_id=task_id,
            task=task,
            phase=SurgicalPhase.ASSIGN,
            machines_participated=list(machines),
        )

        assignments = []
        for machine in machines:
            assignment = MachineAssignment(
                machine_id=machine,
                approach=approaches[machine],
                task_brief=task,
            )
            assignments.append(assignment)

            # Build task_brief packet
            packet = _make_packet(
                "task_brief",
                from_node=self.node_id,
                to_node=machine,
                task_id=task_id,
                task=task,
                approach=approaches[machine],
                machines=machines,
                approaches=approaches,
            )

            # Dispatch
            if self._dispatch_fn:
                try:
                    self._dispatch_fn(packet, machine)
                except Exception as e:
                    logger.error("Failed to dispatch to %s: %s", machine, e)

        result.assignments = [a.to_dict() for a in assignments]
        self._active[task_id] = result

        logger.info(
            "Phase ASSIGN: task=%s machines=%s approaches=%s",
            task_id[:8], machines, approaches,
        )
        return result

    # ── Phase 2/3: Operate + Publish (happens on each machine) ──

    def receive_verdict(self, task_id: str, verdict_data: dict) -> bool:
        """Receive a local verdict from a machine (Phase 2/3: Operate+Publish).

        Returns True if all expected verdicts are collected.
        """
        if task_id not in self._active:
            logger.warning("Verdict for unknown task: %s", task_id[:8])
            return False

        result = self._active[task_id]
        verdict = LocalVerdict.from_dict(verdict_data)
        result.verdicts[verdict.machine_id] = verdict
        result.phase = SurgicalPhase.PUBLISH

        logger.info(
            "Verdict received: task=%s machine=%s confidence=%.2f (%d/%d)",
            task_id[:8], verdict.machine_id, verdict.confidence,
            len(result.verdicts), len(result.machines_participated),
        )

        return len(result.verdicts) >= len(result.machines_participated)

    # ── Phase 4: Review (cross-machine critique) ──

    def phase_review(self, task_id: str) -> list[tuple[str, str]]:
        """Phase 4: Dispatch cross-machine critique requests.

        Each machine reviews the next machine's verdict in a round-robin.
        Returns the critique pairs dispatched.
        """
        if task_id not in self._active:
            logger.warning("Review for unknown task: %s", task_id[:8])
            return []

        result = self._active[task_id]
        result.phase = SurgicalPhase.REVIEW

        pairs = build_critique_pairs(result.machines_participated)

        for reviewer, target in pairs:
            target_verdict = result.verdicts.get(target)
            if not target_verdict:
                logger.warning("No verdict from %s to critique", target)
                continue

            # Build review_request packet
            packet = _make_packet(
                "review_request",
                from_node=self.node_id,
                to_node=reviewer,
                task_id=task_id,
                target_machine=target,
                target_branch=target_verdict.branch,
                target_verdict=target_verdict.to_packet(),
                focus="cross-machine review",
            )

            if self._dispatch_fn:
                try:
                    self._dispatch_fn(packet, reviewer)
                except Exception as e:
                    logger.error("Failed to dispatch review to %s: %s", reviewer, e)

        logger.info(
            "Phase REVIEW: task=%s pairs=%s",
            task_id[:8], [(r, t) for r, t in pairs],
        )
        return pairs

    def receive_critique(self, task_id: str, critique_data: dict) -> bool:
        """Receive a critique from a machine.

        Returns True if all expected critiques are collected.
        """
        if task_id not in self._active:
            logger.warning("Critique for unknown task: %s", task_id[:8])
            return False

        result = self._active[task_id]
        critique = CrossCritique.from_dict(critique_data)
        result.critiques[critique.from_machine] = critique

        expected = len(result.machines_participated)
        logger.info(
            "Critique received: task=%s from=%s (%d/%d)",
            task_id[:8], critique.from_machine,
            len(result.critiques), expected,
        )

        return len(result.critiques) >= expected

    # ── Phase 5: Close (chief synthesis) ──

    def phase_close(self, task_id: str) -> ChiefDecision:
        """Phase 5: Chief synthesis — produce final decision.

        Aggregates all verdicts and critiques, queries ContextDNA memory
        for relevant history, and produces the chief decision.

        This is what makes the chief special: not just "pick the best diff"
        but "compare against institutional memory, prior failures, known
        invariants, and evidence-backed patterns."
        """
        if task_id not in self._active:
            raise ValueError(f"No active protocol for task {task_id}")

        result = self._active[task_id]
        result.phase = SurgicalPhase.CLOSE

        # Collect memory context if available
        memory_reasons: list[str] = []
        if callable(self._memory):
            try:
                mem_ctx = self._memory(result.task)
                if hasattr(mem_ctx, "prior_failures") and mem_ctx.prior_failures:
                    memory_reasons.append(
                        f"Prior failures: {'; '.join(mem_ctx.prior_failures[:3])}"
                    )
                if hasattr(mem_ctx, "prior_successes") and mem_ctx.prior_successes:
                    memory_reasons.append(
                        f"Prior successes: {'; '.join(mem_ctx.prior_successes[:3])}"
                    )
                if hasattr(mem_ctx, "professor_guidance") and mem_ctx.professor_guidance:
                    memory_reasons.append(f"Professor: {mem_ctx.professor_guidance[:100]}")
            except Exception as e:
                logger.debug("Memory query failed: %s", e)

        # Use ChiefSynthesis for verdict aggregation
        decision_data = self._run_synthesis(task_id, result, memory_reasons)

        decision = ChiefDecision(
            task_id=task_id,
            decision=decision_data.get("decision", "needs_rebuttal"),
            winner_branch=decision_data.get("winner_branch", ""),
            confidence=decision_data.get("confidence", 0.0),
            reasoning=decision_data.get("reasoning", ""),
            cherry_pick_from=decision_data.get("cherry_pick_from", []),
            followups=decision_data.get("followups", []),
            dissent_preserved=decision_data.get("dissent_preserved", []),
            memory_reasons=memory_reasons,
        )

        result.decision = decision

        # Dispatch decision to all machines
        packet = decision.to_packet()
        for machine in result.machines_participated:
            if self._dispatch_fn:
                try:
                    self._dispatch_fn(packet, machine)
                except Exception as e:
                    logger.error("Failed to dispatch decision to %s: %s", machine, e)

        # Record to evidence
        if self._evidence:
            try:
                self._evidence.record(
                    "chief_decision",
                    f"Chief decision for {task_id}: {decision.decision}",
                    decision.to_dict(),
                )
            except Exception as e:
                logger.debug("Evidence recording failed: %s", e)

        logger.info(
            "Phase CLOSE: task=%s decision=%s winner=%s confidence=%.2f",
            task_id[:8], decision.decision, decision.winner_branch, decision.confidence,
        )
        return decision

    def _run_synthesis(
        self,
        task_id: str,
        result: SurgicalProtocolResult,
        memory_reasons: list[str],
    ) -> dict:
        """Run ChiefSynthesis on collected verdicts and critiques."""
        if self._synthesis is None:
            return self._basic_synthesis(task_id, result, memory_reasons)

        # Convert verdicts to format ChiefSynthesis expects
        verdict_dicts = {}
        for machine_id, verdict in result.verdicts.items():
            if isinstance(verdict, LocalVerdict):
                verdict_dicts[machine_id] = {
                    "summary": verdict.summary,
                    "branch": verdict.branch,
                    "confidence": verdict.confidence,
                    "files_touched": verdict.files_touched,
                    "risks": verdict.risks,
                    "dissent": verdict.dissent,
                }
            else:
                verdict_dicts[machine_id] = verdict

        try:
            synth_result = self._synthesis.synthesize(
                task_id=task_id,
                verdicts=verdict_dicts,
            )
            decision = synth_result.to_chief_decision()
            decision["memory_reasons"] = memory_reasons
            return decision
        except Exception as e:
            logger.warning("ChiefSynthesis failed, falling back: %s", e)
            return self._basic_synthesis(task_id, result, memory_reasons)

    def _basic_synthesis(
        self,
        task_id: str,
        result: SurgicalProtocolResult,
        memory_reasons: list[str],
    ) -> dict:
        """Fallback synthesis when ChiefSynthesis is unavailable."""
        if not result.verdicts:
            return {
                "decision": "reject",
                "reasoning": "No verdicts received",
                "confidence": 0.0,
            }

        # Find highest-confidence verdict
        best_machine = max(
            result.verdicts,
            key=lambda m: (
                result.verdicts[m].confidence
                if isinstance(result.verdicts[m], LocalVerdict)
                else result.verdicts[m].get("confidence", 0)
            ),
        )
        best = result.verdicts[best_machine]
        best_conf = (
            best.confidence if isinstance(best, LocalVerdict)
            else best.get("confidence", 0)
        )
        best_branch = (
            best.branch if isinstance(best, LocalVerdict)
            else best.get("branch", "")
        )

        # Collect all dissent
        all_dissent = []
        for machine_id, verdict in result.verdicts.items():
            if isinstance(verdict, LocalVerdict):
                for d in verdict.dissent:
                    all_dissent.append({**d, "machine": machine_id})
            elif isinstance(verdict, dict):
                for d in verdict.get("dissent", []):
                    all_dissent.append({**d, "machine": machine_id})

        # Check critiques for blocking concerns
        blocking = []
        for machine_id, critique in result.critiques.items():
            if isinstance(critique, CrossCritique):
                blocking.extend(critique.blocking_concerns)
            elif isinstance(critique, dict):
                blocking.extend(critique.get("blocking_concerns", []))

        decision = "merge" if not blocking and best_conf >= 0.7 else "needs_rebuttal"
        if blocking:
            decision = "needs_rebuttal"

        return {
            "decision": decision,
            "winner_branch": best_branch,
            "confidence": best_conf,
            "reasoning": f"Best verdict from {best_machine} (conf={best_conf:.2f}). "
                        f"{len(blocking)} blocking concern(s). "
                        f"{len(all_dissent)} dissent(s) preserved.",
            "dissent_preserved": all_dissent,
            "followups": blocking,
            "memory_reasons": memory_reasons,
        }

    # ── Full protocol runner ────────────────────────────────────────────

    async def run_surgical_protocol(
        self,
        task: str,
        machines: list[str] | None = None,
        approach_hints: dict[str, str] | None = None,
        verdict_timeout_s: float = 300.0,
        critique_timeout_s: float = 180.0,
        poll_interval_s: float = 5.0,
    ) -> SurgicalProtocolResult:
        """Run the complete 5-phase cross-machine surgical protocol.

        This is the main entry point. It coordinates all phases end-to-end:
        Assign -> Operate (local) -> Publish -> Review -> Close.

        Args:
            task: Task description.
            machines: List of machine IDs (default: mac1/mac2/mac3).
            approach_hints: Optional approach per machine.
            verdict_timeout_s: Max seconds to wait for verdicts.
            critique_timeout_s: Max seconds to wait for critiques.
            poll_interval_s: Polling interval for verdict/critique collection.

        Returns:
            SurgicalProtocolResult with full protocol trace.
        """
        if machines is None:
            machines = ["mac1", "mac2", "mac3"]

        task_id = str(uuid.uuid4())[:8]
        start = time.time()

        # Wire: get architecture blueprint before surgery (memory/context.py)
        _blueprint_context = ""
        try:
            from memory.context import before_work
            _blueprint_context = before_work(task=task) or ""
            if _blueprint_context:
                logger.info(
                    "Surgical protocol: got %d-char blueprint for task=%s",
                    len(_blueprint_context), task_id[:8],
                )
        except Exception as _bpe:
            logger.debug("Blueprint lookup unavailable: %s", _bpe)

        logger.info(
            "Starting surgical protocol: task=%s machines=%s",
            task_id, machines,
        )

        # Phase 1: Assign (enrich task with blueprint context if available)
        enriched_task = task
        if _blueprint_context:
            enriched_task = f"{task}\n\n[Blueprint Context]\n{_blueprint_context[:2000]}"
        result = self.phase_assign(task_id, enriched_task, machines, approach_hints)

        # Phase 2/3: Wait for verdicts (machines operate independently)
        all_collected = False
        deadline = time.time() + verdict_timeout_s
        while not all_collected and time.time() < deadline:
            await asyncio.sleep(poll_interval_s)

            # If collect_fn is set, poll for verdicts
            if self._collect_fn:
                try:
                    collected = self._collect_fn(task_id, machines, poll_interval_s)
                    for machine_id, verdict_data in collected.items():
                        if machine_id not in result.verdicts:
                            self.receive_verdict(task_id, verdict_data)
                except Exception as e:
                    logger.debug("Collection poll error: %s", e)

            all_collected = len(result.verdicts) >= len(machines)

        if not all_collected:
            missing = set(machines) - set(result.verdicts.keys())
            logger.warning(
                "Verdict timeout: task=%s missing=%s",
                task_id[:8], missing,
            )

        # Phase 4: Cross-review
        if result.verdicts:
            self.phase_review(task_id)

            # Wait for critiques
            critique_deadline = time.time() + critique_timeout_s
            all_critiques = False
            while not all_critiques and time.time() < critique_deadline:
                await asyncio.sleep(poll_interval_s)
                all_critiques = len(result.critiques) >= len(machines)

        # Phase 5: Chief synthesis
        try:
            self.phase_close(task_id)
        except Exception as e:
            result.error = str(e)
            logger.error("Synthesis failed: %s", e)

        result.duration_s = time.time() - start

        logger.info(
            "Surgical protocol complete: task=%s decision=%s duration=%.1fs",
            task_id[:8],
            result.decision.decision if result.decision else "none",
            result.duration_s,
        )

        return result

    # ── State queries ───────────────────────────────────────────────────

    def get_active_protocols(self) -> dict[str, dict]:
        """Return all active protocol results."""
        return {
            tid: r.to_dict() for tid, r in self._active.items()
        }

    def get_protocol(self, task_id: str) -> Optional[dict]:
        """Return a specific protocol result."""
        r = self._active.get(task_id)
        return r.to_dict() if r else None

    def clear_completed(self) -> int:
        """Remove completed protocols from active tracking. Returns count."""
        completed = [
            tid for tid, r in self._active.items()
            if r.phase == SurgicalPhase.CLOSE
        ]
        for tid in completed:
            del self._active[tid]
        return len(completed)
