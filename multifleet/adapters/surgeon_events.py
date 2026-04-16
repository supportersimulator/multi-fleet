"""3-Surgeons Event Adapter — convert surgeon phases to fleet events.

Bridges the 3-surgeon protocol into the fleet EventEnvelope system so
that surgeon phase changes, consensus results, and cross-exam outcomes
are visible to all fleet components (dashboard, overlay, Superset).

Also broadcasts surgeon verdicts to the fleet via P7 git messages and
the product bridge, so other nodes learn about cross-exam outcomes.

"Live surgeon visibility: 3-Surgeons phase change -> event bus -> Superset overlay update"
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Rate limiting for P7 verdict broadcasts
_last_p7_broadcast_ts: float = 0.0
_P7_RATE_LIMIT_SECONDS: float = 3600.0  # max 1 P7 broadcast per hour

from multifleet.event_schema import (
    EventEnvelope, EventType, EventSource, create_event,
    surgeon_phase_event,
)
from multifleet.event_emitter import emit

logger = logging.getLogger("fleet-nerve.adapter.surgeon-events")

# Superrepo root for fleet messages (P7 channel)
_SUPERREPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def broadcast_surgeon_verdict(verdict: Dict[str, Any], superrepo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Broadcast a surgeon verdict to the fleet.

    Three delivery paths (best-effort, first success wins):
    1. Product bridge route (surgeons -> fleet)
    2. P7 git channel (.fleet-messages/all/)
    3. EventBus emission (surgeon.verdict.broadcast)

    Args:
        verdict: Dict with task_id, agreed, confidence, dissent_count,
                 surgeons_consulted, node_id.
        superrepo_root: Override path for testing (default: auto-detect).

    Returns:
        {bridge: bool, p7: bool, eventbus: bool} — which channels succeeded.
    """
    results: Dict[str, bool] = {"bridge": False, "p7": False, "eventbus": False}
    node_id = verdict.get("node_id", os.environ.get("MULTIFLEET_NODE_ID", "unknown"))

    # Channel 1: Product bridge
    try:
        from multifleet.product_bridge import get_bridge
        bridge = get_bridge()
        bridge.surgeons_to_fleet({
            "task_id": verdict.get("task_id", "unknown"),
            "verdict": "consensus" if verdict.get("agreed") else "dissent",
            "confidence": verdict.get("confidence", 0.0),
            "target_nodes": "all",
            **verdict,
        })
        results["bridge"] = True
    except Exception as e:
        logger.debug("Bridge broadcast failed (non-fatal): %s", e)

    # Channel 2: P7 git message
    # Only write to fleet-messages for SIGNIFICANT verdicts:
    # - Dissent (disagreement between surgeons)
    # - Low confidence (< 70%)
    # - Rate limited: max 1 routine verdict per hour
    try:
        global _last_p7_broadcast_ts
        now_mono = time.monotonic()
        is_significant = (
            not verdict.get("agreed", True)
            or verdict.get("dissent_count", 0) > 0
            or verdict.get("confidence", 1.0) < 0.7
        )
        is_rate_ok = (now_mono - _last_p7_broadcast_ts) >= _P7_RATE_LIMIT_SECONDS

        if is_significant or is_rate_ok:
            root = superrepo_root or _SUPERREPO_ROOT
            fleet_dir = root / ".fleet-messages" / "all"
            fleet_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
            task_slug = (verdict.get("task_id", "unknown") or "unknown")[:30]
            # Sanitize for filename
            task_slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in task_slug)
            filename = f"{ts}-surgeon-verdict-{task_slug}.md"

            status = "CONSENSUS" if verdict.get("agreed") else "DISSENT"
            confidence = verdict.get("confidence", 0.0)
            content = (
                f"---\n"
                f"from: {node_id}\n"
                f"to: all\n"
                f"subject: surgeon-verdict {status} ({confidence:.0%})\n"
                f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
                f"type: surgeon_verdict\n"
                f"---\n\n"
                f"# Surgeon Verdict: {status}\n\n"
                f"- **Task**: {verdict.get('task_id', 'unknown')}\n"
                f"- **Agreed**: {verdict.get('agreed', False)}\n"
                f"- **Confidence**: {confidence:.1%}\n"
                f"- **Dissent count**: {verdict.get('dissent_count', 0)}\n"
                f"- **Surgeons consulted**: {', '.join(verdict.get('surgeons_consulted', []))}\n"
                f"- **Source node**: {node_id}\n"
            )
            (fleet_dir / filename).write_text(content)
            _last_p7_broadcast_ts = now_mono
            results["p7"] = True
        else:
            logger.debug("P7 broadcast skipped: routine consensus, rate limited (1/hr)")
            results["p7_skipped"] = True
    except Exception as e:
        logger.debug("P7 broadcast failed (non-fatal): %s", e)

    # Channel 3: EventBus
    try:
        from multifleet.event_bus import IDEEventBus
        bus = IDEEventBus.get()
        bus.emit(
            "surgeon.verdict.broadcast",
            payload=verdict,
            source="3-surgeons",
        )
        results["eventbus"] = True
    except Exception as e:
        logger.debug("EventBus broadcast failed (non-fatal): %s", e)

    delivered = [ch for ch, ok in results.items() if ok]
    if delivered:
        logger.info("Surgeon verdict broadcast via: %s", ", ".join(delivered))
    else:
        logger.warning("Surgeon verdict broadcast: ALL channels failed")

    return results


