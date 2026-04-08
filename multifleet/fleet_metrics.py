"""Fleet Metrics Aggregator -- collects and aggregates metrics across all nodes.

Feeds the web dashboard, provides fleet-wide statistics, and tracks
productivity metrics (messages/s, races/day, evidence entries, probe findings).

Unlike ``metrics.py`` (per-node in-memory counters) and ``prometheus_export.py``
(single-node Prometheus text), this module pulls from ALL reachable nodes and
computes fleet-wide aggregates + time-series trends.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.fleet-metrics")


@dataclass
class NodeMetrics:
    """Snapshot of a single node's metrics at a point in time."""

    node_id: str
    uptime_s: float = 0
    messages_sent: int = 0
    messages_received: int = 0
    errors: int = 0
    dedup_dropped: int = 0
    registered_types: int = 0
    active_sessions: int = 0
    channel_score: str = ""
    evidence_entries: int = 0
    active_leases: int = 0
    active_rebuttals: int = 0
    probe_findings: int = 0
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "uptime_s": self.uptime_s,
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "errors": self.errors,
            "dedup_dropped": self.dedup_dropped,
            "registered_types": self.registered_types,
            "active_sessions": self.active_sessions,
            "channel_score": self.channel_score,
            "evidence_entries": self.evidence_entries,
            "active_leases": self.active_leases,
            "active_rebuttals": self.active_rebuttals,
            "probe_findings": self.probe_findings,
            "last_updated": self.last_updated,
        }


