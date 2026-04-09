"""Ghost Cross-Node Synthesis — reweights evidence from multiple nodes at chief level.

When 3 machines disagree: gather which node saw what, what evidence each had,
which invariants were active, which surgeon blocked what. Then reweight
EVIDENCE, not conclusions.

"Nodes exchange evidence, not conclusions."
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-cross-node")


@dataclass
class NodeEvidence:
    """Evidence from a single node about a task/topic."""

    node_id: str
    task_id: str
    decision: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)
    active_invariants: list[str] = field(default_factory=list)
    probe_findings: list[str] = field(default_factory=list)
    surgeon_objections: list[str] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "task_id": self.task_id,
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "evidence_refs": self.evidence_refs,
            "active_invariants": self.active_invariants,
            "probe_findings": self.probe_findings,
            "surgeon_objections": self.surgeon_objections,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NodeEvidence":
        return cls(
            node_id=d.get("node_id", ""),
            task_id=d.get("task_id", ""),
            decision=d.get("decision", ""),
            confidence=d.get("confidence", 0.0),
            evidence_refs=d.get("evidence_refs", []),
            active_invariants=d.get("active_invariants", []),
            probe_findings=d.get("probe_findings", []),
            surgeon_objections=d.get("surgeon_objections", []),
            timestamp=d.get("timestamp", ""),
        )


@dataclass
class SynthesisResult:
    """Result of cross-node evidence synthesis."""

    task_id: str
    nodes_contributing: list[str] = field(default_factory=list)
    consensus_decision: str = ""
    consensus_confidence: float = 0.0
    evidence_weight_by_node: dict = field(default_factory=dict)
    # {node_id: weight} — how much each node's evidence contributed
    unresolved_dissent: list[str] = field(default_factory=list)
    synthesis_method: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "nodes_contributing": self.nodes_contributing,
            "consensus_decision": self.consensus_decision,
            "consensus_confidence": round(self.consensus_confidence, 3),
            "evidence_weight_by_node": self.evidence_weight_by_node,
            "unresolved_dissent": self.unresolved_dissent,
            "synthesis_method": self.synthesis_method,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SynthesisResult":
        return cls(
            task_id=d.get("task_id", ""),
            nodes_contributing=d.get("nodes_contributing", []),
            consensus_decision=d.get("consensus_decision", ""),
            consensus_confidence=d.get("consensus_confidence", 0.0),
            evidence_weight_by_node=d.get("evidence_weight_by_node", {}),
            unresolved_dissent=d.get("unresolved_dissent", []),
            synthesis_method=d.get("synthesis_method", ""),
            timestamp=d.get("timestamp", ""),
        )


class CrossNodeSynthesizer:
    """Synthesizes evidence from multiple fleet nodes.

    Core principle: reweight EVIDENCE, not conclusions. A node's decision
    matters less than the evidence behind it. Nodes with more evidence refs,
    passing probes, and fewer surgeon objections get higher weight.
    """

    def __init__(self, synthesis_dir: Optional[str] = None):
        self._dir = Path(
            synthesis_dir
            or str(Path.home() / ".fleet-nerve" / "ghost" / "synthesis")
        )
        self._dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, task_id: str, node_evidence: list[NodeEvidence]) -> SynthesisResult:
        """Synthesize evidence from multiple nodes into a weighted consensus.

        Weight formula per node:
        - base = confidence
        - +0.1 per evidence_ref (capped at +0.3)
        - -0.1 per surgeon_objection (capped at -0.3)
        - +0.1 if all probes pass (no "high"/"critical" findings)
        - -0.2 if blocking probe findings present
        """
        if not node_evidence:
            return SynthesisResult(task_id=task_id, synthesis_method="empty")

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        weights: dict[str, float] = {}
        decisions: dict[str, list[str]] = {}  # decision -> [node_ids]

        for ne in node_evidence:
            w = ne.confidence

            # Evidence richness bonus
            w += min(0.3, len(ne.evidence_refs) * 0.1)

            # Surgeon objection penalty
            w += max(-0.3, -len(ne.surgeon_objections) * 0.1)

            # Probe health
            has_blocking = any(
                "critical" in f.lower() or "blocking" in f.lower()
                for f in ne.probe_findings
            )
            if has_blocking:
                w -= 0.2
            elif ne.probe_findings:  # probes ran and found nothing blocking
                w += 0.1

            weights[ne.node_id] = max(0.0, min(1.0, w))
            decisions.setdefault(ne.decision, []).append(ne.node_id)

        # Determine consensus by weighted vote
        decision_weights: dict[str, float] = {}
        for decision, nodes in decisions.items():
            decision_weights[decision] = sum(weights.get(n, 0) for n in nodes)

        if decision_weights:
            consensus = max(decision_weights, key=lambda d: decision_weights[d])
            total_weight = sum(decision_weights.values())
            consensus_conf = (
                decision_weights[consensus] / total_weight if total_weight > 0 else 0.0
            )
        else:
            consensus = "unknown"
            consensus_conf = 0.0

        # Collect dissent from nodes that disagreed
        dissent = []
        for ne in node_evidence:
            if ne.decision != consensus:
                dissent.append(
                    f"{ne.node_id}: {ne.decision} (conf={ne.confidence:.2f}, "
                    f"objections={ne.surgeon_objections})"
                )

        method = "weighted_evidence"
        if len(set(ne.decision for ne in node_evidence)) == 1:
            method = "unanimous"
        elif len(node_evidence) >= 3:
            method = "majority_weighted"

        result = SynthesisResult(
            task_id=task_id,
            nodes_contributing=[ne.node_id for ne in node_evidence],
            consensus_decision=consensus,
            consensus_confidence=consensus_conf,
            evidence_weight_by_node={k: round(v, 3) for k, v in weights.items()},
            unresolved_dissent=dissent,
            synthesis_method=method,
            timestamp=ts,
        )

        # Persist
        self._save(result)
        logger.info(
            "Cross-node synthesis for %s: %s (conf=%.2f, method=%s, dissent=%d)",
            task_id[:8], consensus, consensus_conf, method, len(dissent),
        )
        return result

    def get_latest(self, task_id: str) -> Optional[SynthesisResult]:
        """Get the latest synthesis for a task."""
        path = self._dir / f"{task_id[:20]}.json"
        if not path.exists():
            return None
        try:
            return SynthesisResult.from_dict(json.loads(path.read_text()))
        except Exception:
            return None

    def list_recent(self, limit: int = 10) -> list[SynthesisResult]:
        """List recent synthesis results."""
        results = []
        for f in sorted(self._dir.glob("*.json"), reverse=True)[:limit]:
            try:
                results.append(SynthesisResult.from_dict(json.loads(f.read_text())))
            except Exception:
                continue
        return results

    def _save(self, result: SynthesisResult) -> None:
        try:
            path = self._dir / f"{result.task_id[:20]}.json"
            path.write_text(json.dumps(result.to_dict(), indent=2))
        except Exception as e:
            logger.error("Failed to save synthesis: %s", e)
