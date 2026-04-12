"""Event Schema — the shared bloodstream of the 5-plane architecture.

Standardized EventEnvelope that every component uses to communicate:
- Multi-Fleet (transport)
- ContextDNA Ghost (ambient cognition)
- 3-Surgeons (corrigibility protocol)
- Probe Engine (detection)
- Superset/IDE (theater)

"Multi-Fleet carries them. ContextDNA Ghost interprets them. Superset displays them."
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.event-schema")


class EventSource(Enum):
    """Which plane emitted the event."""
    MULTI_FLEET = "multi-fleet"
    CONTEXTDNA = "contextdna"
    GHOST = "ghost"
    THREE_SURGEONS = "3-surgeons"
    SUPERPOWERS = "superpowers"
    SUPERSET = "superset"
    PROBE_ENGINE = "probe-engine"


class EventType(Enum):
    """All standardized event types across the 5-plane architecture."""

    # Fleet transport events
    FLEET_NODE_HEARTBEAT = "fleet.node.heartbeat"
    FLEET_NODE_DEGRADED = "fleet.node.degraded"
    FLEET_TASK_CREATED = "fleet.task.created"
    FLEET_TASK_DISPATCHED = "fleet.task.dispatched"
    FLEET_TASK_DELIVERED = "fleet.task.delivered"
    FLEET_REPAIR_REQUESTED = "fleet.repair.requested"
    FLEET_REPAIR_SUCCEEDED = "fleet.repair.succeeded"
    FLEET_TRANSPORT_DEGRADED = "fleet.transport.degraded"

    # Context/evidence events
    CONTEXT_PACKET_BUILT = "context.packet.built"
    CONTEXT_PACKET_DELIVERED = "context.packet.delivered"

    # Ghost layer events
    GHOST_SESSION_DIGESTED = "ghost.session.digested"
    GHOST_MEMORY_CANDIDATE = "ghost.memory.candidate"
    GHOST_MEMORY_PROMOTED = "ghost.memory.promoted"
    GHOST_OBJECTION_RECORDED = "ghost.objection.recorded"
    GHOST_PULSE_STARTED = "ghost.pulse.started"
    GHOST_PULSE_COMPLETED = "ghost.pulse.completed"

    # Surgeon events
    SURGEON_PHASE_CHANGED = "surgeon.phase.changed"
    SURGEON_CONSENSUS_BLOCKED = "surgeon.consensus.blocked"
    SURGEON_CONSENSUS_REACHED = "surgeon.consensus.reached"

    # Probe events
    PROBE_FINDING_RAISED = "probe.finding.raised"
    PROBE_RUN_STARTED = "probe.run.started"
    PROBE_RUN_COMPLETED = "probe.run.completed"

    # Invariance events
    INVARIANT_ACTIVATED = "invariant.activated"
    INVARIANT_VIOLATED = "invariant.violated"

    # Acceptance events
    TASK_ACCEPTED = "task.accepted"
    TASK_BLOCKED = "task.blocked"

    # Race events
    RACE_STARTED = "race.started"
    RACE_COMPLETED = "race.completed"
    RACE_NODE_FINISHED = "race.node.finished"
    RACE_ARBITER_PENDING = "race.arbiter.pending"

    # Health monitor events
    HEALTH_TRANSITION = "health.transition"
    HEALTH_ANOMALY = "health.anomaly"
    HEALTH_REPAIR = "health.repair"
    HEALTH_CHANNEL_BROKEN = "health.channel.broken"
    HEALTH_CHANNEL_HEALING = "health.channel.healing"
    HEALTH_CHANNEL_RESTORED = "health.channel.restored"

    # Supervisor events
    SUPERVISOR_TRANSITION = "supervisor.transition"

    # Ghost extended events
    GHOST_CORROBORATE_COMPLETED = "ghost.corroborate.completed"
    GHOST_MIRROR_SCANNED = "ghost.mirror.scanned"
    GHOST_MIRROR_DRIFT = "ghost.mirror.drift_detected"
    GHOST_ANTICIPATE_COMPLETED = "ghost.anticipate.completed"

    # Synthesis/merge events
    SYNTHESIS_MERGE_DECISION = "synthesis.merge_decision"
    SYNTHESIS_DECISION = "synthesis.decision"

    # Review events
    REVIEW_STARTED = "review.started"
    REVIEW_VERDICT_RECEIVED = "review.verdict_received"
    REVIEW_COMPLETED = "review.completed"


@dataclass
class EventEnvelope:
    """The universal event container. Every event in the system uses this shape.

    This is the "shared bloodstream" — the one schema all 5 planes agree on.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    type: str = ""  # EventType value
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    fleet_id: str = "default"
    node_id: str = ""
    source: str = ""  # EventSource value

    # Optional context
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    worktree_id: Optional[str] = None
    repo_id: Optional[str] = None

    # Quorum sensing: semantic signal classification (Las/Rhl/PQS/IQS-inspired)
    # See multifleet.quorum.signal_class for the four classes.
    signal_class: Optional[str] = None  # "strategic", "tactical", "state", "stress"

    # The actual data
    payload: Any = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "ts": self.ts,
            "fleet_id": self.fleet_id,
            "node_id": self.node_id,
            "source": self.source,
            "payload": self.payload if isinstance(self.payload, dict) else {"data": self.payload},
        }
        if self.session_id:
            d["session_id"] = self.session_id
        if self.task_id:
            d["task_id"] = self.task_id
        if self.worktree_id:
            d["worktree_id"] = self.worktree_id
        if self.repo_id:
            d["repo_id"] = self.repo_id
        if self.signal_class:
            d["signal_class"] = self.signal_class
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "EventEnvelope":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_json(cls, raw: str) -> "EventEnvelope":
        return cls.from_dict(json.loads(raw))


