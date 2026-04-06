"""
MCP Resources for fleet status — subscribable by IDE extensions.

Exposes three resources:
  fleet://health  — current fleet health (peers, channels, stats)
  fleet://races   — active race status
  fleet://doctor  — last doctor report

Each returns JSON. Designed for MCP resource protocol consumption.
"""

import json
import logging
import time
import urllib.request
from typing import Any

logger = logging.getLogger("fleet-nerve.mcp-resources")

DAEMON_URL = "http://127.0.0.1:8855"
_doctor_cache: dict[str, Any] = {"report": None, "ts": 0.0}
DOCTOR_CACHE_TTL = 30.0  # seconds


def _fetch(path: str, timeout: float = 3.0) -> dict | None:
    """Fetch JSON from fleet daemon."""
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.debug("fetch %s failed: %s", path, e)
        return None


def fleet_health() -> dict:
    """fleet://health — peers, channels, uptime, git state."""
    health = _fetch("/health")
    if health is None:
        return {"status": "unreachable", "daemon_url": DAEMON_URL}
    # Enrich with peer data
    peers = _fetch("/peers")
    result = {
        "status": health.get("status", "unknown"),
        "node_id": health.get("nodeId", health.get("node_id", "unknown")),
        "uptime_s": health.get("uptime_s", 0),
        "transport": health.get("transport", "unknown"),
        "nats_connected": health.get("nats_url") is not None,
        "active_sessions": health.get("activeSessions", 0),
        "peers": peers.get("peers", []) if peers else [],
        "channel_health": health.get("channel_health", {}),
        "timestamp": time.time(),
    }
    return result


def fleet_races() -> dict:
    """fleet://races — active race status and summaries."""
    data = _fetch("/races")
    if data is None:
        return {"status": "unreachable", "races": [], "count": 0}
    races = data if isinstance(data, list) else data.get("races", [])
    return {
        "status": "ok",
        "races": races,
        "count": len(races),
        "timestamp": time.time(),
    }


def fleet_doctor() -> dict:
    """fleet://doctor — last doctor report (cached for 30s)."""
    now = time.time()
    if _doctor_cache["report"] and (now - _doctor_cache["ts"]) < DOCTOR_CACHE_TTL:
        return _doctor_cache["report"]
    data = _fetch("/doctor", timeout=10.0)
    if data is None:
        return {"status": "unreachable", "healthy": False, "checks": []}
    _doctor_cache["report"] = data
    _doctor_cache["ts"] = now
    return data


# Resource registry — maps URI to handler
RESOURCES = {
    "fleet://health": {
        "uri": "fleet://health",
        "name": "Fleet Health",
        "description": "Current fleet health: peers, channels, uptime, sessions",
        "mimeType": "application/json",
        "fn": fleet_health,
    },
    "fleet://races": {
        "uri": "fleet://races",
        "name": "Fleet Races",
        "description": "Active competitive branch races and their status",
        "mimeType": "application/json",
        "fn": fleet_races,
    },
    "fleet://doctor": {
        "uri": "fleet://doctor",
        "name": "Fleet Doctor",
        "description": "Last diagnostic health report (14 checks, cached 30s)",
        "mimeType": "application/json",
        "fn": fleet_doctor,
    },
}


def list_resources() -> list[dict]:
    """Return MCP resource descriptors (without fn)."""
    return [
        {k: v for k, v in r.items() if k != "fn"}
        for r in RESOURCES.values()
    ]


def read_resource(uri: str) -> dict:
    """Read a resource by URI. Returns JSON content."""
    res = RESOURCES.get(uri)
    if res is None:
        return {"error": f"unknown resource: {uri}"}
    return res["fn"]()
