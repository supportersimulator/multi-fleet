"""Ghost Layer — the unified orchestrator for ContextDNA's ambient cognition.

Ties together all 7 sub-modules of the ghost layer:
1. digest/     — turns completed work into candidate learnings (ghost_digest.py)
2. pulse/      — background epistemic health heartbeat (ghost_pulse.py)
3. packet-builder/ — builds next-turn epistemic packets (epistemic_packet.py)
4. unresolved-objections/ — stores surgeon reservations, probe red flags (ghost_objections.py)
5. evidence-candidates/ — normalized evidence from work artifacts (ghost_digest.py)
6. contradiction-clusters/ — groups conflicting truths (ghost_contradictions.py)
7. cross-node-synthesis/ — reweights evidence from multiple nodes (ghost_cross_node.py)

"The ghost is the part of the organism that refuses to let polished
output silently become truth."
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-layer")


@dataclass
class GhostStatus:
    """Current status of the ghost layer."""

    healthy: bool = True
    pulse_available: bool = False
    digest_available: bool = False
    checkpoint_available: bool = False
    objections_available: bool = False
    contradictions_available: bool = False
    cross_node_available: bool = False
    packet_builder_available: bool = False

    open_objections: int = 0
    unresolved_contradictions: int = 0
    pending_evidence_candidates: int = 0
    last_pulse_summary: str = ""
    last_checkpoint_id: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "modules": {
                "pulse": self.pulse_available,
                "digest": self.digest_available,
                "checkpoint": self.checkpoint_available,
                "objections": self.objections_available,
                "contradictions": self.contradictions_available,
                "cross_node": self.cross_node_available,
                "packet_builder": self.packet_builder_available,
            },
            "open_objections": self.open_objections,
            "unresolved_contradictions": self.unresolved_contradictions,
            "pending_evidence_candidates": self.pending_evidence_candidates,
            "last_pulse_summary": self.last_pulse_summary,
            "last_checkpoint_id": self.last_checkpoint_id,
            "errors": self.errors,
        }


class GhostLayer:
    """Unified orchestrator for the ContextDNA ghost layer.

    Provides a single entry point to:
    - Check overall ghost health
    - Run a full ghost cycle (pulse + contradiction scan + checkpoint)
    - Record and query objections
    - Trigger cross-node synthesis
    - Build epistemic packets

    All sub-module failures are caught and reported — the ghost layer
    degrades gracefully, never crashes the host.
    """

    def __init__(self, repo_root: str = ".", node_id: str = ""):
        self._repo_root = repo_root
        self._node_id = node_id or _detect_node_id()

        # Lazy-init sub-modules (import failures are caught)
        self._pulse = None
        self._digest = None
        self._checkpoint = None
        self._objections = None
        self._contradictions = None
        self._cross_node = None
        self._packet_builder = None

    # --- Module accessors (lazy, fault-tolerant) ---

    @property
    def pulse(self):
        if self._pulse is None:
            try:
                from multifleet.ghost_pulse import GhostPulse
                self._pulse = GhostPulse(repo_root=self._repo_root)
            except Exception as e:
                logger.warning("Ghost pulse unavailable: %s", e)
        return self._pulse

    @property
    def digest(self):
        if self._digest is None:
            try:
                from multifleet.ghost_digest import GhostDigest
                self._digest = GhostDigest(node_id=self._node_id)
            except Exception as e:
                logger.warning("Ghost digest unavailable: %s", e)
        return self._digest

    @property
    def checkpoint(self):
        if self._checkpoint is None:
            try:
                from multifleet.ghost_checkpoint import GhostCheckpoint
                self._checkpoint = GhostCheckpoint(repo_root=self._repo_root)
            except Exception as e:
                logger.warning("Ghost checkpoint unavailable: %s", e)
        return self._checkpoint

    @property
    def objections(self):
        if self._objections is None:
            try:
                from multifleet.ghost_objections import ObjectionStore
                self._objections = ObjectionStore()
            except Exception as e:
                logger.warning("Ghost objections unavailable: %s", e)
        return self._objections

    @property
    def contradictions(self):
        if self._contradictions is None:
            try:
                from multifleet.ghost_contradictions import ContradictionDetector
                self._contradictions = ContradictionDetector()
            except Exception as e:
                logger.warning("Ghost contradictions unavailable: %s", e)
        return self._contradictions

    @property
    def cross_node(self):
        if self._cross_node is None:
            try:
                from multifleet.ghost_cross_node import CrossNodeSynthesizer
                self._cross_node = CrossNodeSynthesizer()
            except Exception as e:
                logger.warning("Ghost cross-node unavailable: %s", e)
        return self._cross_node

    @property
    def packet_builder(self):
        if self._packet_builder is None:
            try:
                from multifleet.epistemic_packet import EpistemicPacketBuilder
                self._packet_builder = EpistemicPacketBuilder(repo_root=self._repo_root)
            except Exception as e:
                logger.warning("Ghost packet builder unavailable: %s", e)
        return self._packet_builder

    # --- High-level operations ---

    def status(self) -> GhostStatus:
        """Check overall ghost layer health."""
        s = GhostStatus()

        # Check each module
        s.pulse_available = self.pulse is not None
        s.digest_available = self.digest is not None
        s.checkpoint_available = self.checkpoint is not None
        s.objections_available = self.objections is not None
        s.contradictions_available = self.contradictions is not None
        s.cross_node_available = self.cross_node is not None
        s.packet_builder_available = self.packet_builder is not None

        # Get stats from available modules
        if s.objections_available:
            try:
                stats = self.objections.stats()
                s.open_objections = stats.get("by_status", {}).get("open", 0) + \
                    stats.get("by_status", {}).get("carried", 0)
            except Exception as e:
                s.errors.append(f"objections: {e}")

        if s.contradictions_available:
            try:
                stats = self.contradictions.stats()
                s.unresolved_contradictions = stats.get("unresolved", 0)
            except Exception as e:
                s.errors.append(f"contradictions: {e}")

        if s.checkpoint_available:
            try:
                checkpoints = self.checkpoint.list_checkpoints()
                if checkpoints:
                    s.last_checkpoint_id = checkpoints[0]["checkpoint_id"]
            except Exception as e:
                s.errors.append(f"checkpoint: {e}")

        # Count evidence candidates
        try:
            evidence_dir = Path.home() / ".fleet-nerve" / "evidence-candidates"
            if evidence_dir.exists():
                s.pending_evidence_candidates = len(list(evidence_dir.glob("*.json")))
        except Exception:
            pass

        s.healthy = not s.errors and sum([
            s.pulse_available,
            s.digest_available,
            s.checkpoint_available,
            s.objections_available,
        ]) >= 3  # At least 3 of 4 core modules

        return s

    def cycle(self, reason: str = "periodic") -> dict:
        """Run a full ghost cycle: pulse + contradiction scan + checkpoint.

        This is the main entry point for periodic ghost activity.
        Returns summary of what happened.
        """
        t0 = time.monotonic()
        results = {"reason": reason, "errors": []}

        # 1. Pulse — epistemic health check
        if self.pulse:
            try:
                pulse_result = self.pulse.pulse()
                results["pulse"] = pulse_result.to_dict()
            except Exception as e:
                results["errors"].append(f"pulse: {e}")
        else:
            results["errors"].append("pulse: unavailable")

        # 2. Contradiction scan
        if self.contradictions:
            try:
                new_contradictions = self.contradictions.scan()
                results["new_contradictions"] = len(new_contradictions)
                results["total_unresolved"] = self.contradictions.stats().get("unresolved", 0)
            except Exception as e:
                results["errors"].append(f"contradictions: {e}")

        # 3. Checkpoint
        if self.checkpoint:
            try:
                cp = self.checkpoint.capture(reason=reason)
                results["checkpoint_id"] = cp.checkpoint_id
            except Exception as e:
                results["errors"].append(f"checkpoint: {e}")

        results["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)

        # Emit event
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(EventType.GHOST_PULSE_COMPLETED, EventSource.GHOST, payload={
                "cycle_reason": reason,
                "errors": len(results["errors"]),
                "duration_ms": results["duration_ms"],
            })
        except Exception:
            pass

        logger.info(
            "ghost_layer cycle complete: %s (%.0fms, %d errors)",
            reason, results["duration_ms"], len(results["errors"]),
        )
        return results

    def session_end(self) -> dict:
        """Called at session end — carry forward objections + final checkpoint."""
        results = {"action": "session_end", "errors": []}

        # Carry forward objections
        if self.objections:
            try:
                carried = self.objections.carry_forward()
                results["objections_carried"] = len(carried)
            except Exception as e:
                results["errors"].append(f"carry_forward: {e}")

        # Final checkpoint
        if self.checkpoint:
            try:
                cp = self.checkpoint.capture(reason="session_end")
                results["checkpoint_id"] = cp.checkpoint_id
            except Exception as e:
                results["errors"].append(f"checkpoint: {e}")

        return results

    def session_start(self) -> dict:
        """Called at session start — restore last checkpoint + report state."""
        results = {"action": "session_start", "errors": []}

        # Restore last checkpoint
        if self.checkpoint:
            try:
                cp = self.checkpoint.restore("latest")
                if cp:
                    results["restored_checkpoint"] = cp.checkpoint_id
                    results["last_branch"] = cp.branch
                    results["last_reason"] = cp.reason
                    results["unresolved_objections"] = cp.unresolved_objections
                    results["active_warnings"] = cp.active_warnings
                else:
                    results["restored_checkpoint"] = None
            except Exception as e:
                results["errors"].append(f"restore: {e}")

        # Report current objections
        if self.objections:
            try:
                open_objs = self.objections.list_open()
                results["open_objections"] = [o.summary() for o in open_objs[:5]]
            except Exception as e:
                results["errors"].append(f"objections: {e}")

        # Report contradictions
        if self.contradictions:
            try:
                unresolved = self.contradictions.list_unresolved()
                results["unresolved_contradictions"] = len(unresolved)
            except Exception as e:
                results["errors"].append(f"contradictions: {e}")

        return results


def _detect_node_id() -> str:
    """Detect current node ID from environment or hostname."""
    import os
    node_id = os.environ.get("MULTIFLEET_NODE_ID", "")
    if not node_id:
        import socket
        try:
            node_id = socket.gethostname().split(".")[0]
        except Exception:
            node_id = "unknown"
    return node_id


def ghost_status(repo_root: str = ".") -> dict:
    """Convenience: get ghost layer status as dict."""
    return GhostLayer(repo_root=repo_root).status().to_dict()


def ghost_cycle(repo_root: str = ".", reason: str = "manual") -> dict:
    """Convenience: run a ghost cycle and return results."""
    return GhostLayer(repo_root=repo_root).cycle(reason=reason)
