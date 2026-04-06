"""Pluggable Packet Type Registry — consumers register schemas, multi-fleet validates."""

import json
import logging
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.registry")

# Optional jsonschema — degrade gracefully if not installed
_jsonschema = None
_jsonschema_warned = False
try:
    import jsonschema as _jsonschema
except ImportError:
    pass

BUILTIN_DIR = Path(__file__).parent.parent / "packet-types" / "_builtin"


# ── Deduplication Filter ────────────────────────────────────────────────────


class DeduplicationFilter:
    """Bounded in-memory dedup filter using OrderedDict for LRU eviction."""

    def __init__(self, max_size: int = 10_000):
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size

    def is_duplicate(self, msg_id: str) -> bool:
        """Check if msg_id was seen before. Records it if new.

        Returns True if duplicate (already seen), False if new.
        """
        if msg_id in self._seen:
            self._seen.move_to_end(msg_id)
            return True
        # Evict oldest if at capacity
        while len(self._seen) >= self._max_size:
            self._seen.popitem(last=False)
        self._seen[msg_id] = time.time()
        return False

    def size(self) -> int:
        """Current number of tracked message IDs."""
        return len(self._seen)

    def clear(self):
        """Reset the filter."""
        self._seen.clear()


# ── Packet Type Registry ───────────────────────────────────────────────────


class PacketRegistry:
    """Pluggable type registry where consumers register packet schemas.

    Multi-fleet validates envelopes. The registry validates payloads against
    consumer-registered JSON Schemas. Unregistered types pass through freely.
    """

    def __init__(self):
        self._types: dict[str, dict] = {}
        self._dedup = DeduplicationFilter()
        self.load_builtins()

    @property
    def dedup(self) -> DeduplicationFilter:
        """Access the deduplication filter."""
        return self._dedup

    def register(
        self,
        type_name: str,
        owner: str,
        version: int = 1,
        payload_schema: dict = None,
        ttl_s: int = 86400,
        priority: str = "normal",
        description: str = "",
    ) -> None:
        """Register a packet type with optional JSON Schema for payload validation.

        Args:
            type_name:      Unique packet type identifier (e.g. 'heartbeat', 'local_verdict').
            owner:          Which subsystem owns this type (e.g. 'fleet-nerve', 'context-dna').
            version:        Schema version number.
            payload_schema: Optional JSON Schema dict for payload validation.
            ttl_s:          Default TTL in seconds for packets of this type.
            priority:       Default priority ('low', 'normal', 'high', 'critical').
            description:    Human-readable description.
        """
        if type_name in self._types:
            logger.warning("overwriting registered type %r (owner: %s -> %s)",
                           type_name, self._types[type_name].get("owner"), owner)
        self._types[type_name] = {
            "type_name": type_name,
            "owner": owner,
            "version": version,
            "payload_schema": payload_schema,
            "ttl_s": ttl_s,
            "priority": priority,
            "description": description,
        }
        logger.debug("registered packet type %r (owner=%s, v%d)", type_name, owner, version)

    def load_file(self, path: Path) -> str:
        """Load a single .json schema file and register the type.

        The JSON file must contain at minimum: 'type_name' and 'owner'.
        Returns the type_name that was registered.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"schema file not found: {path}")
        with open(path) as f:
            data = json.load(f)
        type_name = data.get("type_name")
        if not type_name:
            raise ValueError(f"schema file missing 'type_name': {path}")
        self.register(
            type_name=type_name,
            owner=data.get("owner", "unknown"),
            version=data.get("version", 1),
            payload_schema=data.get("payload_schema") or data.get("payload"),
            ttl_s=data.get("ttl_s", 86400),
            priority=data.get("priority", "normal"),
            description=data.get("description", ""),
        )
        logger.debug("loaded schema from %s", path)
        return type_name

    def load_directory(self, dir_path: Path) -> list[str]:
        """Load all .json schema files from a directory recursively.

        Returns list of type_names loaded.
        """
        dir_path = Path(dir_path)
        if not dir_path.is_dir():
            logger.warning("schema directory does not exist: %s", dir_path)
            return []
        loaded = []
        for json_file in sorted(dir_path.rglob("*.json")):
            try:
                name = self.load_file(json_file)
                loaded.append(name)
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                logger.warning("skipping malformed schema %s: %s", json_file, exc)
        return loaded

    def load_builtins(self) -> list[str]:
        """Load built-in packet type schemas from packet-types/_builtin/.

        Auto-called on init. Returns list of type_names loaded.
        """
        if BUILTIN_DIR.is_dir():
            loaded = self.load_directory(BUILTIN_DIR)
            if loaded:
                logger.info("loaded %d builtin packet types", len(loaded))
            return loaded
        logger.debug("no builtin schema directory at %s", BUILTIN_DIR)
        return []

    def get(self, type_name: str) -> Optional[dict]:
        """Return schema dict for a registered type, or None if not registered."""
        return self._types.get(type_name)

    def list_types(self, owner: str = None) -> list[dict]:
        """List registered types, optionally filtered by owner.

        Returns list of schema dicts.
        """
        types = list(self._types.values())
        if owner:
            types = [t for t in types if t["owner"] == owner]
        return types

    def validate(self, type_name: str, payload: dict) -> list[str]:
        """Validate payload against the registered JSON Schema for this type.

        Returns:
            Empty list if valid, type not registered (passthrough), or
            jsonschema is not installed. List of error strings if invalid.
        """
        global _jsonschema_warned

        schema_entry = self._types.get(type_name)
        if not schema_entry:
            return []  # unregistered types pass through

        payload_schema = schema_entry.get("payload_schema")
        if not payload_schema:
            return []  # no schema defined — passthrough

        if _jsonschema is None:
            if not _jsonschema_warned:
                logger.warning("jsonschema not installed — payload validation disabled")
                _jsonschema_warned = True
            return []

        errors = []
        validator = _jsonschema.Draft7Validator(payload_schema)
        for error in validator.iter_errors(payload):
            errors.append(f"{error.json_path}: {error.message}")
        return errors

    def is_registered(self, type_name: str) -> bool:
        """Check if a packet type is registered."""
        return type_name in self._types


# ── Envelope helper ────────────────────────────────────────────────────────


def create_envelope(
    from_node: str,
    to: str,
    type: str,
    payload: dict,
    msg_id: str = None,
    version: int = 1,
    ttl_s: int = 86400,
) -> dict:
    """Create a properly structured packet envelope.

    Args:
        from_node: Sender node ID.
        to:        Target node ID or 'all' for broadcast.
        type:      Packet type string.
        payload:   Opaque payload dict.
        msg_id:    Optional message UUID (generated if not provided).
        version:   Envelope version number.
        ttl_s:     Time-to-live in seconds.

    Returns:
        Dict ready for transport.
    """
    from datetime import datetime, timezone

    return {
        "id": msg_id or str(uuid.uuid4()),
        "version": version,
        "type": type,
        "from_node": from_node,
        "to_node": to,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "ttl_s": ttl_s,
    }