class SurgeonEventsAdapter:
    """Converts 3-surgeon protocol state changes into fleet events.

    Usage:
        adapter = SurgeonEventsAdapter()
        # When surgeon phase changes:
        adapter.on_phase_change("triage", intensity="standard", disagreement=0.3)
        # When consensus reached:
        adapter.on_consensus(task_id="fix-auth", agreed=True, confidence=0.85)
        # Periodic state poll:
        adapter.poll_and_emit()
    """

    def __init__(self, node_id: str = ""):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._last_phase: str = ""
        self._last_poll: float = 0.0
        self._poll_interval_s: float = 10.0
        self._stats = {
            "phase_events": 0,
            "consensus_events": 0,
            "poll_events": 0,
            "errors": 0,
        }

    def on_phase_change(
        self,
        phase: str,
        intensity: str = "light",
        disagreement: float = 0.0,
        task_id: str = "",
    ) -> Optional[EventEnvelope]:
        """Emit an event when surgeon phase changes.

        Args:
            phase: New surgeon phase (triage/operate/review/close).
            intensity: Review intensity (light/standard/full).
            disagreement: Disagreement score between surgeons.
            task_id: Optional task ID context.
        """
        if phase == self._last_phase:
            return None  # Deduplicate

        self._last_phase = phase
        event = surgeon_phase_event(
            node_id=self._node_id,
            phase=phase,
            intensity=intensity,
            disagreement=disagreement,
        )
        if task_id:
            event.task_id = task_id

        try:
            emit(event)
            self._stats["phase_events"] += 1
            logger.info(
                "surgeon_event phase=%s intensity=%s disagreement=%.2f",
                phase, intensity, disagreement,
            )
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Surgeon event emit failed: %s", e)

        return event

    def on_consensus(
        self,
        task_id: str,
        agreed: bool,
        confidence: float = 0.0,
        dissent_count: int = 0,
        surgeons_consulted: list[str] = None,
    ) -> EventEnvelope:
        """Emit consensus reached/blocked event and broadcast verdict to fleet."""
        et = EventType.SURGEON_CONSENSUS_REACHED if agreed else EventType.SURGEON_CONSENSUS_BLOCKED
        payload = {
            "agreed": agreed,
            "confidence": round(confidence, 3),
            "dissent_count": dissent_count,
            "surgeons_consulted": surgeons_consulted or [],
        }
        event = create_event(
            event_type=et,
            source=EventSource.THREE_SURGEONS,
            node_id=self._node_id,
            payload=payload,
            task_id=task_id,
        )
        try:
            emit(event)
            self._stats["consensus_events"] += 1
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Consensus event emit failed: %s", e)

        # Broadcast verdict to fleet (best-effort, never blocks consensus)
        verdict_data = {
            "task_id": task_id,
            "agreed": agreed,
            "confidence": round(confidence, 3),
            "dissent_count": dissent_count,
            "surgeons_consulted": surgeons_consulted or [],
            "node_id": self._node_id,
        }
        try:
            broadcast_surgeon_verdict(verdict_data)
            self._stats["broadcast_events"] = self._stats.get("broadcast_events", 0) + 1
        except Exception as e:
            self._stats["broadcast_errors"] = self._stats.get("broadcast_errors", 0) + 1
            logger.warning("Verdict broadcast failed (non-fatal): %s", e)

        # Route verdict to superpowers for workflow phase changes (3S→SP)
        try:
            from multifleet.product_bridge import get_bridge
            bridge = get_bridge()
            bridge.route("3-surgeons", "superpowers", verdict_data)
        except Exception as e:
            logger.debug("surgeon->superpowers route (non-fatal): %s", e)

        return event

    def poll_and_emit(self) -> Optional[EventEnvelope]:
        """Poll surgeon adapter for state and emit if changed.

        Respects poll_interval_s to avoid spamming.
        """
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval_s:
            return None
        self._last_poll = now

        try:
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            if not adapter.is_available():
                return None

            state = adapter.get_state()
            phase = state.get("phase", "unknown")
            disagreement = state.get("disagreement_score", 0.0)
            intensity = state.get("intensity", "light")

            if phase != self._last_phase:
                self._stats["poll_events"] += 1
                return self.on_phase_change(phase, intensity, disagreement)
        except Exception as e:
            self._stats["errors"] += 1
            logger.debug("Surgeon poll failed: %s", e)

        return None

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_adapter: Optional[SurgeonEventsAdapter] = None


def get_surgeon_events() -> SurgeonEventsAdapter:
    global _adapter
    if _adapter is None:
        _adapter = SurgeonEventsAdapter()
    return _adapter