def _fetch_json(url: str, timeout: float = 3.0) -> Optional[dict]:
    """GET JSON from url, return None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as exc:
        logger.debug("fetch %s failed: %s", url, exc)
        return None


class FleetMetricsAggregator:
    """Collects metrics from all reachable fleet nodes and aggregates them.

    Pulls data from the local daemon ``/health`` endpoint (which includes
    peer information), then builds per-node and fleet-wide summaries.

    Args:
        daemon_port: Local daemon HTTP port (default 8855).
    """

    def __init__(self, daemon_port: int = 8855) -> None:
        self._port = daemon_port
        self._base = f"http://127.0.0.1:{daemon_port}"
        self._nodes: dict[str, NodeMetrics] = {}
        self._history: list[dict] = []  # time-series snapshots
        self._max_history = 1000

    # ── Collection ──

    def collect(self) -> dict[str, NodeMetrics]:
        """Collect metrics from all reachable nodes via daemon /health.

        Returns dict mapping node_id -> NodeMetrics.
        """
        health = _fetch_json(f"{self._base}/health")
        if not health:
            logger.warning("Daemon unreachable at %s/health", self._base)
            return self._nodes

        now = time.time()

        # Local node
        node_id = health.get("node_id", "unknown")
        stats = health.get("stats", {})
        local = NodeMetrics(
            node_id=node_id,
            uptime_s=health.get("uptime_s", 0),
            messages_sent=stats.get("sent", 0),
            messages_received=stats.get("received", 0),
            errors=stats.get("errors", 0),
            dedup_dropped=stats.get("dedup_dropped", 0),
            registered_types=stats.get("registered_types", 0),
            active_sessions=stats.get("active_sessions", 0),
            channel_score=str(stats.get("channel_score", "")),
            last_updated=now,
        )
        self._nodes[node_id] = local

        # Remote peers from health.peers
        for peer in health.get("peers", []):
            pid = peer.get("node_id", peer.get("id", ""))
            if not pid or pid == node_id:
                continue
            peer_stats = peer.get("stats", {})
            remote = NodeMetrics(
                node_id=pid,
                uptime_s=peer.get("uptime_s", 0),
                messages_sent=peer_stats.get("sent", 0),
                messages_received=peer_stats.get("received", 0),
                errors=peer_stats.get("errors", 0),
                dedup_dropped=peer_stats.get("dedup_dropped", 0),
                channel_score=str(peer.get("channel_score", "")),
                last_updated=now,
            )
            self._nodes[pid] = remote

        # Snapshot for trend history
        agg = self._aggregate_raw()
        agg["timestamp"] = now
        self._history.append(agg)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        return self._nodes

    # ── Aggregation ──

    def _aggregate_raw(self) -> dict[str, Any]:
        """Compute raw fleet totals from current node data."""
        total_sent = 0
        total_recv = 0
        total_errors = 0
        total_evidence = 0
        total_probes = 0
        total_leases = 0
        total_rebuttals = 0
        total_dedup = 0
        max_uptime = 0.0
        node_count = len(self._nodes)

        for nm in self._nodes.values():
            total_sent += nm.messages_sent
            total_recv += nm.messages_received
            total_errors += nm.errors
            total_evidence += nm.evidence_entries
            total_probes += nm.probe_findings
            total_leases += nm.active_leases
            total_rebuttals += nm.active_rebuttals
            total_dedup += nm.dedup_dropped
            max_uptime = max(max_uptime, nm.uptime_s)

        return {
            "node_count": node_count,
            "total_messages_sent": total_sent,
            "total_messages_received": total_recv,
            "total_errors": total_errors,
            "total_evidence_entries": total_evidence,
            "total_probe_findings": total_probes,
            "total_active_leases": total_leases,
            "total_active_rebuttals": total_rebuttals,
            "total_dedup_dropped": total_dedup,
            "fleet_max_uptime_s": max_uptime,
        }

    def aggregate(self) -> dict[str, Any]:
        """Aggregate all node metrics into fleet-wide totals.

        Calls ``collect()`` first to refresh data.
        """
        self.collect()
        result = self._aggregate_raw()
        result["nodes"] = {nid: nm.to_dict() for nid, nm in self._nodes.items()}
        return result

    # ── Trends ──

    def trends(self, window_minutes: int = 60) -> dict[str, Any]:
        """Compute metric trends over a time window.

        Returns rates (messages/min, errors/min) and deltas for key counters.
        Requires at least 2 history snapshots within the window.
        """
        if len(self._history) < 2:
            return {"status": "insufficient_data", "snapshots": len(self._history)}

        now = time.time()
        cutoff = now - (window_minutes * 60)
        window = [s for s in self._history if s.get("timestamp", 0) >= cutoff]

        if len(window) < 2:
            return {"status": "insufficient_data", "snapshots": len(window)}

        first = window[0]
        last = window[-1]
        elapsed_s = last["timestamp"] - first["timestamp"]
        if elapsed_s <= 0:
            return {"status": "zero_elapsed", "snapshots": len(window)}

        elapsed_min = elapsed_s / 60.0

        def rate(key: str) -> float:
            delta = last.get(key, 0) - first.get(key, 0)
            return round(delta / elapsed_min, 2) if elapsed_min > 0 else 0

        return {
            "window_minutes": window_minutes,
            "snapshots": len(window),
            "elapsed_s": round(elapsed_s, 1),
            "messages_sent_per_min": rate("total_messages_sent"),
            "messages_recv_per_min": rate("total_messages_received"),
            "errors_per_min": rate("total_errors"),
            "evidence_per_hour": round(rate("total_evidence_entries") * 60, 2),
            "probes_per_hour": round(rate("total_probe_findings") * 60, 2),
        }

    # ── Productivity ──

    def productivity_score(self) -> dict[str, Any]:
        """Compute fleet productivity score.

        Based on: message throughput, evidence collected, rebuttals resolved,
        probes finding real issues, low error rate.
        Score 0-100, higher is better.
        """
        self.collect()
        agg = self._aggregate_raw()

        total_msgs = agg["total_messages_sent"] + agg["total_messages_received"]
        evidence = agg["total_evidence_entries"]
        probes = agg["total_probe_findings"]
        errors = agg["total_errors"]
        nodes = agg["node_count"]

        # Components (each 0-25, total 0-100)
        # 1. Communication health (messages flowing, low errors)
        error_ratio = errors / max(total_msgs, 1)
        comm_score = min(25, max(0, 25 * (1 - error_ratio * 10)))

        # 2. Evidence productivity
        evidence_score = min(25, evidence * 2.5)

        # 3. Probe effectiveness
        probe_score = min(25, probes * 5)

        # 4. Fleet participation (nodes online)
        participation_score = min(25, nodes * 8.33)

        total_score = round(comm_score + evidence_score + probe_score + participation_score, 1)

        return {
            "score": total_score,
            "max": 100,
            "components": {
                "communication_health": round(comm_score, 1),
                "evidence_productivity": round(evidence_score, 1),
                "probe_effectiveness": round(probe_score, 1),
                "fleet_participation": round(participation_score, 1),
            },
            "raw": {
                "total_messages": total_msgs,
                "total_evidence": evidence,
                "total_probes": probes,
                "total_errors": errors,
                "active_nodes": nodes,
            },
        }

    # ── Export ──

    def to_prometheus(self) -> str:
        """Export fleet-wide aggregated metrics in Prometheus text exposition format.

        Complements ``prometheus_export.py`` (which exports per-node metrics)
        by adding fleet-level totals and per-node breakdowns with labels.
        """
        self.collect()
        lines: list[str] = []

        # Fleet-wide totals
        agg = self._aggregate_raw()
        lines.append(f'# HELP fleet_nodes_total Number of known fleet nodes')
        lines.append(f'# TYPE fleet_nodes_total gauge')
        lines.append(f'fleet_nodes_total {agg["node_count"]}')
        lines.append("")

        for key, help_text, mtype in [
            ("total_messages_sent", "Fleet-wide total messages sent", "counter"),
            ("total_messages_received", "Fleet-wide total messages received", "counter"),
            ("total_errors", "Fleet-wide total errors", "counter"),
            ("total_evidence_entries", "Fleet-wide total evidence entries", "counter"),
            ("total_probe_findings", "Fleet-wide total probe findings", "counter"),
            ("total_active_leases", "Fleet-wide active leases", "gauge"),
            ("total_active_rebuttals", "Fleet-wide active rebuttals", "gauge"),
            ("total_dedup_dropped", "Fleet-wide dedup dropped messages", "counter"),
        ]:
            prom_name = f"fleet_{key}"
            lines.append(f"# HELP {prom_name} {help_text}")
            lines.append(f"# TYPE {prom_name} {mtype}")
            lines.append(f"{prom_name} {agg[key]}")
            lines.append("")

        # Per-node breakdown
        for nm in self._nodes.values():
            nid = nm.node_id.replace('"', '\\"')
            lines.append(f'fleet_node_messages_sent{{node="{nid}"}} {nm.messages_sent}')
            lines.append(f'fleet_node_messages_received{{node="{nid}"}} {nm.messages_received}')
            lines.append(f'fleet_node_errors{{node="{nid}"}} {nm.errors}')
            lines.append(f'fleet_node_uptime_seconds{{node="{nid}"}} {nm.uptime_s}')

        lines.append("")
        lines.append(f"# fleet_aggregator_scrape_timestamp {int(time.time())}")
        lines.append("")

        return "\n".join(lines)

    # ── Dashboard helpers ──

    def message_stats(self) -> dict[str, int]:
        """Get fleet-wide message stats for the dashboard Messages panel.

        Returns dict with sent, received, broadcasts, errors matching
        the dashboard renderMessages() expected shape.
        """
        self.collect()
        agg = self._aggregate_raw()
        return {
            "sent": agg["total_messages_sent"],
            "received": agg["total_messages_received"],
            "broadcasts": 0,  # filled by daemon stats if available
            "errors": agg["total_errors"],
        }
