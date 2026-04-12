"""Event type registry and factory for ContextDNA Event v1 schema.

Defines namespaces, validates event types, and provides a factory function
for creating properly structured events.
"""

import time
import uuid
from typing import Optional

# ── Namespace registry ──

NAMESPACES: dict[str, str] = {
    "injection": "Python → TS: webhook injection events",
    "evidence": "Python → TS: evidence collection/grading",
    "skill": "Bidirectional: skill suggestions and activations",
    "health": "Python → TS: health checks and degradation",
    "ide": "TS → Python: IDE state changes",
    "surgeon": "Python → TS: cross-exam, consensus events",
    "phase": "Python → TS: workflow phase transitions",
    "fleet": "Multi-Fleet: peer, message, lease, rebuttal events",
    "probe": "Probe Engine: findings, scan completions, errors",
    "panel": "IDE Panel: lifecycle events (register, activate, suspend, close)",
    "quorum": "Quorum: distributed decision proposals, votes, outcomes",
    "breaker": "Reserved for Phase 4 circuit breaker",
}

# Current schema version
EVENT_VERSION = 1


def validate_namespace(event_type: str) -> bool:
    """Check that event_type starts with a known namespace.

    Returns True if valid, False if the namespace is unknown.
    Dotted format expected: 'namespace.action' or 'namespace.sub.action'.
    """
    if not event_type or "." not in event_type:
        return False
    ns = event_type.split(".")[0]
    return ns in NAMESPACES


def create_event(
    event_type: str,
    payload: dict,
    source: str = "python",
    correlation_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Create a plain dict ContextDNA Event v1 — used by the in-process EventBus.

    For typed EventEnvelope events (with signal_class, quorum sensing),
    use event_schema.create_event() instead.

    Args:
        event_type: Dotted namespace event type (e.g. 'injection.completed').
        payload: Type-specific event data.
        source: Origin of the event ('python', 'typescript', 'hook', 'websocket', etc.).
        correlation_id: Optional ID to chain related events.
        session_id: Optional session identifier.

    Returns:
        Event dict with id, version, type, source, timestamp, payload, metadata.
    """
    metadata: dict = {}
    if correlation_id:
        metadata["correlation_id"] = correlation_id
    if session_id:
        metadata["session_id"] = session_id

    return {
        "id": str(uuid.uuid4()),
        "version": EVENT_VERSION,
        "type": event_type,
        "source": source,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
        "metadata": metadata,
    }
