"""Fleet Dashboard Data Layer — aggregates EventBus events into dashboard-ready state.

Pulls from daemon HTTP endpoints (/health, /races, /evidence/latest, /gains-gate)
and merges into a single queryable snapshot. Caches aggressively to avoid hammering
the daemon on every TUI refresh (2s default).

Used by: dashboard.py (Rich TUI), future web dashboard, MCP resources.
"""

import json
import logging
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("fleet-nerve.dashboard-data")


class FleetDashboardData:
    """Aggregates fleet state from daemon HTTP endpoints into a single queryable snapshot.

    All fetches are synchronous with short timeouts — safe to call from TUI render loops.
    Results are cached for ``_cache_ttl`` seconds to avoid redundant daemon calls.
    """

    def __init__(self, daemon_port: int = 8855, cache_ttl: float = 5.0):
        self._port = daemon_port
        self._base = f"http://127.0.0.1:{daemon_port}"
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = cache_ttl

    # ── Public API ──

    def snapshot(self) -> dict:
        """Get complete fleet state snapshot for dashboard rendering.

        Returns a flat dict with all dashboard sections. Each section degrades
        gracefully to empty/default if the daemon endpoint is unreachable.
        """
        return {
            "timestamp": time.time(),
            "nodes": self._get_nodes(),
            "races": self._get_races(),
            "rebuttals": self._get_rebuttals(),
            "leases": self._get_leases(),
            "evidence": self._get_recent_evidence(),
            "gains_gate": self._get_gains_gate(),
            "eventbus": self._get_eventbus_stats(),
            "messages": self._get_message_stats(),
            "probe_results": self._get_probe_results(),
            "truth_ladder": self._get_truth_ladder(),
            "supervisor": self._get_supervisor(),
            "overlays": self._get_overlays(),
        }

    def race_live_view(self, race_id: str) -> dict:
        """Get live view of a specific race — participants, branches, status, rebuttals."""
        data = self._fetch(f"/race/{race_id}")
        if not data or "error" in data:
            return {"race_id": race_id, "status": "unknown", "error": data.get("error", "not found") if data else "unreachable"}

        # Enrich with rebuttal state if cross-rebuttal sessions reference this race
        rebuttals = self._get_rebuttals()
        race_rebuttals = [r for r in rebuttals if r.get("task_id", "").startswith(race_id)]
        data["rebuttals"] = race_rebuttals
        return data

    def node_detail(self, node_id: str) -> dict:
        """Get detailed view of a specific node from the health data."""
        health = self._fetch("/health")
        if not health:
            return {"node_id": node_id, "status": "unreachable"}

        peers = health.get("peers", {})
        if node_id == health.get("nodeId"):
            # This is us
            return {
                "node_id": node_id,
                "status": "self",
                "uptime_s": health.get("uptime_s", 0),
                "sessions": health.get("activeSessions", 0),
                "session_details": health.get("sessions", []),
                "vscode": health.get("vscode", "unknown"),
                "git": health.get("git", {}),
                "channel_health": health.get("channel_health", {}),
            }

        peer = peers.get(node_id)
        if not peer:
            return {"node_id": node_id, "status": "unknown"}

        ch = health.get("channel_health", {}).get(node_id, {})
        return {
            "node_id": node_id,
            "status": "online" if peer.get("lastSeen", 9999) < 60 else "stale",
            "last_seen_s": peer.get("lastSeen", 9999),
            "sessions": peer.get("sessions", 0),
            "transport": peer.get("transport", "?"),
            "vscode": peer.get("vscode", "?"),
            "channel_score": ch.get("score", "?"),
            "broken_channels": ch.get("broken", []),
        }

    # ── Section fetchers (each returns safe defaults on failure) ──

    def _get_nodes(self) -> list:
        """Get peer node status with health scores from /health channel_health."""
        health = self._fetch("/health")
        if not health:
            return []

        nodes = []
        ch = health.get("channel_health", {})

        # Add self
        nodes.append({
            "id": health.get("nodeId", "?"),
            "status": "online",
            "last_seen_s": 0,
            "sessions": health.get("activeSessions", 0),
            "channel_score": "self",
            "broken_channels": [],
            "is_self": True,
        })

        # Add peers
        for name, peer in health.get("peers", {}).items():
            ls = peer.get("lastSeen", 9999)
            peer_ch = ch.get(name, {})
            nodes.append({
                "id": name,
                "status": "online" if ls < 30 else ("stale" if ls < 120 else "offline"),
                "last_seen_s": ls,
                "sessions": peer.get("sessions", 0),
                "transport": peer.get("transport", "?"),
                "vscode": peer.get("vscode", "?"),
                "channel_score": peer_ch.get("score", "?"),
                "broken_channels": peer_ch.get("broken", []),
                "is_self": False,
            })
        return nodes

    def _get_races(self) -> list:
        """Get active and recent races from /races."""
        data = self._fetch("/races")
        if not data:
            return []
        return data.get("races", [])

    def _get_rebuttals(self) -> list:
        """Get active rebuttals. Parsed from /health or dedicated endpoint if available."""
        # No dedicated /rebuttals endpoint in the daemon yet — return empty
        # Future: GET /rebuttals when the endpoint is wired
        return []

    def _get_leases(self) -> list:
        """Get active leases. Parsed from /health or dedicated endpoint if available."""
        # No dedicated /leases endpoint yet — return empty
        # Future: GET /leases when the endpoint is wired
        return []

    def _get_recent_evidence(self) -> list:
        """Get last 10 evidence entries from /evidence/latest."""
        data = self._fetch("/evidence/latest")
        if not data:
            return []
        return data.get("entries", [])

    def _get_gains_gate(self) -> dict:
        """Get quick gains gate result from /gains-gate?quick=1."""
        data = self._fetch("/gains-gate?quick=1")
        if not data:
            return {"passed": False, "error": "unreachable"}
        return data

    def _get_eventbus_stats(self) -> dict:
        """Get EventBus stats from the /health eventbus field."""
        health = self._fetch("/health")
        if not health:
            return {}
        return health.get("eventbus", {})

    def _get_message_stats(self) -> dict:
        """Get message send/receive counts from /health stats field."""
        health = self._fetch("/health")
        if not health:
            return {}
        stats = health.get("stats", {})
        return {
            "sent": stats.get("sent", 0),
            "received": stats.get("received", 0),
            "broadcasts": stats.get("broadcasts", 0),
            "errors": stats.get("errors", 0),
        }

    def _get_probe_results(self) -> list:
        """Get latest probe scan results from /scan/results."""
        data = self._fetch("/scan/results")
        if not data:
            return []
        return data.get("results", [])

    def _get_truth_ladder(self) -> dict:
        """Get truth ladder statistics from /truth/stats."""
        data = self._fetch("/truth/stats")
        if not data:
            return {}
        return data

    def _get_supervisor(self) -> dict:
        """Get supervisor task state from /supervisor."""
        data = self._fetch("/supervisor")
        if not data:
            return {"tasks": [], "count": 0}
        return data

    def _get_overlays(self) -> list:
        """Get node overlay state from /overlays."""
        data = self._fetch("/overlays")
        if not data:
            return []
        return data.get("overlays", [])

    # ── HTTP fetch with caching ──

    def _fetch(self, path: str, timeout: float = 3.0) -> Optional[dict]:
        """Fetch JSON from daemon, with TTL cache.

        Returns None on any error (connection refused, timeout, bad JSON).
        Never raises — dashboard must never crash on daemon failure.
        """
        now = time.time()
        cached = self._cache.get(path)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        try:
            url = f"{self._base}{path}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                self._cache[path] = (now, data)
                return data
        except Exception as exc:
            logger.debug("Dashboard fetch %s failed: %s", path, exc)
            return None

    def invalidate_cache(self) -> None:
        """Clear all cached data — forces fresh fetch on next access."""
        self._cache.clear()
