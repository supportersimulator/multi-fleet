"""DEPRECATED: Use node_overlay.py (NodeOverlayManager) instead.

Fleet Status Aggregator — single-source fleet status for IDE consumption.
health_dot, status_bar_text, and daemon caching have been migrated to
NodeOverlayManager. This module re-exports from node_overlay for backward
compatibility.
"""

import warnings
warnings.warn(
    "multifleet.status_aggregator is deprecated, use multifleet.node_overlay (NodeOverlayManager) instead",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export NodeOverlayManager for callers expecting FleetStatusAggregator
from multifleet.node_overlay import NodeOverlayManager  # noqa: F401

import json
import logging
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("fleet-nerve.status")

# How long to cache daemon health before re-fetching (seconds)
_CACHE_TTL_S = 5.0


class FleetStatusAggregator:
    """Aggregates all fleet state into a single snapshot."""

    def __init__(self, daemon_url: str = "http://127.0.0.1:8855", node_id: str = ""):
        self.daemon_url = daemon_url.rstrip("/")
        self.node_id = node_id
        self._cached_health: Optional[dict] = None
        self._cached_health_ts: float = 0.0

    # ── Public API ──

    def snapshot(self) -> dict:
        """Get complete fleet status snapshot."""
        daemon = self._get_daemon_health()
        peers = self._get_peer_summary(daemon)
        liaison = self._get_liaison_state(daemon)
        rebuttals = self._get_rebuttal_state(daemon)
        leases = self._get_lease_summary(daemon)
        events = self._get_recent_events(daemon)
        health_dot = self._compute_health_dot(daemon, peers)
        return {
            "timestamp": time.time(),
            "node_id": self.node_id,
            "daemon": daemon,
            "peers": peers,
            "liaison": liaison,
            "rebuttals": rebuttals,
            "leases": leases,
            "recent_events": events,
            "health_dot": health_dot,
        }

    def status_bar_text(self) -> str:
        """One-line status for IDE status bar. E.g., 'F:3 ● 2 reviews'"""
        snap = self.snapshot()
        dot_char = {"green": "●", "yellow": "◐", "red": "○"}.get(snap["health_dot"], "?")

        parts: list[str] = []

        # Peer count
        reachable = snap["peers"].get("reachable", 0)
        total = snap["peers"].get("total", 0)
        parts.append(f"F:{reachable}/{total}")

        # Health dot
        parts.append(dot_char)

        # Liaison highlights
        pending = snap["liaison"].get("pending_reviews", 0)
        if pending:
            parts.append(f"{pending} review{'s' if pending != 1 else ''}")

        active_rebuttals = snap["rebuttals"].get("active", 0)
        if active_rebuttals:
            parts.append(f"{active_rebuttals} rebuttal{'s' if active_rebuttals != 1 else ''}")

        unread = snap["liaison"].get("unread", 0)
        if unread:
            parts.append(f"{unread} unread")

        return " ".join(parts)

    # ── Internal: daemon health ──

    def _get_daemon_health(self) -> dict:
        """Fetch daemon /health, with short cache to avoid hammering."""
        now = time.time()
        if self._cached_health is not None and (now - self._cached_health_ts) < _CACHE_TTL_S:
            return self._cached_health

        try:
            req = urllib.request.Request(f"{self.daemon_url}/health", method="GET")
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read().decode("utf-8"))
            self._cached_health = data
            self._cached_health_ts = now
            return data
        except Exception as exc:
            logger.debug("Daemon health unavailable: %s", exc)
            return {"status": "unreachable", "error": str(exc)}

    # ── Internal: derived state ──

    def _get_peer_summary(self, daemon: dict) -> dict:
        """Extract peer info from daemon health."""
        peers = daemon.get("peers", {})
        if isinstance(peers, dict):
            peer_list = list(peers.values()) if peers else []
            peer_names = list(peers.keys())
        elif isinstance(peers, list):
            peer_list = peers
            peer_names = [p.get("node_id", "?") for p in peer_list]
        else:
            return {"total": 0, "reachable": 0, "peers": []}

        reachable = sum(
            1 for p in peer_list
            if isinstance(p, dict) and p.get("status") in ("online", "reachable", "ok")
        )
        return {
            "total": len(peer_list),
            "reachable": reachable,
            "peers": peer_names,
        }

    def _get_liaison_state(self, daemon: dict) -> dict:
        """Extract liaison state from daemon health or query separately."""
        liaison = daemon.get("liaison", {})
        if liaison:
            return {
                "pending_reviews": liaison.get("pending_reviews", 0),
                "active_rebuttals": liaison.get("active_rebuttals", 0),
                "unread": liaison.get("unread", 0),
                "summary": liaison.get("summary", ""),
            }
        # Try dedicated endpoint
        try:
            req = urllib.request.Request(f"{self.daemon_url}/liaison/status", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            return {
                "pending_reviews": len(data.get("pending_reviews", [])),
                "active_rebuttals": len(data.get("active_rebuttals", [])),
                "unread": len(data.get("unread_messages", [])),
                "summary": data.get("summary", ""),
            }
        except Exception:
            return {"pending_reviews": 0, "active_rebuttals": 0, "unread": 0, "summary": ""}

    def _get_rebuttal_state(self, daemon: dict) -> dict:
        """Extract rebuttal state from daemon health or query separately."""
        rebuttals = daemon.get("rebuttals", {})
        if rebuttals:
            return rebuttals
        # Try dedicated endpoint
        try:
            req = urllib.request.Request(f"{self.daemon_url}/rebuttals", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            active = [r for r in data if isinstance(r, dict) and r.get("state") not in ("resolved", "expired")]
            return {"active": len(active), "total": len(data)}
        except Exception:
            return {"active": 0, "total": 0}

    def _get_lease_summary(self, daemon: dict) -> dict:
        """Extract lease info from daemon health."""
        leases = daemon.get("leases", {})
        if leases:
            return leases
        return {"active": 0, "held_by_me": 0}

    def _get_recent_events(self, daemon: dict) -> list:
        """Get recent fleet events from daemon or EventBus."""
        events = daemon.get("recent_events", [])
        if events:
            return events[:10]
        # Try EventBus /events endpoint
        try:
            req = urllib.request.Request("http://127.0.0.1:8031/events", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data[:10]
        except Exception:
            pass
        return []

    def _compute_health_dot(self, daemon: dict, peers: dict) -> str:
        """Compute health indicator: green/yellow/red.

        green:  daemon up + NATS connected + >=1 peer reachable
        yellow: daemon up but degraded (no NATS or no peers)
        red:    daemon down / unreachable
        """
        status = daemon.get("status", "unreachable")
        if status == "unreachable":
            return "red"

        # Daemon is up — check NATS and peers
        nats_ok = daemon.get("nats", {}).get("connected", False) if isinstance(daemon.get("nats"), dict) else bool(daemon.get("nats_connected", False))
        reachable = peers.get("reachable", 0)

        if nats_ok and reachable >= 1:
            return "green"
        return "yellow"
