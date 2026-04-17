"""Event Catalog — complete reference of ALL events in the 5-plane architecture.

Unifies three event sources:
1. event_schema.py EventType enum (typed EventEnvelope events)
2. event_registry.py EVENTS dict (payload-validated registry events)
3. Ad-hoc string events emitted throughout the codebase

Provides:
- get_event_catalog() — full list of all event definitions
- validate_event() — runtime payload validation against catalog
- get_event() — lookup a single event definition
- list_events() — filter by namespace, source, etc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("multifleet.event_catalog")


@dataclass(frozen=True)
class CatalogEntry:
    """One event in the catalog."""

    name: str                        # e.g. "fleet.node.heartbeat"
    namespace: str                   # e.g. "fleet"
    description: str
    required_fields: tuple[str, ...]  # payload keys that MUST be present
    optional_fields: tuple[str, ...] = ()
    emitters: tuple[str, ...] = ()   # which modules emit this event
    subscribers: tuple[str, ...] = ()  # which modules listen for this event
    broadcast: bool = False          # if True, sent to all fleet nodes
    source: str = ""                 # canonical EventSource value

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "description": self.description,
            "required_fields": list(self.required_fields),
            "optional_fields": list(self.optional_fields),
            "emitters": list(self.emitters),
            "subscribers": list(self.subscribers),
            "broadcast": self.broadcast,
            "source": self.source,
        }


def _build_catalog() -> dict[str, CatalogEntry]:
    """Build the complete event catalog from all sources."""
    catalog: dict[str, CatalogEntry] = {}

    def _add(entry: CatalogEntry) -> None:
        catalog[entry.name] = entry

    # ══════════════════════════════════════════════════════════════
    # Fleet transport events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "fleet.node.heartbeat", "fleet",
        "Periodic heartbeat from a fleet node",
        ("peer_id", "uptime_s"),
        ("active_tasks",),
        emitters=("nats_client", "eventbus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "fleet.node.degraded", "fleet",
        "Fleet node health score dropped below threshold",
        ("peer_id", "score", "broken"),
        (),
        emitters=("health_monitor", "eventbus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "fleet.task.created", "fleet",
        "New fleet task created for dispatch",
        ("task_id",),
        ("description", "priority"),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.task.dispatched", "fleet",
        "Task dispatched to a fleet node",
        ("task_id", "target_node"),
        (),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.task.delivered", "fleet",
        "Task delivery confirmed by target node",
        ("task_id", "node_id"),
        (),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.repair.requested", "fleet",
        "Self-heal repair requested for a broken channel",
        ("target", "channel"),
        ("severity",),
        emitters=("self_heal",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.repair.succeeded", "fleet",
        "Self-heal repair completed successfully",
        ("target", "channel"),
        ("duration_s",),
        emitters=("self_heal",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.transport.degraded", "fleet",
        "Transport layer degraded (NATS/HTTP issues)",
        ("channel", "error"),
        (),
        emitters=("nats_client", "channels"),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "fleet.branch.convergence_scanned", "fleet",
        "Branch convergence scan completed",
        ("branches_scanned", "conflicts_found"),
        (),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))

    # Fleet peer events (from eventbus.py convenience methods)
    _add(CatalogEntry(
        "peer.online", "peer",
        "Fleet node came online",
        ("peer_id",),
        ("sessions",),
        emitters=("eventbus", "event_bus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "peer.offline", "peer",
        "Fleet node went offline",
        ("peer_id",),
        ("last_seen_s",),
        emitters=("eventbus", "event_bus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "peer.heartbeat", "peer",
        "Periodic heartbeat from node (registry alias)",
        ("peer_id", "uptime_s"),
        ("active_tasks",),
        emitters=("eventbus",),
        source="multi-fleet",
    ))

    # Fleet message events
    _add(CatalogEntry(
        "message.sent", "message",
        "Fleet message sent to target node",
        ("target", "channel", "type"),
        ("elapsed_s",),
        emitters=("eventbus",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "message.received", "message",
        "Fleet message received from remote node",
        ("from",),
        ("type",),
        emitters=("eventbus",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "message.failed", "message",
        "Fleet message delivery failed",
        ("target", "channel", "error"),
        (),
        emitters=("eventbus",),
        source="multi-fleet",
    ))

    # ══════════════════════════════════════════════════════════════
    # Health events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "health.transition", "health",
        "Node health state transition",
        ("peer_id", "from_state", "to_state"),
        (),
        emitters=("health_monitor",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "health.anomaly", "health",
        "Health anomaly detected",
        ("peer_id", "anomaly_type"),
        ("detail",),
        emitters=("health_monitor",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "health.repair", "health",
        "Health repair action taken",
        ("peer_id", "repair_type"),
        ("result",),
        emitters=("health_monitor",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "health.degraded", "health",
        "Node health score dropped (broadcast)",
        ("peer_id", "score", "broken"),
        (),
        emitters=("eventbus", "event_bus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "health.channel.broken", "health",
        "Communication channel to peer is broken",
        ("peer_id", "score"),
        ("broken", "severity"),
        emitters=("health_monitor", "eventbus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "health.channel.healing", "health",
        "Broken channel repair in progress",
        ("peer_id", "score"),
        ("escalation_level",),
        emitters=("health_monitor", "eventbus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "health.channel.restored", "health",
        "Communication channel restored to healthy state",
        ("peer_id", "score"),
        (),
        emitters=("health_monitor", "eventbus"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "health.partition.healed", "health",
        "Network partition fully healed",
        ("peer_id",),
        ("duration_s",),
        emitters=("health_monitor",),
        source="multi-fleet",
    ))
    # channel.* aliases (emitted by eventbus.py without health. prefix)
    _add(CatalogEntry(
        "channel.broken", "channel",
        "Channel broken (eventbus alias for health.channel.broken)",
        ("peer_id", "score"),
        ("broken", "severity"),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "channel.healing", "channel",
        "Channel healing (eventbus alias for health.channel.healing)",
        ("peer_id", "score"),
        ("escalation_level",),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "channel.restored", "channel",
        "Channel restored (eventbus alias for health.channel.restored)",
        ("peer_id", "score"),
        (),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))

    # ══════════════════════════════════════════════════════════════
    # Lease events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "lease.acquired", "lease",
        "Resource lease acquired",
        ("resource_id", "holder"),
        (),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "lease.released", "lease",
        "Resource lease released",
        ("resource_id", "holder"),
        (),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "lease.expired", "lease",
        "Resource lease expired (timeout)",
        ("resource_id", "holder"),
        ("held_for_s",),
        emitters=("eventbus",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "lease.contention", "lease",
        "Multiple nodes competing for lease",
        ("resource_id", "contenders"),
        (),
        emitters=("eventbus",),
        source="multi-fleet",
    ))

    # ══════════════════════════════════════════════════════════════
    # Probe events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "probe.finding", "probe",
        "Probe finding detected",
        ("probe_id", "title", "severity", "confidence"),
        ("blast_radius", "file_path", "summary"),
        emitters=("probe_engine", "event_bus"),
        subscribers=("evidence_ledger", "admin_feed"),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "probe.finding.raised", "probe",
        "Probe finding raised (EventEnvelope variant)",
        ("probe_id", "severity", "title"),
        ("blast_radius", "file_path", "summary", "confidence"),
        emitters=("event_schema",),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "probe.complete", "probe",
        "Probe scan run completed",
        ("probes_run", "findings_count"),
        ("duration_ms", "cost_limit"),
        emitters=("probe_engine", "event_bus"),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "probe.error", "probe",
        "Probe execution error",
        ("probe_id", "error"),
        ("duration_ms",),
        emitters=("probe_engine", "event_bus"),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "probe.run.started", "probe",
        "Probe run started (EventEnvelope variant)",
        ("probe_id",),
        ("parameters",),
        emitters=("event_schema",),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "probe.run.completed", "probe",
        "Probe run completed (EventEnvelope variant)",
        ("probe_id", "result"),
        ("duration_ms",),
        emitters=("event_schema",),
        source="probe-engine",
    ))

    # Scan events (registry aliases)
    _add(CatalogEntry(
        "scan.finding", "scan",
        "Probe finding detected (scan registry alias)",
        ("probe_id", "title", "severity", "confidence"),
        ("blast_radius", "file_path", "summary"),
        emitters=("probe_engine",),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "scan.complete", "scan",
        "Probe scan run completed (scan registry alias)",
        ("cost_limit", "probes_run", "findings_count"),
        ("duration_ms",),
        emitters=("probe_engine",),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "scan.error", "scan",
        "Probe execution error (scan registry alias)",
        ("probe_id", "error"),
        ("duration_ms",),
        emitters=("probe_engine",),
        source="probe-engine",
    ))
    _add(CatalogEntry(
        "scan.registered", "scan",
        "New probe registered in engine",
        ("probe_id", "category", "cost"),
        (),
        emitters=("probe_engine",),
        source="probe-engine",
    ))

    # ══════════════════════════════════════════════════════════════
    # Surgeon events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "surgeon.phase.changed", "surgeon",
        "Surgeon protocol phase changed",
        ("phase", "intensity"),
        ("disagreement_score",),
        emitters=("event_schema",),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.consensus.blocked", "surgeon",
        "Surgeon consensus blocked",
        ("reason",),
        ("phase",),
        emitters=("event_schema",),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.consensus.reached", "surgeon",
        "Surgeon consensus reached",
        ("decision",),
        ("confidence",),
        emitters=("event_schema",),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.cross_exam.started", "surgeon",
        "Cross-examination started",
        ("topic", "surgeons"),
        (),
        emitters=("event_bus",),
        subscribers=("synaptic_bridge",),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.cross_exam.complete", "surgeon",
        "Cross-examination completed",
        ("topic", "consensus"),
        ("summary",),
        emitters=("event_bus",),
        subscribers=("synaptic_bridge",),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.verdict", "surgeon",
        "Individual surgeon verdict",
        ("surgeon", "verdict", "confidence"),
        (),
        emitters=("event_bus",),
        subscribers=("synaptic_bridge", "admin_feed"),
        source="3-surgeons",
    ))
    _add(CatalogEntry(
        "surgeon.verdict.broadcast", "surgeon",
        "Surgeon verdict broadcast to fleet",
        ("surgeon", "verdict"),
        ("confidence", "topic"),
        emitters=("adapters.surgeon_events",),
        source="3-surgeons",
        broadcast=True,
    ))

    # ══════════════════════════════════════════════════════════════
    # Supervisor events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "supervisor.phase_transition", "supervisor",
        "Task phase changed in supervisor",
        ("task_id", "from", "to"),
        ("iteration",),
        emitters=("supervisor",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "supervisor.blocked", "supervisor",
        "Task blocked in supervisor",
        ("task_id", "reason"),
        ("phase",),
        emitters=("supervisor",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "supervisor.stall_detected", "supervisor",
        "Watchdog detected stall in task",
        ("task_id", "phase", "elapsed_s"),
        ("action",),
        emitters=("supervisor",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "supervisor.completed", "supervisor",
        "Task completed in supervisor",
        ("task_id", "criteria_met"),
        ("elapsed_s",),
        emitters=("supervisor",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "supervisor.transition", "supervisor",
        "Supervisor state transition (EventEnvelope variant)",
        ("from_state", "to_state"),
        (),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Context / Evidence events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "context.packet.built", "context",
        "Context packet assembled for injection",
        ("packet_id", "sections"),
        ("token_count",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "context.packet.delivered", "context",
        "Context packet delivered to consumer",
        ("packet_id",),
        ("delivery_ms",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "evidence.recorded", "evidence",
        "New entry in evidence ledger",
        ("entry_id", "event_type", "node_id"),
        ("subject",),
        emitters=("event_bus", "eventbus"),
        source="evidence-ledger",
    ))
    _add(CatalogEntry(
        "evidence.chain.verified", "evidence",
        "Evidence hash chain verified (EventEnvelope variant)",
        ("chain_length", "valid"),
        (),
        emitters=("event_schema",),
        source="evidence-ledger",
    ))
    _add(CatalogEntry(
        "evidence.chain_broken", "evidence",
        "Hash chain integrity failure",
        ("broken_at_seq", "detail"),
        (),
        emitters=("event_bus",),
        source="evidence-ledger",
    ))
    _add(CatalogEntry(
        "evidence.synced", "evidence",
        "Evidence synced from remote node",
        ("from_node", "entries_imported"),
        ("entries_skipped",),
        emitters=("event_bus",),
        source="evidence-ledger",
    ))
    _add(CatalogEntry(
        "evidence.promoted", "evidence",
        "Evidence entry promoted up truth ladder",
        ("entry_id", "from_layer", "to_layer"),
        ("promoted_by",),
        emitters=("memory_bridge",),
        source="evidence-ledger",
    ))

    # ══════════════════════════════════════════════════════════════
    # Ghost layer events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "ghost.session.digested", "ghost",
        "Ghost digested a session for memory candidates",
        ("candidates", "objections"),
        ("task_id",),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.memory.candidate", "ghost",
        "Ghost identified a memory candidate",
        ("candidate_id", "content"),
        ("confidence",),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.memory.promoted", "ghost",
        "Ghost memory promoted to long-term",
        ("candidate_id",),
        ("reason",),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.objection.recorded", "ghost",
        "Ghost recorded an objection to a candidate",
        ("candidate_id", "objection"),
        (),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.pulse.started", "ghost",
        "Ghost pulse cycle started",
        (),
        ("cycle_id",),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.pulse.completed", "ghost",
        "Ghost pulse cycle completed",
        ("candidates_found", "objections_raised"),
        ("cycle_id",),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.corroborate.completed", "ghost",
        "Ghost corroboration completed",
        ("entries_corroborated",),
        (),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.mirror.scanned", "ghost",
        "Ghost mirror scan completed",
        ("entries_scanned",),
        (),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.mirror.drift_detected", "ghost",
        "Ghost mirror drift detected between nodes",
        ("from_node", "to_node", "drift_score"),
        (),
        emitters=("event_schema",),
        source="ghost",
    ))
    _add(CatalogEntry(
        "ghost.anticipate.completed", "ghost",
        "Ghost anticipation engine completed",
        ("predictions",),
        (),
        emitters=("event_schema",),
        source="ghost",
    ))

    # ══════════════════════════════════════════════════════════════
    # Panel events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "panel.registered", "panel",
        "IDE panel registered",
        ("panel_id",),
        ("source",),
        emitters=("event_bus",),
        source="panel-registry",
    ))
    _add(CatalogEntry(
        "panel.activated", "panel",
        "IDE panel activated",
        ("panel_id",),
        (),
        emitters=("event_bus",),
        source="panel-registry",
    ))
    _add(CatalogEntry(
        "panel.suspended", "panel",
        "IDE panel suspended",
        ("panel_id",),
        ("reason",),
        emitters=("event_bus",),
        source="panel-registry",
    ))
    _add(CatalogEntry(
        "panel.closed", "panel",
        "IDE panel closed",
        ("panel_id",),
        (),
        emitters=("event_bus",),
        source="panel-registry",
    ))
    _add(CatalogEntry(
        "panel.error", "panel",
        "IDE panel error",
        ("panel_id", "error"),
        (),
        emitters=("event_bus",),
        source="panel-registry",
    ))
    _add(CatalogEntry(
        "panel.updated", "panel",
        "IDE panel state updated",
        ("panel_id",),
        (),
        emitters=("panel_bus_bridge",),
        source="panel-bus-bridge",
    ))

    # ══════════════════════════════════════════════════════════════
    # Quorum events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "quorum.proposed", "quorum",
        "Quorum decision proposed",
        ("proposal_id", "topic", "proposer", "required_votes"),
        (),
        emitters=("event_bus",),
        source="quorum",
    ))
    _add(CatalogEntry(
        "quorum.voted", "quorum",
        "Node cast its quorum vote",
        ("proposal_id", "voter", "vote"),
        (),
        emitters=("event_bus",),
        source="quorum",
    ))
    _add(CatalogEntry(
        "quorum.decided", "quorum",
        "Quorum reached a decision",
        ("proposal_id", "decision", "votes_for", "votes_against"),
        (),
        emitters=("event_bus",),
        source="quorum",
    ))

    # ══════════════════════════════════════════════════════════════
    # Superpowers events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "superpowers.phase.changed", "superpowers",
        "Workflow phase transitioned",
        ("old_phase", "new_phase"),
        ("node_id", "timestamp", "source", "queued_races"),
        emitters=("event_bus", "event_schema"),
        source="superpowers",
    ))
    _add(CatalogEntry(
        "superpowers.phase_changed", "superpowers",
        "Workflow phase transitioned (registry alias)",
        ("old_phase", "new_phase", "source"),
        ("queued_races",),
        emitters=("event_bus",),
        source="superpowers",
    ))
    _add(CatalogEntry(
        "superpowers.race_blocked", "superpowers",
        "Race dispatch blocked by workflow phase",
        ("phase", "task"),
        ("queued",),
        emitters=("fleet_race",),
        source="superpowers",
    ))
    _add(CatalogEntry(
        "superpowers.race_released", "superpowers",
        "Queued races released after phase transition",
        ("phase", "count"),
        (),
        emitters=("fleet_race",),
        source="superpowers",
    ))

    # ══════════════════════════════════════════════════════════════
    # Synaptic events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "synaptic.insight", "synaptic",
        "Synaptic detected a pattern or anomaly",
        ("category", "severity", "message"),
        ("evidence", "node_id"),
        emitters=("event_bus", "synaptic_bridge"),
        source="synaptic-bridge",
    ))
    _add(CatalogEntry(
        "synaptic.pattern", "synaptic",
        "Cross-session pattern detected by Synaptic",
        ("pattern_type", "title", "confidence"),
        ("description",),
        emitters=("event_bus",),
        source="synaptic-bridge",
    ))
    _add(CatalogEntry(
        "synaptic.pattern.detected", "synaptic",
        "Synaptic pattern detected (bridge variant)",
        ("pattern_type", "title", "confidence"),
        ("description", "evidence"),
        emitters=("synaptic_bridge",),
        source="synaptic-bridge",
    ))
    _add(CatalogEntry(
        "synaptic.personality.update", "synaptic",
        "Personality trait evolved",
        ("trait", "strength"),
        ("evidence",),
        emitters=("event_bus",),
        source="synaptic-bridge",
    ))

    # ══════════════════════════════════════════════════════════════
    # Gate events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "gate.checked", "gate",
        "Invariance gate checked",
        ("gate_name", "passed"),
        ("details",),
        emitters=("invariance_gates",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "gate.result", "gate",
        "Invariance gate result recorded",
        ("gate_name", "passed"),
        ("errors", "warnings"),
        emitters=("invariance_gates",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "gate.acceptance.checked", "gate",
        "Acceptance gate checked",
        ("task_id", "passed"),
        ("evidence_count", "blocking_issues"),
        emitters=("acceptance_gate",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "gate.constitutional.checked", "gate",
        "Constitutional gate checked",
        ("rule", "passed"),
        ("detail",),
        emitters=("constitution",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Invariance events (EventEnvelope)
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "invariant.activated", "invariant",
        "Invariance check activated",
        ("invariant_name",),
        ("context",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "invariant.violated", "invariant",
        "Invariance violation detected",
        ("invariant_name", "violation"),
        ("severity",),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Task acceptance events (EventEnvelope)
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "task.accepted", "task",
        "Task accepted by acceptance gate",
        ("reason",),
        ("task_id",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "task.blocked", "task",
        "Task blocked by acceptance gate",
        ("blocking_issues",),
        ("task_id",),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # Acceptance events (registry)
    _add(CatalogEntry(
        "acceptance.passed", "acceptance",
        "Acceptance gate passed (registry variant)",
        ("task_id",),
        ("evidence_count",),
        emitters=("acceptance_gate",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "acceptance.blocked", "acceptance",
        "Acceptance gate blocked (registry variant)",
        ("task_id", "blocking_issues"),
        (),
        emitters=("acceptance_gate",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Race events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "race.started", "race",
        "Branch race started",
        ("race_id", "task"),
        ("nodes", "timeout_s"),
        emitters=("fleet_race", "event_schema"),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "race.completed", "race",
        "Branch race completed",
        ("race_id",),
        ("winner", "duration_s"),
        emitters=("event_schema",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "race.node.finished", "race",
        "Node finished its race branch",
        ("race_id", "node_id"),
        ("duration_s", "commit_hash"),
        emitters=("fleet_race", "event_schema"),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "race.node_completed", "race",
        "Node finished its race branch (registry alias)",
        ("race_id", "node_id"),
        ("duration_s", "commit_hash"),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "race.arbiter.pending", "race",
        "Race arbiter evaluation pending",
        ("race_id",),
        (),
        emitters=("event_schema",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "race.winner", "race",
        "Race winner selected by chief",
        ("race_id", "winner", "reason"),
        ("evidence",),
        emitters=("fleet_race",),
        source="multi-fleet",
        broadcast=True,
    ))
    _add(CatalogEntry(
        "race.abandoned", "race",
        "Race abandoned (timeout/error)",
        ("race_id", "reason"),
        (),
        emitters=("fleet_race",),
        source="multi-fleet",
        broadcast=True,
    ))

    # ══════════════════════════════════════════════════════════════
    # Corrigibility events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "corrigibility.decision.recorded", "corrigibility",
        "Corrigibility decision recorded",
        ("decision",),
        ("evidence",),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Synthesis / merge events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "synthesis.merge_decision", "synthesis",
        "Merge decision made during synthesis",
        ("decision", "reason"),
        (),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "synthesis.decision", "synthesis",
        "Synthesis decision recorded",
        ("decision",),
        ("evidence",),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Truth ladder events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "truth.entry.created", "truth",
        "Truth ladder entry created (EventEnvelope variant)",
        ("entry_id", "layer"),
        ("content",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "truth.promotion", "truth",
        "Truth ladder promotion (EventEnvelope variant)",
        ("entry_id", "from_layer", "to_layer"),
        ("promoted_by",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "truth.promoted", "truth",
        "Entry promoted up the truth ladder (registry variant)",
        ("entry_id", "from_layer", "to_layer"),
        ("promoted_by",),
        emitters=("memory_bridge",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "truth.rejected", "truth",
        "Promotion rejected",
        ("entry_id", "reason"),
        (),
        emitters=("memory_bridge",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Overlay events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "overlay.state.changed", "overlay",
        "Overlay state changed",
        ("overlay_id", "state"),
        (),
        emitters=("event_schema",),
        source="superset",
    ))

    # ══════════════════════════════════════════════════════════════
    # Review events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "review.started", "review",
        "Code review started",
        ("review_id",),
        ("files",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "review.verdict_received", "review",
        "Review verdict received from reviewer",
        ("review_id", "verdict"),
        ("reviewer",),
        emitters=("event_schema",),
        source="contextdna",
    ))
    _add(CatalogEntry(
        "review.completed", "review",
        "Code review completed",
        ("review_id", "result"),
        (),
        emitters=("event_schema",),
        source="contextdna",
    ))

    # ══════════════════════════════════════════════════════════════
    # Rebuttal events
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "rebuttal.filed", "rebuttal",
        "Cross-rebuttal filed against a claim",
        ("claim_id", "from_node"),
        ("severity",),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "rebuttal.resolved", "rebuttal",
        "Rebuttal chain resolved",
        ("claim_id", "resolution"),
        ("rounds",),
        emitters=("fleet_race",),
        source="multi-fleet",
    ))

    # ══════════════════════════════════════════════════════════════
    # Stress events (quorum stress scoring)
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "stress.level_change", "stress",
        "Fleet stress level changed",
        ("level", "score"),
        ("factors",),
        emitters=("quorum.stress_score",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "stress.critical", "stress",
        "Fleet stress reached critical level",
        ("level", "score"),
        ("factors",),
        emitters=("quorum.stress_score",),
        source="multi-fleet",
        broadcast=True,
    ))

    # ══════════════════════════════════════════════════════════════
    # Liaison events (message relay)
    # ══════════════════════════════════════════════════════════════

    _add(CatalogEntry(
        "liaison.message.received", "liaison",
        "Fleet message received via liaison",
        ("from", "type", "summary"),
        (),
        emitters=("liaison",),
        source="multi-fleet",
    ))
    _add(CatalogEntry(
        "liaison.message.sent", "liaison",
        "Fleet message sent via liaison",
        ("from", "type", "summary"),
        (),
        emitters=("liaison",),
        source="multi-fleet",
    ))

    return catalog


# ══════════════════════════════════════════════════════════════════
# Module-level catalog (built once on import)
# ══════════════════════════════════════════════════════════════════

CATALOG: dict[str, CatalogEntry] = _build_catalog()


def get_event_catalog() -> list[dict]:
    """Return the full event catalog as a list of dicts.

    Each dict has: name, namespace, description, required_fields,
    optional_fields, emitters, subscribers, broadcast, source.
    """
    return [entry.to_dict() for entry in CATALOG.values()]


def get_event(name: str) -> Optional[CatalogEntry]:
    """Look up a single event by name. Returns None if not found."""
    return CATALOG.get(name)


def list_events(
    namespace: str = None,
    source: str = None,
    broadcast_only: bool = False,
) -> list[CatalogEntry]:
    """Filter catalog entries by namespace, source, or broadcast flag."""
    entries = list(CATALOG.values())
    if namespace:
        entries = [e for e in entries if e.namespace == namespace]
    if source:
        entries = [e for e in entries if e.source == source]
    if broadcast_only:
        entries = [e for e in entries if e.broadcast]
    return entries


def list_namespaces() -> list[str]:
    """Return sorted list of all namespaces in the catalog."""
    return sorted({e.namespace for e in CATALOG.values()})


def validate_event(event_type: str, payload: dict) -> list[str]:
    """Validate a payload against the catalog entry for event_type.

    Returns list of validation errors (empty = valid).
    If the event_type is not in the catalog, returns a single warning.
    Also checks the event_registry.py for registry-level validation.
    """
    errors: list[str] = []

    # Try direct lookup
    entry = CATALOG.get(event_type)

    # Try stripping common prefixes (fleet. is added by FleetEventBus)
    if entry is None and event_type.startswith("fleet."):
        entry = CATALOG.get(event_type.removeprefix("fleet."))

    if entry is None:
        # Not a hard error — ad-hoc events are allowed but warned about
        logger.debug("event_not_in_catalog type=%s", event_type)
        return [f"event_type '{event_type}' not found in catalog (unregistered)"]

    # Validate required fields
    for req_field in entry.required_fields:
        if req_field not in payload:
            errors.append(f"missing required field '{req_field}' for {event_type}")

    if errors:
        logger.warning("event_validation_failed type=%s errors=%s", event_type, errors)

    return errors


def summary() -> dict:
    """Return a summary of the event catalog."""
    by_ns: dict[str, int] = {}
    broadcast_count = 0
    for e in CATALOG.values():
        by_ns[e.namespace] = by_ns.get(e.namespace, 0) + 1
        if e.broadcast:
            broadcast_count += 1
    return {
        "total_events": len(CATALOG),
        "namespaces": by_ns,
        "namespace_count": len(by_ns),
        "broadcast_events": broadcast_count,
    }
