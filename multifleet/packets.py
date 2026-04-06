"""
Generic fleet packet envelope for multi-fleet transport.

Multi-fleet is pure generic transport — it validates the ENVELOPE (required fields,
types, dedup) but does NOT validate or interpret payload contents. The payload schema
is owned by whatever consumes it (Context DNA IDE, 3-surgeons, etc.).
"""

import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "fleet/v1"


# ── Errors ───────────────────────────────────────────────────────────────────


class PacketError(ValueError):
    """Raised when envelope data fails validation."""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Generic Envelope ─────────────────────────────────────────────────────────


@dataclass
class FleetPacket:
    """Generic fleet packet — multi-fleet routes but never parses the payload.

    Fields:
        id:          UUID for dedup
        version:     Protocol version ("fleet/v1")
        type:        Freeform string — multi-fleet doesn't interpret this
        from_node:   Sender node ID
        to_node:     Target node ID or "all" for broadcast
        timestamp:   ISO 8601
        payload:     OPAQUE dict — multi-fleet routes but doesn't parse
        ttl_minutes: Optional expiry (0 = no expiry)
    """
    type: str
    from_node: str
    to_node: str
    payload: dict = field(default_factory=dict)
    version: str = field(default=PROTOCOL_VERSION)
    id: str = field(default_factory=_new_id)
    timestamp: str = field(default_factory=_now_iso)
    ttl_minutes: int = 0

    def to_dict(self) -> dict:
        """Serialize to plain dict (JSON-safe)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FleetPacket":
        """Parse and validate a raw dict into a FleetPacket.

        Validates envelope fields only. Payload is passed through opaquely.
        """
        if not isinstance(data, dict):
            raise PacketError(f"packet must be a dict, got {type(data).__name__}")

        # Required string fields
        for key in ("type", "from_node", "to_node"):
            if key not in data:
                raise PacketError(f"missing required field '{key}'")
            if not isinstance(data[key], str):
                raise PacketError(f"field '{key}' must be str, got {type(data[key]).__name__}")

        # Payload must be a dict (opaque — contents not validated)
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise PacketError(f"field 'payload' must be dict, got {type(payload).__name__}")

        # Version check
        version = data.get("version", PROTOCOL_VERSION)
        if version != PROTOCOL_VERSION:
            raise PacketError(f"unsupported version '{version}', expected '{PROTOCOL_VERSION}'")

        # ttl_minutes must be non-negative int
        ttl = data.get("ttl_minutes", 0)
        if not isinstance(ttl, int) or ttl < 0:
            raise PacketError(f"ttl_minutes must be a non-negative int, got {ttl}")

        return cls(
            id=data.get("id", _new_id()),
            version=version,
            type=data["type"],
            from_node=data["from_node"],
            to_node=data["to_node"],
            timestamp=data.get("timestamp", _now_iso()),
            payload=payload,
            ttl_minutes=ttl,
        )


def validate_packet(data: Any) -> FleetPacket:
    """Parse and validate a raw dict into a FleetPacket.

    This is the main entry point for packet validation. Multi-fleet validates
    the envelope structure but treats the payload as opaque.
    """
    if not isinstance(data, dict):
        raise PacketError(f"packet must be a dict, got {type(data).__name__}")
    return FleetPacket.from_dict(data)


# ── Dedup tracker ────────────────────────────────────────────────────────────


class DedupTracker:
    """In-memory LRU tracker of seen packet IDs. Bounded to prevent unbounded growth.

    Uses OrderedDict for true LRU eviction — oldest entries are evicted first
    when capacity is reached, not random entries from a set.
    """

    def __init__(self, max_size: int = 10_000):
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size

    def is_duplicate(self, packet_id: str) -> bool:
        if packet_id in self._seen:
            # Move to end (most recently used)
            self._seen.move_to_end(packet_id)
            return True
        return False

    def mark_seen(self, packet_id: str):
        if packet_id in self._seen:
            self._seen.move_to_end(packet_id)
            return
        if len(self._seen) >= self._max_size:
            # Evict oldest (first) entry — true LRU
            self._seen.popitem(last=False)
        self._seen[packet_id] = None

    def check_and_mark(self, packet_id: str) -> bool:
        """Returns True if duplicate (already seen). Marks as seen if new."""
        if self.is_duplicate(packet_id):
            return True
        self.mark_seen(packet_id)
        return False

    def __len__(self) -> int:
        return len(self._seen)


# ── PacketStore integration ──────────────────────────────────────────────────


def ingest_packet(store, data: dict) -> tuple[FleetPacket, int]:
    """Validate a raw dict, then store it in a PacketStore.

    Returns (packet, row_id). Raises PacketError on invalid data.
    """
    packet = validate_packet(data)
    store_dict = packet.to_dict()
    # PacketStore uses "nodeId" for the node_id column
    if "from_node" in store_dict and "nodeId" not in store_dict:
        store_dict["nodeId"] = store_dict["from_node"]
    row_id = store.ingest(store_dict)
    return packet, row_id


def query_packets(store, packet_type: str = None, node_id: str = None, limit: int = 50) -> list[FleetPacket]:
    """Query PacketStore and return FleetPacket objects.

    Returns packets that successfully validate. Silently skips malformed rows.
    """
    raw_list = store.recent(packet_type=packet_type, node_id=node_id, limit=limit)
    results = []
    for raw in raw_list:
        try:
            results.append(validate_packet(raw))
        except PacketError:
            continue
    return results


# ── Backward compat aliases (deprecated) ─────────────────────────────────────

# Old names for callers that haven't updated yet
Packet = FleetPacket
ingest_typed_packet = ingest_packet
query_typed_packets = query_packets
