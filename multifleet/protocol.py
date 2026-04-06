"""Protocol version negotiation between fleet nodes.

Ensures nodes running different multi-fleet versions can detect
mismatches and degrade gracefully instead of silently diverging.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "fleet/v1"

_VERSION_RE = re.compile(r"^fleet/v(\d+)$")


def parse_major(version: str) -> int | None:
    """Extract major version number. Returns None if unparseable."""
    m = _VERSION_RE.match(version)
    return int(m.group(1)) if m else None


def protocol_compatible(local_version: str, peer_version: str | None) -> bool:
    """Check if a peer's protocol version is compatible with ours.

    Rules:
      - None / empty  → legacy node, compatible (warn)
      - Same major     → fully compatible
      - Different major → incompatible (warn)
      - Unparseable    → incompatible (warn)
    """
    if not peer_version:
        logger.warning("Peer sent no protocol version — treating as legacy node")
        return True

    local_major = parse_major(local_version)
    peer_major = parse_major(peer_version)

    if peer_major is None:
        logger.warning("Peer protocol version unparseable: %s", peer_version)
        return False

    if local_major == peer_major:
        return True

    logger.warning(
        "Protocol version mismatch: local=%s peer=%s", local_version, peer_version
    )
    return False


def check_peer_version(peer_version: str | None) -> tuple[bool, str]:
    """Convenience: check peer against current PROTOCOL_VERSION.

    Returns (compatible, status) where status is one of:
      "ok", "legacy", "version_mismatch", "unparseable"
    """
    if not peer_version:
        return True, "legacy"
    if parse_major(peer_version) is None:
        return False, "unparseable"
    if protocol_compatible(PROTOCOL_VERSION, peer_version):
        return True, "ok"
    return False, "version_mismatch"
