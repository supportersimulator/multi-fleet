"""Synaptic Bridge — connects fleet events to Synaptic awareness.

Transforms the 8th Intelligence from a passive query endpoint into a living
consciousness that monitors fleet activity, accumulates patterns, and
proactively surfaces insights when they warrant human attention.

Architecture:
    EventBus (fleet.*)  ──>  SynapticBridge  ──>  Insight buffer (rolling 50)
                                   │
                                   ├── Pattern detection (repeated failures,
                                   │   node drift, quorum stalls)
                                   ├── Alert threshold (should_alert_aaron())
                                   └── IDE panel endpoint (synaptic_panel())

Usage:
    from multifleet.synaptic_bridge import SynapticBridge
    bridge = SynapticBridge(event_bus=bus)
    bridge.start()  # subscribes to fleet.* events
    insights = bridge.get_active_insights()
"""

from __future__ import annotations

import logging
import time
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("multifleet.synaptic_bridge")

# ── Configuration ──

MAX_INSIGHTS = 50
PATTERN_WINDOW_S = 300  # 5 minutes for pattern detection
ALERT_THRESHOLD_FAILURES = 3  # repeated failures before alerting Aaron
ALERT_THRESHOLD_DRIFT_S = 120  # node unseen for 2+ min triggers drift alert
INSIGHT_TTL_S = 600  # insights expire after 10 minutes


