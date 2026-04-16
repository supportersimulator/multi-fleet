"""Superset Overlay Stream Adapter — publish state to Superset overlays.

Aggregates state from all subsystems (surgeon, probe, supervisor,
evidence, fleet transport) into a single NodeOverlay update that
the Superset dashboard or VS Code webview can consume.

"Superset lets you watch the whole organism think."
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.adapter.overlay-stream")


class OverlayStreamAdapter:
    """Collects state from all subsystems and pushes to NodeOverlay.

    Usage:
        adapter = OverlayStreamAdapter()
        overlay = adapter.collect_and_push()  # Full refresh
        adapter.push_surgeon_update(phase="review", disagreement=0.2)
        adapter.push_probe_update(findings=3, blocking=1)
    """

    def __init__(self, node_id: str = "", supervisor=None):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._supervisor = supervisor  # Use injected singleton; avoids fresh Supervisor per call
        self._last_push: float = 0.0
        self._push_interval_s: float = 5.0
        self._stats = {"pushes": 0, "errors": 0}

    def collect_and_push(self, force: bool = False) -> Optional[dict]:
        """Collect state from all subsystems and push to overlay manager.

        Args:
            force: Push even if within rate limit window.

        Returns:
            The overlay dict that was pushed, or None if rate-limited.
        """
        now = time.monotonic()
        if not force and (now - self._last_push) < self._push_interval_s:
            return None
        self._last_push = now

        state = {}

        # 1. Surgeon state
        try:
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            if adapter.is_available():
                state["surgeon"] = adapter.get_state()
        except Exception as e:
            logger.debug("Surgeon state collection failed: %s", e)

        # 2. Supervisor state (use injected singleton to preserve task state)
        try:
            sup = self._supervisor
            if sup is None:
                # Fallback: try to get daemon's singleton via module-level ref
                from multifleet.supervisor import Supervisor
                sup = Supervisor(node_id=self._node_id)
            active = sup.list_active()
            state["supervisor"] = {
                "active_tasks": len(active),
                "tasks": [
                    {"task_id": s.task_id, "phase": s.phase.value, "progress": s.elapsed_s}
                    for s in active[:5]
                ],
            }
        except Exception:
            pass

        # 3. Evidence stats
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            recent = ledger.query(limit=5)
            state["evidence"] = {
                "recent_count": len(recent),
                "latest_type": recent[0].get("event_type", "") if recent else "",
            }
        except Exception:
            pass

        # 4. Probe summary
        try:
            from multifleet.probe_engine import ProbeEngine
            engine = ProbeEngine()
            summary = engine.findings_summary()
            state["probes"] = summary
        except Exception:
            pass

        # 5. Superpowers workflow phase
        try:
            from multifleet.superpowers_adapter import get_adapter
            sp = get_adapter()
            state["superpowers"] = sp.get_state()
        except Exception:
            pass

        # Push to overlay manager
        try:
            from multifleet.node_overlay import NodeOverlayManager
            mgr = NodeOverlayManager()
            overlay = mgr.update(self._node_id, **state)
            self._stats["pushes"] += 1
            logger.debug("overlay_stream pushed %d subsystem(s)", len(state))
            return overlay.to_dict() if hasattr(overlay, "to_dict") else state
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Overlay push failed: %s", e)
            return state

    def push_surgeon_update(
        self, phase: str, disagreement: float = 0.0, intensity: str = "light",
    ) -> None:
        """Push a surgeon-specific overlay update."""
        try:
            from multifleet.node_overlay import NodeOverlayManager
            mgr = NodeOverlayManager()
            mgr.update(self._node_id, surgeon={
                "phase": phase,
                "disagreement_score": disagreement,
                "intensity": intensity,
            })
            self._stats["pushes"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Surgeon overlay update failed: %s", e)

    def push_probe_update(
        self, findings: int = 0, blocking: int = 0, top_finding: str = "",
    ) -> None:
        """Push a probe-specific overlay update."""
        try:
            from multifleet.node_overlay import NodeOverlayManager
            mgr = NodeOverlayManager()
            mgr.update(self._node_id, probes={
                "total_findings": findings,
                "blocking": blocking,
                "top_finding": top_finding[:100],
            })
            self._stats["pushes"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Probe overlay update failed: %s", e)

    def push_supervisor_update(
        self, task_id: str, phase: str, progress: float = 0.0,
    ) -> None:
        """Push a supervisor-specific overlay update."""
        try:
            from multifleet.node_overlay import NodeOverlayManager
            mgr = NodeOverlayManager()
            mgr.update(self._node_id, supervisor={
                "active_task": task_id,
                "phase": phase,
                "progress": round(progress, 2),
            })
            self._stats["pushes"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Supervisor overlay update failed: %s", e)

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_adapter: Optional[OverlayStreamAdapter] = None


def get_overlay_stream() -> OverlayStreamAdapter:
    global _adapter
    if _adapter is None:
        _adapter = OverlayStreamAdapter()
    return _adapter