def create_event(
    event_type: EventType,
    source: EventSource,
    node_id: str,
    payload: dict = None,
    task_id: str = None,
    session_id: str = None,
    signal_class: str = None,
) -> EventEnvelope:
    """Canonical factory for creating typed EventEnvelope events.

    Returns an EventEnvelope (dataclass). For plain dict events used by
    the in-process EventBus, see event_types.create_event().

    If signal_class is not provided, it is auto-classified using
    the quorum sensing signal classifier (Las/Rhl/PQS/IQS mapping).
    """
    evt_payload = payload or {}
    # Auto-classify signal class if not explicitly provided
    if signal_class is None:
        try:
            from .quorum.signal_class import classify_message
            signal_class = classify_message(event_type.value, evt_payload).value
        except Exception:
            signal_class = "tactical"  # Safe default: Rhl-like execution flow
    return EventEnvelope(
        type=event_type.value,
        source=source.value,
        node_id=node_id,
        payload=evt_payload,
        task_id=task_id,
        session_id=session_id,
        signal_class=signal_class,
    )


# ── Convenience builders for common events ──

def probe_finding_event(node_id: str, probe_id: str, severity: str, title: str, **kwargs) -> EventEnvelope:
    """Create a probe.finding.raised event."""
    return create_event(
        EventType.PROBE_FINDING_RAISED,
        EventSource.PROBE_ENGINE,
        node_id,
        payload={"probe_id": probe_id, "severity": severity, "title": title, **kwargs},
    )


def surgeon_phase_event(node_id: str, phase: str, intensity: str, disagreement: float = 0.0) -> EventEnvelope:
    """Create a surgeon.phase.changed event."""
    return create_event(
        EventType.SURGEON_PHASE_CHANGED,
        EventSource.THREE_SURGEONS,
        node_id,
        payload={"phase": phase, "intensity": intensity, "disagreement_score": disagreement},
    )


def ghost_digest_event(node_id: str, task_id: str, candidates: int, objections: int) -> EventEnvelope:
    """Create a ghost.session.digested event."""
    return create_event(
        EventType.GHOST_SESSION_DIGESTED,
        EventSource.GHOST,
        node_id,
        payload={"candidates": candidates, "objections": objections},
        task_id=task_id,
    )


def task_accepted_event(node_id: str, task_id: str, reason: str) -> EventEnvelope:
    """Create a task.accepted event."""
    return create_event(
        EventType.TASK_ACCEPTED,
        EventSource.CONTEXTDNA,
        node_id,
        payload={"reason": reason},
        task_id=task_id,
    )


def task_blocked_event(node_id: str, task_id: str, blocking_issues: list[str]) -> EventEnvelope:
    """Create a task.blocked event."""
    return create_event(
        EventType.TASK_BLOCKED,
        EventSource.CONTEXTDNA,
        node_id,
        payload={"blocking_issues": blocking_issues},
        task_id=task_id,
    )


def race_event(event_type: EventType, node_id: str, race_id: str, **payload) -> EventEnvelope:
    """Create a race.* event."""
    return create_event(
        event_type,
        EventSource.MULTI_FLEET,
        node_id,
        payload={"race_id": race_id, **payload},
        task_id=race_id,
    )
