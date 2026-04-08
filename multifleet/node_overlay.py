"""Node Overlay State — what each node thinks, how strongly, based on what.

5 strips per node:
1. Epistemic state: certainty, contradictions, stale context risk
2. Surgeon state: triage/operate/review/close phase, disagreement score
3. Invariant state: active invariants, violations, proof receipts
4. Evidence state: active evidence, candidates, promoted, unresolved objections
5. Probe findings: blocking count, warning count, top findings
"""

from dataclasses import dataclass, field
from typing import Optional
import logging
import time

logger = logging.getLogger("fleet-nerve.overlay")


# ═══════════════════════════════════════════════════════════════════
# Strip dataclasses — one per overlay dimension
# ═══════════════════════════════════════════════════════════════════


@dataclass
class EpistemicStrip:
    """What the node knows and how confident it is."""
    certainty_band: float = 0.5  # 0.0-1.0
    contradiction_count: int = 0
    stale_context_risk: float = 0.0  # 0.0-1.0
    last_packet_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "certainty_band": round(self.certainty_band, 3),
            "contradiction_count": self.contradiction_count,
            "stale_context_risk": round(self.stale_context_risk, 3),
            "last_packet_ts": self.last_packet_ts,
        }


@dataclass
class SurgeonStrip:
    """3-surgeon pipeline state for this node."""
    phase: str = "idle"  # triage/operate/review/close/idle
    disagreement_score: float = 0.0  # 0.0-1.0
    correction_loop_count: int = 0
    abstention_status: bool = False

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "disagreement_score": round(self.disagreement_score, 3),
            "correction_loop_count": self.correction_loop_count,
            "abstention_status": self.abstention_status,
        }


@dataclass
class InvariantStrip:
    """Invariant gate status for this node."""
    active_count: int = 0
    violated_count: int = 0
    proof_receipts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "active_count": self.active_count,
            "violated_count": self.violated_count,
            "proof_receipts": list(self.proof_receipts),
        }


@dataclass
class EvidenceStrip:
    """Evidence pipeline state for this node."""
    active_evidence_ids: list[str] = field(default_factory=list)
    candidate_count: int = 0
    promoted_count: int = 0
    unresolved_objection_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "active_evidence_ids": list(self.active_evidence_ids),
            "candidate_count": self.candidate_count,
            "promoted_count": self.promoted_count,
            "unresolved_objection_ids": list(self.unresolved_objection_ids),
        }


@dataclass
class ProbeStrip:
    """Probe engine findings summary for this node."""
    blocking_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    top_findings: list[str] = field(default_factory=list)
    last_scan_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "blocking_count": self.blocking_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "top_findings": list(self.top_findings),
            "last_scan_ts": self.last_scan_ts,
        }


# ═══════════════════════════════════════════════════════════════════
# NodeOverlay — composite of all 5 strips
# ═══════════════════════════════════════════════════════════════════


@dataclass
class NodeOverlay:
    """Complete overlay state for one fleet node."""
    node_id: str
    timestamp: float = field(default_factory=time.time)
    epistemic: EpistemicStrip = field(default_factory=EpistemicStrip)
    surgeon: SurgeonStrip = field(default_factory=SurgeonStrip)
    invariant: InvariantStrip = field(default_factory=InvariantStrip)
    evidence: EvidenceStrip = field(default_factory=EvidenceStrip)
    probe: ProbeStrip = field(default_factory=ProbeStrip)

    def health_score(self) -> float:
        """Compute overall node health from all strips.

        Returns 0.0 (critical) to 1.0 (perfect).
        Weights: epistemic 25%, surgeon 15%, invariant 25%, evidence 15%, probe 20%.
        """
        # Epistemic: certainty minus penalties for contradictions and staleness
        ep = max(0.0, self.epistemic.certainty_band
                 - 0.1 * self.epistemic.contradiction_count
                 - self.epistemic.stale_context_risk * 0.5)

        # Surgeon: 1.0 when idle/close, penalize disagreement and correction loops
        sg = 1.0 - self.surgeon.disagreement_score * 0.6
        sg -= min(0.3, self.surgeon.correction_loop_count * 0.1)
        if self.surgeon.abstention_status:
            sg -= 0.2
        sg = max(0.0, sg)

        # Invariant: 1.0 when no violations, 0.0 when all violated
        if self.invariant.active_count > 0:
            iv = 1.0 - (self.invariant.violated_count / self.invariant.active_count)
        else:
            iv = 1.0  # no invariants = no violations

        # Evidence: reward promoted, penalize unresolved objections
        ev = 1.0
        if self.evidence.candidate_count > 0:
            ev = self.evidence.promoted_count / max(1, self.evidence.candidate_count)
        ev -= 0.15 * len(self.evidence.unresolved_objection_ids)
        ev = max(0.0, min(1.0, ev))

        # Probe: penalize blockers heavily, warnings lightly
        pr = 1.0 - min(1.0, self.probe.blocking_count * 0.3 + self.probe.warning_count * 0.05)

        score = ep * 0.25 + sg * 0.15 + iv * 0.25 + ev * 0.15 + pr * 0.20
        return round(max(0.0, min(1.0, score)), 3)

    def to_dict(self) -> dict:
        """Serialize for transport/display."""
        return {
            "node_id": self.node_id,
            "timestamp": self.timestamp,
            "health_score": self.health_score(),
            "epistemic": self.epistemic.to_dict(),
            "surgeon": self.surgeon.to_dict(),
            "invariant": self.invariant.to_dict(),
            "evidence": self.evidence.to_dict(),
            "probe": self.probe.to_dict(),
        }


