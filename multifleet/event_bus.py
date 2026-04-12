"""Phase 10 M10.2 Event Bus — the nervous system of the IDE panel lifecycle.

Unified facade over the existing EventBus core (event_bus_server.py).
Every significant event in the system flows through here. Panels subscribe,
engines publish. Provides:

- Namespace-typed publish helpers (fleet.*, probe.*, surgeon.*, panel.*,
  evidence.*, quorum.*)
- Per-namespace history rings for late subscribers (replay on connect)
- Thread-safe singleton for system-wide access
- Integration hooks for fleet daemon, probe engine, surgeon protocol,
  and evidence ledger
- Middleware pipeline (pre-emit transforms/filters)

All events conform to ContextDNA Event v1 schema (event_types.py).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from .event_bus_server import EventBus
from .event_middleware import register_default_middleware
from .event_types import validate_namespace

logger = logging.getLogger("multifleet.event_bus")

# ── Namespace constants ──

NS_FLEET = "fleet"
NS_PROBE = "probe"
NS_SURGEON = "surgeon"
NS_PANEL = "panel"
NS_EVIDENCE = "evidence"
NS_QUORUM = "quorum"
NS_SUPERPOWERS = "superpowers"

ALL_NAMESPACES = frozenset({
    NS_FLEET, NS_PROBE, NS_SURGEON, NS_PANEL, NS_EVIDENCE, NS_QUORUM,
    NS_SUPERPOWERS,
})

# Default ring sizes
DEFAULT_GLOBAL_RING = 1000
DEFAULT_NAMESPACE_RING = 200

# ── Middleware type ──
# Middleware receives (event_type, payload, metadata) and returns
# (event_type, payload, metadata) or None to suppress the event.
Middleware = Callable[[str, dict, dict], Optional[tuple[str, dict, dict]]]


class IDEEventBus:
    """System-wide event bus for the IDE panel lifecycle.

    Wraps the core EventBus with:
    - Per-namespace history rings (late subscribers can replay)
    - Typed publish helpers for each namespace
    - Middleware pipeline for pre-emit transforms
    - Integration hooks (attach fleet daemon, probe engine, etc.)
    - Thread-safe singleton access via IDEEventBus.get()
    """

    _instance: Optional[IDEEventBus] = None
    _lock = threading.Lock()

    def __init__(
        self,
        ring_size: int = DEFAULT_GLOBAL_RING,
        namespace_ring_size: int = DEFAULT_NAMESPACE_RING,
    ):
        self._bus = EventBus(ring_size=ring_size)
        self._ns_ring_size = namespace_ring_size
        self._ns_rings: dict[str, deque] = {
            ns: deque(maxlen=namespace_ring_size) for ns in ALL_NAMESPACES
        }
        self._middleware: list[Middleware] = []
        self._integrations: dict[str, Any] = {}
        self._ns_stats: dict[str, int] = defaultdict(int)

        # Auto-capture events into namespace rings
        self._bus.on("*", self._capture_to_ns_ring)

        # Register default advisory middleware (schema validation + rate limiting)
        register_default_middleware(self)

    # ── Singleton ──

    @classmethod
    def get(cls, **kwargs) -> IDEEventBus:
        """Get or create the singleton IDEEventBus.

        Thread-safe. Extra kwargs forwarded to __init__ on first call only.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(**kwargs)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing). Not thread-safe with concurrent use."""
        cls._instance = None

    # ── Core pub/sub (delegates to EventBus) ──

    def on(self, pattern: str, handler: Callable) -> None:
        """Subscribe to events matching pattern (supports * and ? wildcards)."""
        self._bus.on(pattern, handler)

    def once(self, pattern: str, handler: Callable) -> None:
        """Subscribe for a single matching event, then auto-unsubscribe."""
        self._bus.once(pattern, handler)

    def off(self, pattern: str, handler: Callable) -> None:
        """Unsubscribe a handler from a pattern."""
        self._bus.off(pattern, handler)

    def emit(
        self,
        event_type: str,
        payload: dict,
        source: str = "python",
        correlation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Emit an event through the middleware pipeline and core bus.

        Returns the created event dict, or None if middleware suppressed it.
        """
        metadata = {
            "correlation_id": correlation_id,
            "session_id": session_id,
        }

        # Run middleware pipeline
        for mw in self._middleware:
            try:
                result = mw(event_type, payload, metadata)
                if result is None:
                    logger.debug("Event %s suppressed by middleware %s",
                                 event_type, getattr(mw, "__name__", "?"))
                    return None
                event_type, payload, metadata = result
            except Exception as exc:
                logger.warning("Middleware %s error: %s",
                               getattr(mw, "__name__", "?"), exc)

        return self._bus.emit(
            event_type=event_type,
            payload=payload,
            source=source,
            correlation_id=metadata.get("correlation_id"),
            session_id=metadata.get("session_id"),
        )

    # ── Namespace history ──

    def _capture_to_ns_ring(self, event: dict) -> None:
        """Capture every event into its namespace ring buffer."""
        event_type = event.get("type", "")
        ns = event_type.split(".")[0] if "." in event_type else ""
        if ns in self._ns_rings:
            self._ns_rings[ns].append(event)
            self._ns_stats[ns] += 1

    def history(
        self,
        namespace: str,
        limit: int = 50,
        pattern: Optional[str] = None,
    ) -> list[dict]:
        """Get recent events for a namespace (newest first).

        Panels call this on connect to catch up on missed events.
        Optional pattern further filters within the namespace.
        """
        ring = self._ns_rings.get(namespace)
        if ring is None:
            return []

        events = list(ring)
        events.reverse()

        if pattern:
            events = [e for e in events if EventBus._match(pattern, e.get("type", ""))]

        return events[:limit]

    def recent(self, limit: int = 50, pattern: Optional[str] = None) -> list[dict]:
        """Get recent events from the global ring buffer."""
        return self._bus.recent(limit=limit, pattern=pattern)

    # ── Middleware ──

    def use(self, middleware: Middleware) -> None:
        """Add a middleware function to the pre-emit pipeline.

        Middleware signature: (event_type, payload, metadata) -> (event_type, payload, metadata) | None
        Return None to suppress the event.
        """
        self._middleware.append(middleware)

    def remove_middleware(self, middleware: Middleware) -> None:
        """Remove a middleware from the pipeline."""
        try:
            self._middleware.remove(middleware)
        except ValueError:
            pass

    # ══════════════════════════════════════════════════════════════
    # Typed publish helpers — one per namespace
    # ══════════════════════════════════════════════════════════════

    # ── fleet.* ──

    def fleet_peer_online(self, peer_id: str, sessions: int = 0, **extra) -> Optional[dict]:
        """Publish fleet.peer.online."""
        return self.emit("fleet.peer.online", {"peer_id": peer_id, "sessions": sessions, **extra},
                         source="fleet-daemon")

    def fleet_peer_offline(self, peer_id: str, last_seen_s: float = 0, **extra) -> Optional[dict]:
        """Publish fleet.peer.offline."""
        return self.emit("fleet.peer.offline", {"peer_id": peer_id, "last_seen_s": last_seen_s, **extra},
                         source="fleet-daemon")

    def fleet_health(self, peer_id: str, score: str, broken: list = None, **extra) -> Optional[dict]:
        """Publish fleet.health.degraded."""
        return self.emit("fleet.health.degraded",
                         {"peer_id": peer_id, "score": score, "broken": broken or [], **extra},
                         source="fleet-daemon")

    def fleet_channel(self, action: str, peer_id: str, score: str, **extra) -> Optional[dict]:
        """Publish fleet.channel.{action} (broken/healing/restored)."""
        return self.emit(f"fleet.channel.{action}",
                         {"peer_id": peer_id, "score": score, **extra},
                         source="fleet-daemon")

    # ── probe.* ──

    def probe_finding(
        self,
        probe_id: str,
        title: str,
        severity: str,
        confidence: float,
        **extra,
    ) -> Optional[dict]:
        """Publish probe.finding — a probe detected something."""
        return self.emit("probe.finding", {
            "probe_id": probe_id, "title": title,
            "severity": severity, "confidence": confidence, **extra,
        }, source="probe-engine")

    def probe_complete(
        self,
        probes_run: int,
        findings_count: int,
        duration_ms: float = 0,
        **extra,
    ) -> Optional[dict]:
        """Publish probe.complete — a scan run finished."""
        return self.emit("probe.complete", {
            "probes_run": probes_run, "findings_count": findings_count,
            "duration_ms": duration_ms, **extra,
        }, source="probe-engine")

    def probe_error(self, probe_id: str, error: str, **extra) -> Optional[dict]:
        """Publish probe.error — a probe execution failed."""
        return self.emit("probe.error", {
            "probe_id": probe_id, "error": error, **extra,
        }, source="probe-engine")

    # ── surgeon.* ──

    def surgeon_cross_exam_started(
        self,
        topic: str,
        surgeons: list[str],
        correlation_id: Optional[str] = None,
        **extra,
    ) -> Optional[dict]:
        """Publish surgeon.cross_exam.started."""
        return self.emit("surgeon.cross_exam.started", {
            "topic": topic, "surgeons": surgeons, **extra,
        }, source="3-surgeons", correlation_id=correlation_id)

    def surgeon_cross_exam_complete(
        self,
        topic: str,
        consensus: bool,
        summary: str = "",
        correlation_id: Optional[str] = None,
        **extra,
    ) -> Optional[dict]:
        """Publish surgeon.cross_exam.complete."""
        return self.emit("surgeon.cross_exam.complete", {
            "topic": topic, "consensus": consensus, "summary": summary, **extra,
        }, source="3-surgeons", correlation_id=correlation_id)

    def surgeon_verdict(
        self,
        surgeon: str,
        verdict: str,
        confidence: float,
        correlation_id: Optional[str] = None,
        **extra,
    ) -> Optional[dict]:
        """Publish surgeon.verdict — individual surgeon result."""
        return self.emit("surgeon.verdict", {
            "surgeon": surgeon, "verdict": verdict, "confidence": confidence, **extra,
        }, source="3-surgeons", correlation_id=correlation_id)

    # ── panel.* ──

    def panel_registered(self, panel_id: str, source: str = "builtin", **extra) -> Optional[dict]:
        """Publish panel.registered."""
        return self.emit("panel.registered", {"panel_id": panel_id, "source": source, **extra},
                         source="panel-registry")

    def panel_activated(self, panel_id: str, **extra) -> Optional[dict]:
        """Publish panel.activated."""
        return self.emit("panel.activated", {"panel_id": panel_id, **extra},
                         source="panel-registry")

    def panel_suspended(self, panel_id: str, reason: str = "", **extra) -> Optional[dict]:
        """Publish panel.suspended."""
        return self.emit("panel.suspended", {"panel_id": panel_id, "reason": reason, **extra},
                         source="panel-registry")

    def panel_closed(self, panel_id: str, **extra) -> Optional[dict]:
        """Publish panel.closed."""
        return self.emit("panel.closed", {"panel_id": panel_id, **extra},
                         source="panel-registry")

    def panel_error(self, panel_id: str, error: str, **extra) -> Optional[dict]:
        """Publish panel.error."""
        return self.emit("panel.error", {"panel_id": panel_id, "error": error, **extra},
                         source="panel-registry")

    # ── evidence.* ──

    def evidence_recorded(
        self,
        entry_id: str,
        event_type: str,
        node_id: str,
        subject: str = "",
        **extra,
    ) -> Optional[dict]:
        """Publish evidence.recorded — new entry in the evidence ledger."""
        return self.emit("evidence.recorded", {
            "entry_id": entry_id, "event_type": event_type,
            "node_id": node_id, "subject": subject, **extra,
        }, source="evidence-ledger")

    def evidence_chain_broken(self, broken_at_seq: int, detail: str, **extra) -> Optional[dict]:
        """Publish evidence.chain_broken — hash chain integrity failure."""
        return self.emit("evidence.chain_broken", {
            "broken_at_seq": broken_at_seq, "detail": detail, **extra,
        }, source="evidence-ledger")

    def evidence_synced(self, from_node: str, entries_imported: int, **extra) -> Optional[dict]:
        """Publish evidence.synced — evidence synced from remote node."""
        return self.emit("evidence.synced", {
            "from_node": from_node, "entries_imported": entries_imported, **extra,
        }, source="evidence-ledger")

    # ── quorum.* ──

    def quorum_proposed(
        self,
        proposal_id: str,
        topic: str,
        proposer: str,
        required_votes: int,
        **extra,
    ) -> Optional[dict]:
        """Publish quorum.proposed — a quorum decision was proposed."""
        return self.emit("quorum.proposed", {
            "proposal_id": proposal_id, "topic": topic,
            "proposer": proposer, "required_votes": required_votes, **extra,
        }, source="quorum")

    def quorum_voted(
        self,
        proposal_id: str,
        voter: str,
        vote: str,
        **extra,
    ) -> Optional[dict]:
        """Publish quorum.voted — a node cast its vote."""
        return self.emit("quorum.voted", {
            "proposal_id": proposal_id, "voter": voter, "vote": vote, **extra,
        }, source="quorum")

    def quorum_decided(
        self,
        proposal_id: str,
        decision: str,
        votes_for: int,
        votes_against: int,
        **extra,
    ) -> Optional[dict]:
        """Publish quorum.decided — quorum reached a decision."""
        return self.emit("quorum.decided", {
            "proposal_id": proposal_id, "decision": decision,
            "votes_for": votes_for, "votes_against": votes_against, **extra,
        }, source="quorum")

    # ── superpowers.* ──

    def superpowers_phase_changed(
        self,
        old_phase: str,
        new_phase: str,
        node_id: str,
        timestamp: Optional[float] = None,
        **extra,
    ) -> Optional[dict]:
        """Publish superpowers.phase.changed — workflow phase transition."""
        return self.emit("superpowers.phase.changed", {
            "old_phase": old_phase,
            "new_phase": new_phase,
            "node_id": node_id,
            "timestamp": timestamp or time.time(),
            **extra,
        }, source="superpowers-adapter")

    # ══════════════════════════════════════════════════════════════
    # Integration hooks — attach external systems
    # ══════════════════════════════════════════════════════════════

    def attach_fleet_daemon(self, fleet_eventbus) -> None:
        """Wire a FleetEventBus (eventbus.py) to publish through this bus.

        Sets the in-process core bus on the fleet adapter so fleet events
        flow through the unified IDEEventBus.
        """
        fleet_eventbus.set_core_bus(self._bus)
        self._integrations["fleet_daemon"] = fleet_eventbus
        logger.info("Fleet daemon attached to IDEEventBus")

    def attach_probe_engine(self, probe_engine) -> None:
        """Register probe engine for event forwarding.

        The probe engine should call probe_finding/probe_complete/probe_error
        on this bus. This hook stores a reference for health checks.
        """
        self._integrations["probe_engine"] = probe_engine
        logger.info("Probe engine attached to IDEEventBus")

    def attach_surgeon_adapter(self, surgeon_adapter) -> None:
        """Register 3-surgeon adapter for cross-exam event forwarding.

        Stores reference for health checks and enables surgeon.* events.
        """
        self._integrations["surgeon_adapter"] = surgeon_adapter
        logger.info("Surgeon adapter attached to IDEEventBus")

    def attach_evidence_ledger(self, evidence_ledger) -> None:
        """Register evidence ledger for chain event forwarding.

        Stores reference for health checks and enables evidence.* events.
        """
        self._integrations["evidence_ledger"] = evidence_ledger
        logger.info("Evidence ledger attached to IDEEventBus")

    # ── Health / stats ──

    @property
    def core_bus(self) -> EventBus:
        """Access the underlying EventBus (for EventBusServer wiring)."""
        return self._bus

    @property
    def stats(self) -> dict:
        """Combined stats: core bus + namespace counters + integrations."""
        return {
            "core": self._bus.stats,
            "namespace_events": dict(self._ns_stats),
            "integrations": list(self._integrations.keys()),
            "middleware_count": len(self._middleware),
        }

    @property
    def integrations(self) -> dict[str, Any]:
        """Return attached integrations (for health checks)."""
        return dict(self._integrations)

    def health(self) -> dict:
        """Health check for the event bus subsystem."""
        core_stats = self._bus.stats
        return {
            "status": "ok",
            "events_emitted": core_stats["emitted"],
            "events_delivered": core_stats["delivered"],
            "events_dropped": core_stats["dropped"],
            "errors": core_stats["errors"],
            "namespaces_active": [
                ns for ns, count in self._ns_stats.items() if count > 0
            ],
            "integrations": list(self._integrations.keys()),
            "middleware_count": len(self._middleware),
        }
