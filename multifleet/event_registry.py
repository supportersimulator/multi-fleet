"""Fleet Event Types Registry — canonical list of all GhostScan event types.

Defines the complete event taxonomy so producers and consumers agree on
event shapes. Each event type has a namespace, description, and required
payload fields. Emitters validate against this registry.

Usage:
    from multifleet.event_registry import EVENTS, validate_event, list_events
    validate_event("scan.finding", {"probe_id": "mirror_drift", ...})
    for e in list_events(namespace="scan"): print(e)
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("fleet-nerve.event_registry")


@dataclass(frozen=True)
class EventType:
    """Definition of a fleet event type."""
    name: str                        # e.g. "scan.finding"
    namespace: str                   # e.g. "scan"
    description: str
    required_fields: tuple[str, ...]  # payload keys that MUST be present
    optional_fields: tuple[str, ...] = ()
    broadcast: bool = False          # if True, sent to all fleet nodes


# ── Scan Events (GhostScan probe output) ──
_SCAN_EVENTS = [
    EventType("scan.finding", "scan", "Probe finding detected",
              ("probe_id", "title", "severity", "confidence"),
              ("blast_radius", "file_path", "summary")),
    EventType("scan.complete", "scan", "Probe scan run completed",
              ("cost_limit", "probes_run", "findings_count"),
              ("duration_ms",)),
    EventType("scan.error", "scan", "Probe execution error",
              ("probe_id", "error"),
              ("duration_ms",)),
    EventType("scan.registered", "scan", "New probe registered in engine",
              ("probe_id", "category", "cost")),
]

# ── Supervisor Events (Dev Squad execution) ──
_SUPERVISOR_EVENTS = [
    EventType("supervisor.phase_transition", "supervisor", "Task phase changed",
              ("task_id", "from", "to"),
              ("iteration",)),
    EventType("supervisor.blocked", "supervisor", "Task blocked",
              ("task_id", "reason"),
              ("phase",)),
    EventType("supervisor.stall_detected", "supervisor", "Watchdog detected stall",
              ("task_id", "phase", "elapsed_s"),
              ("action",)),
    EventType("supervisor.completed", "supervisor", "Task completed",
              ("task_id", "criteria_met"),
              ("elapsed_s",)),
]

# ── Peer Events (fleet node status) ──
_PEER_EVENTS = [
    EventType("peer.online", "peer", "Fleet node came online",
              ("peer_id",), ("sessions",), broadcast=True),
    EventType("peer.offline", "peer", "Fleet node went offline",
              ("peer_id",), ("last_seen_s",), broadcast=True),
    EventType("peer.heartbeat", "peer", "Periodic heartbeat from node",
              ("peer_id", "uptime_s"),
              ("active_tasks",)),
]

# ── Lease Events (distributed resource locking) ──
_LEASE_EVENTS = [
    EventType("lease.acquired", "lease", "Resource lease acquired",
              ("resource_id", "holder"), broadcast=True),
    EventType("lease.released", "lease", "Resource lease released",
              ("resource_id", "holder"), broadcast=True),
    EventType("lease.expired", "lease", "Resource lease expired (timeout)",
              ("resource_id", "holder"),
              ("held_for_s",), broadcast=True),
    EventType("lease.contention", "lease", "Multiple nodes competing for lease",
              ("resource_id", "contenders")),
]

# ── Health Events ──
_HEALTH_EVENTS = [
    EventType("health.degraded", "health", "Node health score dropped",
              ("peer_id", "score", "broken"), broadcast=True),
    EventType("health.recovered", "health", "Node health restored",
              ("peer_id", "score"), broadcast=True),
    EventType("health.channel_repair", "health", "Communication channel repaired",
              ("channel", "target"),
              ("repair_method",)),
]

# ── Message Events (fleet communication) ──
_MESSAGE_EVENTS = [
    EventType("message.sent", "message", "Fleet message sent",
              ("target", "channel", "type"),
              ("elapsed_s",)),
    EventType("message.received", "message", "Fleet message received",
              ("from",), ("type",)),
    EventType("message.failed", "message", "Fleet message delivery failed",
              ("target", "channel", "error")),
]

# ── Race Events (fleet_race competitive branches) ──
_RACE_EVENTS = [
    EventType("race.started", "race", "Branch race started",
              ("race_id", "task", "nodes"),
              ("timeout_s",), broadcast=True),
    EventType("race.node_completed", "race", "Node finished its race branch",
              ("race_id", "node_id"),
              ("duration_s", "commit_hash")),
    EventType("race.winner", "race", "Race winner selected by chief",
              ("race_id", "winner", "reason"),
              ("evidence",), broadcast=True),
    EventType("race.abandoned", "race", "Race abandoned (timeout/error)",
              ("race_id", "reason"), broadcast=True),
]

# ── Evidence Events (ledger + sync) ──
_EVIDENCE_EVENTS = [
    EventType("evidence.recorded", "evidence", "New entry in evidence ledger",
              ("entry_id", "event_type", "node_id"),
              ("subject",)),
    EventType("evidence.chain_broken", "evidence", "Hash chain integrity failure",
              ("broken_at_seq", "detail")),
    EventType("evidence.synced", "evidence", "Evidence synced from remote node",
              ("from_node", "entries_imported"),
              ("entries_skipped",)),
]

# ── Truth Ladder Events ──
_TRUTH_EVENTS = [
    EventType("truth.promoted", "truth", "Entry promoted up the truth ladder",
              ("entry_id", "from_layer", "to_layer"),
              ("promoted_by",)),
    EventType("truth.rejected", "truth", "Promotion rejected",
              ("entry_id", "reason")),
]

# ── Rebuttal Events ──
_REBUTTAL_EVENTS = [
    EventType("rebuttal.filed", "rebuttal", "Cross-rebuttal filed",
              ("claim_id", "from_node"),
              ("severity",)),
    EventType("rebuttal.resolved", "rebuttal", "Rebuttal chain resolved",
              ("claim_id", "resolution"),
              ("rounds",)),
]

# ── Acceptance Events ──
_ACCEPTANCE_EVENTS = [
    EventType("acceptance.passed", "acceptance", "Acceptance gate passed",
              ("task_id",), ("evidence_count",)),
    EventType("acceptance.blocked", "acceptance", "Acceptance gate blocked",
              ("task_id", "blocking_issues")),
]

# ── Superpowers Workflow Events (Wire 5: workflow-fleet coordination) ──
_SUPERPOWERS_EVENTS = [
    EventType("superpowers.phase_changed", "superpowers",
              "Workflow phase transitioned",
              ("old_phase", "new_phase", "source"),
              ("queued_races",)),
    EventType("superpowers.race_blocked", "superpowers",
              "Race dispatch blocked by workflow phase",
              ("phase", "task"),
              ("queued",)),
    EventType("superpowers.race_released", "superpowers",
              "Queued races released after phase transition",
              ("phase", "count")),
]

# ══════════════════════════════════════════════════════════════
# Master registry
# ══════════════════════════════════════════════════════════════

EVENTS: dict[str, EventType] = {}
for _group in [
    _SCAN_EVENTS, _SUPERVISOR_EVENTS, _PEER_EVENTS, _LEASE_EVENTS,
    _HEALTH_EVENTS, _MESSAGE_EVENTS, _RACE_EVENTS, _EVIDENCE_EVENTS,
    _TRUTH_EVENTS, _REBUTTAL_EVENTS, _ACCEPTANCE_EVENTS,
    _SUPERPOWERS_EVENTS,
]:
    for evt in _group:
        EVENTS[evt.name] = evt


def validate_event(event_type: str, payload: dict) -> list[str]:
    """Validate a payload against the registered event type.

    Returns list of validation errors (empty = valid).
    Unregistered event types get a warning but no errors.
    """
    # Strip fleet. prefix if present (EventBus adds it)
    clean = event_type.removeprefix("fleet.")

    if clean not in EVENTS:
        logger.debug("event_unregistered type=%s (no validation)", clean)
        return []

    defn = EVENTS[clean]
    errors = []
    for field in defn.required_fields:
        if field not in payload:
            errors.append(f"missing required field '{field}' for {clean}")

    if errors:
        logger.warning("event_validation_failed type=%s errors=%s", clean, errors)
    return errors


def list_events(namespace: str = None) -> list[EventType]:
    """List all registered event types, optionally filtered by namespace."""
    if namespace:
        return [e for e in EVENTS.values() if e.namespace == namespace]
    return list(EVENTS.values())


def list_namespaces() -> list[str]:
    """List all event namespaces."""
    return sorted({e.namespace for e in EVENTS.values()})


def summary() -> dict:
    """Return a summary of the event registry."""
    by_ns: dict[str, int] = {}
    broadcast_count = 0
    for e in EVENTS.values():
        by_ns[e.namespace] = by_ns.get(e.namespace, 0) + 1
        if e.broadcast:
            broadcast_count += 1
    return {
        "total_event_types": len(EVENTS),
        "namespaces": by_ns,
        "broadcast_events": broadcast_count,
    }
