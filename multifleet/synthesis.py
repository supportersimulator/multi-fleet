"""Chief Synthesis Engine — aggregates fleet verdicts into merge decisions.

Generic by design: multi-fleet provides the aggregation framework,
consumers (3-surgeons, superpowers) register semantic analyzers.

The engine:
1. Collects local_verdict packets from all machines
2. Runs registered analyzers (pluggable)
3. Produces chief_decision packet
4. Logs to decision ledger
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("fleet-nerve.synthesis")


@dataclass
class SynthesisResult:
    """Result of synthesizing verdicts into a merge decision."""

    task_id: str
    decision: str  # merge | cherry_pick | reconciliation | reject | needs_rebuttal
    winner_branch: Optional[str]
    confidence: float
    reasoning: str
    cherry_pick_from: list = field(default_factory=list)  # [{machine, commit, reason}]
    followups: list = field(default_factory=list)
    dissent_preserved: list = field(default_factory=list)  # [{surgeon, machine, claim}]
    metadata: dict = field(default_factory=dict)  # extensible metadata (e.g. memory_reasons)

    def to_chief_decision(self) -> dict:
        """Convert to chief_decision packet payload."""
        return {
            "type": "chief_decision",
            "task_id": self.task_id,
            "decision": self.decision,
            "winner_branch": self.winner_branch,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "cherry_pick_from": self.cherry_pick_from,
            "followups": self.followups,
            "dissent_preserved": self.dissent_preserved,
            "dissent_count": len(self.dissent_preserved),
            "metadata": self.metadata,
            "timestamp": time.time(),
        }


class ChiefSynthesis:
    """Aggregates verdicts and produces merge decisions."""

    def __init__(self, ledger_path: Optional[str] = None):
        self._ledger_path = (
            Path(ledger_path)
            if ledger_path
            else Path.home() / ".fleet-nerve" / "decisions.jsonl"
        )
        self._analyzers: list[Callable] = []
        self._default_analyzer = self._basic_confidence_analyzer
        self._evidence_ledger = None

        # Auto-register evidence ledger if available
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            self._evidence_ledger = EvidenceLedger()
            logger.info("Evidence ledger connected to synthesis")
        except (ImportError, Exception) as e:
            logger.debug("Evidence ledger not available: %s", e)

        # Auto-register ContextDNA analyzer if available
        try:
            from multifleet.contextdna_analyzer import create_contextdna_analyzer

            analyzer = create_contextdna_analyzer()
            if analyzer:
                self.register_analyzer(analyzer)
                logger.info("ContextDNA memory analyzer registered")
        except ImportError:
            pass  # ContextDNA not available — use default scoring only

        # Auto-register Quorum analyzer if available
        self._quorum_registry = None
        try:
            from multifleet.quorum_analyzer import create_quorum_analyzer
            from multifleet.quorum.quorum_registry import QuorumRegistry

            self._quorum_registry = QuorumRegistry()
            q_analyzer = create_quorum_analyzer(registry=self._quorum_registry)
            self.register_analyzer(q_analyzer)
            logger.info("Quorum analyzer registered")
        except ImportError:
            pass  # Quorum system not available

    def register_analyzer(self, fn: Callable):
        """Register a semantic analyzer. Called with (task_id, verdicts) -> SynthesisResult."""
        self._analyzers.append(fn)

    def synthesize(self, task_id: str, verdicts: dict[str, dict]) -> SynthesisResult:
        """Run synthesis on collected verdicts.

        Args:
            task_id: The task being synthesized
            verdicts: {machine_id: verdict_payload} from all machines

        Returns:
            SynthesisResult with merge decision
        """
        if not verdicts:
            result = SynthesisResult(
                task_id=task_id,
                decision="reject",
                winner_branch=None,
                confidence=0.0,
                reasoning="No verdicts received",
            )
            result = self._apply_quorum_feedback(result)
            self._log_decision(result)
            return result

        t0 = time.monotonic()

        # Pull latest evidence from Context DNA before synthesis (bidirectional sync)
        contextdna_context: list[dict] = []
        try:
            from multifleet.product_bridge import get_bridge
            task_summary = next(
                (v.get("summary", "") for v in verdicts.values() if v.get("summary")), ""
            )
            if task_summary:
                cdna_result = get_bridge().memory_to_fleet(task_summary)
                contextdna_context = cdna_result.get("results", [])
                if contextdna_context:
                    logger.info("Pre-synthesis ContextDNA pull: %d evidence entries", len(contextdna_context))
        except Exception as e:
            logger.debug("Pre-synthesis ContextDNA pull failed: %s", e)

        # Run probes on the task context to get fresh evidence before analyzing
        probe_summary = None
        try:
            from multifleet.probe_engine import ProbeEngine
            engine = ProbeEngine()
            probe_results = engine.scan(trigger="synthesis", context={"task_id": task_id})
            if probe_results:
                probe_summary = engine.findings_summary()
                logger.info(
                    "Pre-synthesis probe scan: %d probes, %d findings",
                    probe_summary.get("total_probes", 0),
                    probe_summary.get("total_findings", 0),
                )
        except Exception as e:
            logger.debug("Pre-synthesis probe scan failed: %s", e)

        # Corroborate probe findings against historical evidence
        try:
            if probe_results:
                from multifleet.ghost_corroborate import GhostCorroborate
                corroborator = GhostCorroborate(
                    node_id=os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                )
                findings_list = []
                for pr in probe_results:
                    findings_list.extend(pr.get("findings", []) if isinstance(pr, dict) else [])
                if findings_list:
                    corr_result = corroborator.corroborate(findings_list)
                    if probe_summary is None:
                        probe_summary = {}
                    probe_summary["corroboration"] = corr_result.to_dict()
                    logger.info("Ghost corroborate: %s", corr_result.summary())
        except Exception as e:
            logger.debug("Ghost corroborate failed (non-blocking): %s", e)

        if self._analyzers:
            # Run registered analyzers, take highest-confidence result
            results = []
            for analyzer in self._analyzers:
                try:
                    result = analyzer(task_id, verdicts)
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.warning("Analyzer %s failed: %s", analyzer.__name__, e)
            if results:
                best = max(results, key=lambda r: r.confidence)
                if probe_summary:
                    best.metadata["probe_summary"] = probe_summary
                if contextdna_context:
                    best.metadata["contextdna_evidence_count"] = len(contextdna_context)
                # Resolve memory influence (the "holy shit" moment)
                best = self._apply_memory_influence(best, task_id, verdicts)
                # Wisdom feedback loop — proven wisdom checks the decision
                best = self._apply_wisdom_feedback(best)
                # Quorum feedback loop — enrich with stress/quorum + create proposals
                best = self._apply_quorum_feedback(best)
                self._log_decision(best, duration_ms=_elapsed_ms(t0))
                self._record_evidence(best)
                self._push_to_contextdna(best)
                return best

        # Fallback to basic confidence-weighted scoring
        result = self._default_analyzer(task_id, verdicts)
        if probe_summary:
            result.metadata["probe_summary"] = probe_summary
        if contextdna_context:
            result.metadata["contextdna_evidence_count"] = len(contextdna_context)
        # Resolve memory influence (the "holy shit" moment)
        result = self._apply_memory_influence(result, task_id, verdicts)
        # Wisdom feedback loop — proven wisdom checks the decision
        result = self._apply_wisdom_feedback(result)
        # Quorum feedback loop — enrich with stress/quorum + create proposals
        result = self._apply_quorum_feedback(result)
        self._log_decision(result, duration_ms=_elapsed_ms(t0))
        self._record_evidence(result)
        self._push_to_contextdna(result)
        # Track outcome for learning (high-value, zero-caller module)
        try:
            from memory.outcome_tracker import OutcomeTracker
            tracker = OutcomeTracker()
            outcome_id = tracker.start_tracking(
                task=result.task_id,
                approach=result.decision,
                expected_outcome=f"confidence={result.confidence:.3f}",
                related_patterns=[],
            )
            tracker.record_outcome(
                outcome_id=outcome_id,
                success=result.confidence >= 0.5,
                actual_outcome=result.reasoning[:200],
                verification_method="synthesis_confidence",
            )
        except Exception:
            pass
        return result

    def _basic_confidence_analyzer(
        self, task_id: str, verdicts: dict[str, dict]
    ) -> SynthesisResult:
        """Default analyzer: pick highest confidence, preserve all dissent.

        Scoring: confidence - (risk_count * 0.05) - (dissent_count * 0.1)
        Cherry-pick suggestions from non-winners with unique strengths.
        """
        scores: dict[str, float] = {}
        all_dissent: list[dict] = []

        for machine_id, verdict in verdicts.items():
            confidence = float(verdict.get("confidence", 0.0))
            risks = verdict.get("risks", [])
            dissents = verdict.get("dissent", [])

            risk_penalty = len(risks) * 0.05
            dissent_penalty = len(dissents) * 0.1
            scores[machine_id] = confidence - risk_penalty - dissent_penalty

            # Collect all dissent from all machines
            for d in dissents:
                entry = dict(d) if isinstance(d, dict) else {"claim": str(d)}
                entry.setdefault("machine", machine_id)
                all_dissent.append(entry)

        # Winner = highest score
        winner_id = max(scores, key=scores.get)  # type: ignore[arg-type]
        winner_verdict = verdicts[winner_id]
        winner_score = scores[winner_id]

        # Cherry-pick suggestions from non-winners with unique strengths
        cherry_picks: list[dict] = []
        for machine_id, verdict in verdicts.items():
            if machine_id == winner_id:
                continue
            strengths = verdict.get("strengths", [])
            commit = verdict.get("commit", verdict.get("branch", ""))
            for s in strengths:
                cherry_picks.append(
                    {
                        "machine": machine_id,
                        "commit": commit,
                        "reason": s if isinstance(s, str) else str(s),
                    }
                )

        # Determine decision type
        if len(verdicts) == 1:
            decision = "merge"
        elif cherry_picks:
            decision = "cherry_pick"
        elif len(all_dissent) > len(verdicts):
            decision = "needs_rebuttal"
        else:
            decision = "merge"

        # Build reasoning
        score_summary = ", ".join(
            f"{m}: {s:.3f}" for m, s in sorted(scores.items(), key=lambda x: -x[1])
        )
        reasoning = f"Scores: [{score_summary}]. Winner: {winner_id} ({winner_score:.3f})."
        if cherry_picks:
            reasoning += f" {len(cherry_picks)} cherry-pick suggestion(s) from non-winners."
        if all_dissent:
            reasoning += f" {len(all_dissent)} dissent(s) preserved."

        return SynthesisResult(
            task_id=task_id,
            decision=decision,
            winner_branch=winner_verdict.get("branch", winner_id),
            confidence=max(0.0, min(1.0, winner_score)),
            reasoning=reasoning,
            cherry_pick_from=cherry_picks,
            followups=winner_verdict.get("followups", []),
            dissent_preserved=all_dissent,
        )

    def _log_decision(self, result: SynthesisResult, duration_ms: float = 0.0):
        """Append decision to JSONL ledger."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": time.time(),
                "task_id": result.task_id,
                "decision": result.decision,
                "winner_branch": result.winner_branch,
                "confidence": round(result.confidence, 4),
                "reasoning": result.reasoning,
                "cherry_pick_from": result.cherry_pick_from,
                "dissent_count": len(result.dissent_preserved),
                "duration_ms": round(duration_ms, 1),
            }
            with open(self._ledger_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error("Failed to log decision: %s", e)

    def _apply_memory_influence(
        self, result: SynthesisResult, task_id: str, verdicts: dict
    ) -> SynthesisResult:
        """Query past decisions and attach memory influence to result.

        When confidence is low, evidence becomes the PRIMARY decision driver.
        This is the "holy shit" demo moment — showing which past memories
        influenced the current verdict.
        """
        try:
            from multifleet.memory_influence import MemoryInfluenceResolver
            resolver = MemoryInfluenceResolver(
                evidence_ledger=self._evidence_ledger,
            )
            influence = resolver.resolve(task_id, verdicts, result.confidence)

            # Attach to metadata
            result.metadata["memory_influence"] = influence.to_dict()

            # Apply confidence adjustment
            if influence.confidence_adjustment != 0:
                old_conf = result.confidence
                result.confidence = max(0.0, min(1.0,
                    result.confidence + influence.confidence_adjustment
                ))
                logger.info(
                    "Memory influence adjusted confidence: %.3f → %.3f (%s)",
                    old_conf, result.confidence, influence.summary_line(),
                )

            # When low confidence + evidence suggests different outcome:
            # evidence becomes PRIMARY decision driver
            if result.confidence < 0.7 and influence.evidence_refs:
                result.reasoning = (
                    f"**Evidence-backed decision**: {influence.summary_line()} "
                    f"Original reasoning: {result.reasoning}"
                )

        except (ImportError, Exception) as e:
            logger.debug("Memory influence unavailable: %s", e)

        return result

    def _apply_wisdom_feedback(self, result: SynthesisResult) -> SynthesisResult:
        """Post-synthesis wisdom check — does proven wisdom contradict this decision?

        Reads promoted wisdom from evidence ledger + fleet-wisdom files.
        Attaches warnings to metadata. Adjusts confidence downward if
        contradictions are found.
        """
        try:
            from multifleet.wisdom_consumer import WisdomConsumer
            consumer = WisdomConsumer(
                evidence_ledger=self._evidence_ledger,
                wisdom_path=getattr(self, '_wisdom_path', None),
            )
            feedback = consumer.feed_synthesis(result.to_chief_decision())

            result.metadata["wisdom_feedback"] = {
                "warnings_count": len(feedback["warnings"]),
                "supporting_count": len(feedback["supporting"]),
                "wisdom_checked": feedback["wisdom_checked"],
            }

            if feedback["warnings"]:
                # Each contradiction warning reduces confidence slightly
                penalty = min(0.15, len(feedback["warnings"]) * 0.05)
                old_conf = result.confidence
                result.confidence = max(0.0, result.confidence - penalty)
                result.metadata["wisdom_feedback"]["confidence_penalty"] = round(penalty, 3)

                warning_msgs = [w["message"] for w in feedback["warnings"]]
                result.reasoning = (
                    f"**Wisdom warning ({len(warning_msgs)} contradiction(s))**: "
                    f"{warning_msgs[0][:120]}... "
                    f"Original: {result.reasoning}"
                )
                logger.warning(
                    "Wisdom feedback: %d contradiction(s), confidence %.3f → %.3f",
                    len(warning_msgs), old_conf, result.confidence,
                )
            elif feedback["supporting"]:
                logger.info(
                    "Wisdom feedback: %d supporting entries, no contradictions",
                    len(feedback["supporting"]),
                )

        except (ImportError, Exception) as e:
            logger.debug("Wisdom feedback unavailable: %s", e)

        return result

    def _apply_quorum_feedback(self, result: SynthesisResult) -> SynthesisResult:
        """Post-synthesis quorum integration.

        Two-way coupling:
        1. Enrich result with stress/quorum context (quorum -> synthesis)
        2. Record decision as quorum proposal (synthesis -> quorum)
        """
        if not self._quorum_registry:
            return result
        try:
            from multifleet.quorum_analyzer import (
                enrich_result_with_quorum,
                apply_synthesis_to_quorum,
            )

            # Quorum -> Synthesis: stress gating + context enrichment
            result = enrich_result_with_quorum(result, self._quorum_registry)

            # Synthesis -> Quorum: decision becomes proposal
            feedback = apply_synthesis_to_quorum(result, self._quorum_registry)
            result.metadata["quorum_feedback"] = feedback

        except (ImportError, Exception) as e:
            logger.debug("Quorum feedback unavailable: %s", e)

        return result

    def _record_evidence(self, result: SynthesisResult):
        """Record synthesis decision to the evidence ledger (hash-chain)."""
        if not self._evidence_ledger:
            return
        if not hasattr(self._evidence_ledger, 'record'):
            logger.warning("Evidence ledger lacks .record() — skipping evidence write")
            return
        try:
            self._evidence_ledger.record(
                event_type="merge_decision",
                node_id=os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                subject=f"synthesis:{result.task_id} decision:{result.decision}",
                payload={
                    "task_id": result.task_id,
                    "decision": result.decision,
                    "winner_branch": result.winner_branch,
                    "confidence": round(result.confidence, 4),
                    "dissent_count": len(result.dissent_preserved),
                    "cherry_pick_count": len(result.cherry_pick_from),
                },
            )
        except Exception as e:
            logger.warning("Evidence ledger record failed: %s", e)

        # Wire 2: evidence already recorded in ledger above — ContextDNA sync
        # happens asynchronously via daemon's sync_cycle, not inline during synthesis
        # (inline push was blocking on MemoryBridge subprocess calls)

    def _push_to_contextdna(self, result: SynthesisResult):
        """Push synthesis decision to Context DNA via ProductBridge (closes learning loop)."""
        try:
            from multifleet.product_bridge import get_bridge
            bridge = get_bridge()
            bridge.fleet_to_memory({
                "event_type": "synthesis_decision",
                "node_id": os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                "evidence": {
                    "task_id": result.task_id,
                    "decision": result.decision,
                    "winner_branch": result.winner_branch,
                    "confidence": round(result.confidence, 4),
                    "reasoning": result.reasoning[:300],
                    "dissent_count": len(result.dissent_preserved),
                },
                "tags": ["synthesis", "fleet_decision"],
            })
        except Exception as e:
            logger.debug("fleet->contextdna bridge (non-blocking): %s", e)

    def recent_decisions(self, limit: int = 10) -> list[dict]:
        """Read recent decisions from ledger."""
        if not self._ledger_path.exists():
            return []
        try:
            lines = self._ledger_path.read_text().strip().splitlines()
            recent = lines[-limit:] if len(lines) > limit else lines
            results = []
            for line in reversed(recent):
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return results
        except Exception as e:
            logger.error("Failed to read decisions: %s", e)
            return []


def _elapsed_ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000