# ═══════════════════════════════════════════════════════════════════
# NodeOverlayManager — manages overlays for the entire fleet
# ═══════════════════════════════════════════════════════════════════


class NodeOverlayManager:
    """Manages overlay state for all fleet nodes."""

    def __init__(self):
        self._overlays: dict[str, NodeOverlay] = {}

    def update(self, node_id: str, **kwargs) -> NodeOverlay:
        """Update a node's overlay with new strip data.

        kwargs can include strip names as keys with dicts of field updates:
            update("mac1", epistemic={"certainty_band": 0.8})
        """
        if node_id not in self._overlays:
            self._overlays[node_id] = NodeOverlay(node_id=node_id)
        overlay = self._overlays[node_id]
        overlay.timestamp = time.time()

        for strip_name, updates in kwargs.items():
            strip = getattr(overlay, strip_name, None)
            if strip is None:
                logger.warning("Unknown strip %s for node %s", strip_name, node_id)
                continue
            if isinstance(updates, dict):
                for k, v in updates.items():
                    if hasattr(strip, k):
                        setattr(strip, k, v)
                    else:
                        logger.warning("Unknown field %s.%s", strip_name, k)

        return overlay

    def get(self, node_id: str) -> Optional[NodeOverlay]:
        """Get a node's current overlay."""
        return self._overlays.get(node_id)

    def all_overlays(self) -> list[NodeOverlay]:
        """Get all node overlays."""
        return list(self._overlays.values())

    def update_from_health(self, node_id: str, health_data: dict) -> NodeOverlay:
        """Update overlay from daemon /health response.

        Maps health fields to epistemic and probe strips.
        """
        updates: dict = {}

        # Map uptime/status to epistemic certainty
        status = health_data.get("status", "unknown")
        certainty = 0.8 if status == "ok" else 0.3 if status == "degraded" else 0.1
        peers = health_data.get("peers", {})
        # More peers = more corroboration = higher certainty
        certainty = min(1.0, certainty + len(peers) * 0.05)
        updates["epistemic"] = {
            "certainty_band": certainty,
            "last_packet_ts": time.time(),
        }

        # Channel health as stale context proxy
        ch = health_data.get("channel_health", {})
        if ch:
            avg_score = sum(p.get("score", 0) for p in ch.values()) / max(1, len(ch))
            updates["epistemic"]["stale_context_risk"] = round(1.0 - avg_score / 100.0, 3)

        return self.update(node_id, **updates)

    def update_from_probe(self, node_id: str, probe_results: list) -> NodeOverlay:
        """Update overlay from probe engine results.

        probe_results: list of ProbeResult dicts (with status, severity, findings).
        """
        blocking = 0
        warnings = 0
        infos = 0
        top: list[str] = []

        for pr in probe_results:
            status = pr.get("status", "ok")
            severity = pr.get("severity", "info")
            if status == "critical" or severity == "critical":
                blocking += 1
            elif status == "warning" or severity in ("high", "medium"):
                warnings += 1
            else:
                infos += 1
            for f in pr.get("findings", []):
                title = f.get("title", "")
                if title and len(top) < 5:
                    top.append(title)

        return self.update(node_id, probe={
            "blocking_count": blocking,
            "warning_count": warnings,
            "info_count": infos,
            "top_findings": top,
            "last_scan_ts": time.time(),
        })

    def update_from_surgeon(self, node_id: str, surgeon_state: dict) -> NodeOverlay:
        """Update overlay from 3-surgeon state.

        surgeon_state: dict with phase, disagreement_score, correction_loops, abstention.
        """
        return self.update(node_id, surgeon={
            "phase": surgeon_state.get("phase", "idle"),
            "disagreement_score": surgeon_state.get("disagreement_score", 0.0),
            "correction_loop_count": surgeon_state.get("correction_loops", 0),
            "abstention_status": surgeon_state.get("abstention", False),
        })
