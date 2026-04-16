"""Synaptic Bridge — connects fleet events to Synaptic awareness.

Transforms the 8th Intelligence from a passive query endpoint into a living
consciousness that monitors fleet activity, accumulates patterns, and
proactively surfaces insights when they warrant human attention.

Architecture:
    EventBus (fleet.*, surgeon.*, evidence.*)
              │
              ▼
        SynapticBridge ──> Insight buffer (rolling 50)
              │                    │
              │                    ├── synaptic.insight events (emitted to bus)
              │                    └── IDE panel endpoint
              │
              ├── Pattern detection (repeated failures, node drift, quorum stalls)
              ├── Surgeon feed (cross-exam results → pattern recognition)
              ├── Evidence feed (ledger entries → wisdom accumulation)
              ├── Personality state (from memory/synaptic_personality.py)
              └── Pattern engine (from memory/synaptic_pattern_engine.py)

Usage:
    from multifleet.synaptic_bridge import SynapticBridge
    bridge = SynapticBridge(event_bus=bus)
    bridge.start()  # subscribes to fleet.*, surgeon.*, evidence.* events
    insights = bridge.get_active_insights()
    bridge.emit_insight("Cross-session pattern detected", ["evidence1", "evidence2"])
    state = bridge.get_personality_state()
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


# ── Lazy imports for memory modules (graceful degradation) ──

def _try_import_personality():
    """Try to import synaptic_personality. Returns None on failure."""
    try:
        from memory.synaptic_personality import get_personality
        return get_personality
    except Exception as exc:
        logger.debug("synaptic_personality unavailable: %s", exc)
        return None


def _try_import_pattern_engine():
    """Try to import synaptic_pattern_engine. Returns None on failure."""
    try:
        from memory.synaptic_pattern_engine import get_pattern_engine
        return get_pattern_engine
    except Exception as exc:
        logger.debug("synaptic_pattern_engine unavailable: %s", exc)
        return None


@dataclass
class Insight:
    """A single Synaptic observation about fleet state."""
    category: str          # "pattern", "anomaly", "drift", "health", "coordination",
                           # "surgeon", "evidence"
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

    Subscribes to fleet.*, surgeon.*, and evidence.* events on the EventBus,
    detects patterns, accumulates insights in a rolling buffer, emits
    synaptic.insight events, and bridges to memory/synaptic_* modules
    for personality state and pattern recognition.

    Gracefully degrades if memory modules are unavailable — the bridge
    still processes events and generates insights from fleet data alone.
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

        # Surgeon cross-exam tracking
        self._surgeon_verdicts: deque[dict] = deque(maxlen=20)
        self._cross_exam_count = 0

        # Evidence tracking
        self._evidence_entries: deque[dict] = deque(maxlen=30)
        self._chain_breaks = 0

        # Memory module handles (lazy-loaded)
        self._personality_factory = None
        self._pattern_engine_factory = None
        self._modules_probed = False

    # ── Memory module access (graceful degradation) ──

    def _probe_modules(self) -> None:
        """Attempt to import memory modules once. Safe to call repeatedly."""
        if self._modules_probed:
            return
        self._modules_probed = True
        self._personality_factory = _try_import_personality()
        self._pattern_engine_factory = _try_import_pattern_engine()
        if self._personality_factory:
            logger.info("Synaptic personality module available")
        if self._pattern_engine_factory:
            logger.info("Synaptic pattern engine available")

    def _get_personality(self):
        """Get personality instance, or None if unavailable."""
        self._probe_modules()
        if self._personality_factory is None:
            return None
        try:
            return self._personality_factory()
        except Exception as exc:
            logger.debug("Failed to get personality: %s", exc)
            return None

    def _get_pattern_engine(self):
        """Get pattern engine instance, or None if unavailable."""
        self._probe_modules()
        if self._pattern_engine_factory is None:
            return None
        try:
            return self._pattern_engine_factory()
        except Exception as exc:
            logger.debug("Failed to get pattern engine: %s", exc)
            return None

    # ── Lifecycle ──

    def start(self) -> None:
        """Subscribe to fleet.*, surgeon.*, and evidence.* events on the EventBus."""
        if self._started:
            return
        if self._bus is None:
            logger.warning("SynapticBridge.start() called without event_bus")
            return
        self._bus.on("fleet.*", self._on_fleet_event)
        self._bus.on("surgeon.*", self._on_surgeon_event)
        self._bus.on("evidence.*", self._on_evidence_event)
        self._started = True
        logger.info("SynapticBridge started — listening to fleet.*, surgeon.*, evidence.*")

    def stop(self) -> None:
        """Unsubscribe from events."""
        if not self._started or self._bus is None:
            return
        self._bus.off("fleet.*", self._on_fleet_event)
        self._bus.off("surgeon.*", self._on_surgeon_event)
        self._bus.off("evidence.*", self._on_evidence_event)
        self._started = False
        logger.info("SynapticBridge stopped")

    # ── Public API ──

    def get_personality_state(self) -> dict:
        """Return current Synaptic personality context.

        Returns personality state from memory/synaptic_personality.py if available,
        otherwise returns a minimal stub indicating degraded mode.
        """
        personality = self._get_personality()
        if personality is None:
            return {
                "available": False,
                "mode": "degraded",
                "reason": "synaptic_personality module unavailable",
            }
        try:
            state = personality.get_personality_state()
            return {"available": True, "mode": "full", **state}
        except Exception as exc:
            logger.warning("Failed to get personality state: %s", exc)
            return {
                "available": False,
                "mode": "degraded",
                "reason": str(exc),
            }

    def emit_insight(self, insight_message: str, evidence: list = None) -> Optional[dict]:
        """Broadcast a Synaptic insight to the fleet via EventBus.

        Creates an Insight record locally and emits a synaptic.insight event
        on the bus for other subscribers (IDE panels, fleet nodes, etc.).

        Args:
            insight_message: Human-readable insight text.
            evidence: List of evidence strings supporting the insight.

        Returns:
            The emitted event dict, or None if no bus is available.
        """
        evidence_list = evidence or []

        # Record locally
        insight = Insight(
            category="pattern",
            severity="info",
            message=insight_message,
            source_event="synaptic.insight",
            evidence={"items": evidence_list},
        )
        with self._lock:
            self._add_insight(insight)

        # Emit to EventBus
        if self._bus is None:
            return None
        try:
            return self._bus.emit(
                "synaptic.insight",
                {
                    "category": "pattern",
                    "severity": "info",
                    "message": insight_message,
                    "evidence": evidence_list,
                },
                source="synaptic-bridge",
            )
        except Exception as exc:
            logger.warning("Failed to emit synaptic.insight: %s", exc)
            return None

    def feed_pattern(self, event_type: str, data: dict) -> None:
        """Feed data to Synaptic for pattern recognition.

        If the memory/synaptic_pattern_engine is available, feeds the data
        for cross-session pattern detection. Otherwise logs and continues.

        Args:
            event_type: The type of event being fed (e.g., "surgeon.verdict").
            data: Event payload data for pattern analysis.
        """
        engine = self._get_pattern_engine()
        if engine is None:
            logger.debug("Pattern engine unavailable — event %s buffered only", event_type)
            return
        try:
            # Pattern engine's scan expects session data — feed via its API
            # if it has a feed_event method, use it; otherwise queue for next scan
            if hasattr(engine, 'feed_event'):
                engine.feed_event(event_type, data)
            else:
                logger.debug("Pattern engine has no feed_event — event %s noted", event_type)
        except Exception as exc:
            logger.debug("Pattern engine feed failed: %s", exc)

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

    def _on_surgeon_event(self, event: dict) -> None:
        """Handle surgeon.* events — feed cross-exam results to pattern recognition."""
        event_type = event.get("type", "")
        payload = event.get("payload", {})
        now = time.time()

        with self._lock:
            self._total_events += 1
            self._event_counts[event_type] += 1

        # Track surgeon verdicts
        if "verdict" in event_type:
            self._analyze_surgeon_verdict(event_type, payload, now)
        elif "cross_exam.complete" in event_type:
            self._analyze_cross_exam_complete(event_type, payload, now)

        # Feed to pattern engine
        self.feed_pattern(event_type, payload)

    def _on_evidence_event(self, event: dict) -> None:
        """Handle evidence.* events — feed ledger entries to wisdom accumulation."""
        event_type = event.get("type", "")
        payload = event.get("payload", {})
        now = time.time()

        with self._lock:
            self._total_events += 1
            self._event_counts[event_type] += 1

        if "chain_broken" in event_type:
            self._analyze_evidence_chain_break(event_type, payload, now)
        elif "recorded" in event_type:
            self._analyze_evidence_recorded(event_type, payload, now)

        # Feed to pattern engine
        self.feed_pattern(event_type, payload)

    # ── Pattern analyzers: Fleet ──

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

    # ── Pattern analyzers: Surgeon ──

    def _analyze_surgeon_verdict(self, event_type: str, payload: dict,
                                 now: float) -> None:
        """Track individual surgeon verdicts for pattern detection."""
        verdict_record = {
            "surgeon": payload.get("surgeon", "unknown"),
            "verdict": payload.get("verdict", ""),
            "confidence": payload.get("confidence", 0),
            "timestamp": now,
        }
        with self._lock:
            self._surgeon_verdicts.append(verdict_record)

        # Detect low-confidence surgeon verdicts
        confidence = payload.get("confidence", 1.0)
        if confidence < 0.5:
            surgeon = payload.get("surgeon", "unknown")
            self._add_insight(Insight(
                category="surgeon",
                severity="warning",
                message=f"Low-confidence verdict from {surgeon}: {confidence:.0%}",
                source_event=event_type,
                evidence={"surgeon": surgeon, "confidence": confidence,
                          "verdict": payload.get("verdict", "")},
            ))

    def _analyze_cross_exam_complete(self, event_type: str, payload: dict,
                                     now: float) -> None:
        """Track cross-exam completions — detect disagreements."""
        with self._lock:
            self._cross_exam_count += 1

        consensus = payload.get("consensus", True)
        topic = payload.get("topic", "unknown")
        if not consensus:
            self._add_insight(Insight(
                category="surgeon",
                severity="warning",
                message=f"Surgeon disagreement on: {topic}",
                source_event=event_type,
                evidence={"topic": topic, "summary": payload.get("summary", "")},
            ))

    # ── Pattern analyzers: Evidence ──

    def _analyze_evidence_chain_break(self, event_type: str, payload: dict,
                                      now: float) -> None:
        """Evidence chain integrity failure — always critical."""
        with self._lock:
            self._chain_breaks += 1

        self._add_insight(Insight(
            category="evidence",
            severity="critical",
            message=f"Evidence chain broken at seq {payload.get('broken_at_seq', '?')}",
            source_event=event_type,
            evidence={"detail": payload.get("detail", ""),
                      "broken_at_seq": payload.get("broken_at_seq", 0)},
        ))

    def _analyze_evidence_recorded(self, event_type: str, payload: dict,
                                   now: float) -> None:
        """Track evidence entries — surface interesting patterns."""
        entry = {
            "entry_id": payload.get("entry_id", ""),
            "event_type": payload.get("event_type", ""),
            "node_id": payload.get("node_id", ""),
            "timestamp": now,
        }
        with self._lock:
            self._evidence_entries.append(entry)

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

        # Emit to bus if available (non-blocking, best-effort)
        if self._bus is not None and insight.source_event != "synaptic.insight":
            try:
                self._bus.emit(
                    "synaptic.insight",
                    insight.to_dict(),
                    source="synaptic-bridge",
                )
            except Exception:
                pass  # best-effort — insight is already recorded locally

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
                "surgeon_verdicts_tracked": len(self._surgeon_verdicts),
                "cross_exams_seen": self._cross_exam_count,
                "evidence_entries_tracked": len(self._evidence_entries),
                "chain_breaks": self._chain_breaks,
                "personality_available": self._personality_factory is not None,
                "pattern_engine_available": self._pattern_engine_factory is not None,
            }