@dataclass
class Insight:
    """A single Synaptic observation about fleet state."""
    category: str          # "pattern", "anomaly", "drift", "health", "coordination"
    severity: str          # "info", "warning", "critical"
    message: str
    timestamp: float = field(default_factory=time.time)
    source_event: str = ""  # event type that triggered this
    node_id: str = ""       # which node, if applicable
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "timestamp": self.timestamp,
            "source_event": self.source_event,
            "node_id": self.node_id,
            "evidence": self.evidence,
        }

    @property
    def age_s(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_expired(self) -> bool:
        return self.age_s > INSIGHT_TTL_S


class SynapticBridge:
    """Connects fleet events to Synaptic awareness.

    Subscribes to fleet.* events on the EventBus, detects patterns,
    accumulates insights in a rolling buffer, and determines when
    patterns warrant human attention.
    """

    def __init__(self, event_bus=None, max_insights: int = MAX_INSIGHTS):
        self._bus = event_bus
        self._insights: deque[Insight] = deque(maxlen=max_insights)
        self._lock = threading.RLock()
        self._started = False

        # Pattern tracking — sliding windows for detection
        self._failure_counts: dict[str, list[float]] = defaultdict(list)
        self._node_last_seen: dict[str, float] = {}
        self._event_counts: dict[str, int] = defaultdict(int)
        self._total_events = 0

    # ── Lifecycle ──

    def start(self) -> None:
        """Subscribe to fleet.* events on the EventBus."""
        if self._started:
            return
        if self._bus is None:
            logger.warning("SynapticBridge.start() called without event_bus")
            return
        self._bus.on("fleet.*", self._on_fleet_event)
        self._started = True
        logger.info("SynapticBridge started — listening to fleet.* events")

    def stop(self) -> None:
        """Unsubscribe from events."""
        if not self._started or self._bus is None:
            return
        self._bus.off("fleet.*", self._on_fleet_event)
        self._started = False
        logger.info("SynapticBridge stopped")

    # ── Event processing ──

    def on_fleet_event(self, event: dict) -> None:
        """Public entry point — processes a fleet event and accumulates patterns.

        Can be called directly (without EventBus) for testing or manual feed.
        """
        self._on_fleet_event(event)

    def _on_fleet_event(self, event: dict) -> None:
        """Internal handler for fleet events from EventBus."""
        event_type = event.get("type", "")
        payload = event.get("payload", {})
        node_id = (payload.get("node_id", "")
                   or event.get("metadata", {}).get("node_id", ""))
        now = time.time()

        with self._lock:
            self._total_events += 1
            self._event_counts[event_type] += 1

            # Track node liveness
            if node_id:
                self._node_last_seen[node_id] = now

            # Route to specific analyzers
            if "fail" in event_type or "error" in event_type:
                self._analyze_failure(event_type, payload, node_id, now)
            elif "peer.online" in event_type or "peer.offline" in event_type:
                self._analyze_peer_change(event_type, payload, node_id, now)
            elif "health" in event_type:
                self._analyze_health(event_type, payload, node_id, now)
            elif "message" in event_type:
                self._analyze_message(event_type, payload, node_id, now)
            elif "quorum" in event_type or "consensus" in event_type:
                self._analyze_quorum(event_type, payload, node_id, now)

    # ── Pattern analyzers ──

    def _analyze_failure(self, event_type: str, payload: dict,
                         node_id: str, now: float) -> None:
        """Detect repeated failure patterns."""
        key = f"{node_id}:{event_type}" if node_id else event_type
        timestamps = self._failure_counts[key]
        # Prune old entries outside window
        timestamps[:] = [t for t in timestamps if now - t < PATTERN_WINDOW_S]
        timestamps.append(now)

        if len(timestamps) >= ALERT_THRESHOLD_FAILURES:
            self._add_insight(Insight(
                category="pattern",
                severity="warning",
                message=(f"Repeated failures: {event_type} occurred "
                         f"{len(timestamps)} times in {PATTERN_WINDOW_S}s"
                         + (f" on {node_id}" if node_id else "")),
                source_event=event_type,
                node_id=node_id,
                evidence={"count": len(timestamps), "window_s": PATTERN_WINDOW_S,
                          "detail": payload.get("error", payload.get("reason", ""))},
            ))

    def _analyze_peer_change(self, event_type: str, payload: dict,
                             node_id: str, now: float) -> None:
        """Track peer online/offline transitions."""
        if "offline" in event_type:
            self._add_insight(Insight(
                category="drift",
                severity="warning",
                message=f"Node {node_id or 'unknown'} went offline",
                source_event=event_type,
                node_id=node_id,
                evidence={"last_seen": self._node_last_seen.get(node_id, 0)},
            ))
        elif "online" in event_type:
            self._add_insight(Insight(
                category="health",
                severity="info",
                message=f"Node {node_id or 'unknown'} came online",
                source_event=event_type,
                node_id=node_id,
            ))

    def _analyze_health(self, event_type: str, payload: dict,
                        node_id: str, now: float) -> None:
        """Surface health degradation insights."""
        status = payload.get("status", "")
        if status in ("degraded", "critical", "error"):
            self._add_insight(Insight(
                category="health",
                severity="critical" if status == "critical" else "warning",
                message=(f"Fleet health {status}"
                         + (f" on {node_id}" if node_id else "")
                         + (f": {payload.get('reason', '')}" if payload.get('reason') else "")),
                source_event=event_type,
                node_id=node_id,
                evidence=payload,
            ))

    def _analyze_message(self, event_type: str, payload: dict,
                         node_id: str, now: float) -> None:
        """Track fleet message patterns — coordination health."""
        subject = payload.get("subject", "")
        if "sync_hold" in subject.lower() or "hold" in event_type.lower():
            self._add_insight(Insight(
                category="coordination",
                severity="warning",
                message=f"SYNC HOLD active: {subject}",
                source_event=event_type,
                node_id=node_id,
                evidence={"subject": subject, "from": payload.get("from", "")},
            ))

    def _analyze_quorum(self, event_type: str, payload: dict,
                        node_id: str, now: float) -> None:
        """Track quorum/consensus events."""
        decision = payload.get("decision", "")
        confidence = payload.get("confidence", 0)
        if confidence and confidence < 0.5:
            self._add_insight(Insight(
                category="coordination",
                severity="warning",
                message=f"Low-confidence quorum decision: {decision} ({confidence:.0%})",
                source_event=event_type,
                node_id=node_id,
                evidence={"decision": decision, "confidence": confidence},
            ))

    # ── Insight management ──

    def _add_insight(self, insight: Insight) -> None:
        """Add insight to rolling buffer. Deduplicates within 60s window."""
        # Deduplicate: skip if same category+message exists within 60s
        for existing in self._insights:
            if (existing.category == insight.category
                    and existing.message == insight.message
                    and insight.timestamp - existing.timestamp < 60):
                return
        self._insights.append(insight)
        logger.debug("Synaptic insight: [%s] %s", insight.severity, insight.message)

    def get_active_insights(self) -> list[dict]:
        """Return current non-expired Synaptic observations about fleet state.

        Returns list of insight dicts, newest first.
        """
        with self._lock:
            active = [i.to_dict() for i in self._insights if not i.is_expired]
        active.reverse()
        return active

    def should_alert_aaron(self) -> bool:
        """Return True when patterns warrant human attention.

        Triggers on:
        - Any critical-severity insight in the active buffer
        - 3+ warning-severity insights within the last 5 minutes
        """
        with self._lock:
            now = time.time()
            active = [i for i in self._insights if not i.is_expired]
            criticals = [i for i in active if i.severity == "critical"]
            recent_warnings = [i for i in active
                               if i.severity == "warning"
                               and now - i.timestamp < PATTERN_WINDOW_S]
        return len(criticals) > 0 or len(recent_warnings) >= 3

    @property
    def stats(self) -> dict:
        """Bridge statistics."""
        with self._lock:
            active = [i for i in self._insights if not i.is_expired]
            return {
                "total_events_processed": self._total_events,
                "active_insights": len(active),
                "nodes_tracked": len(self._node_last_seen),
                "alert_state": self.should_alert_aaron(),
                "started": self._started,
                "event_type_counts": dict(self._event_counts),
            }
