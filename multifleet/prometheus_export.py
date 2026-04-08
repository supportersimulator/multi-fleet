"""Prometheus text exposition format exporter for fleet metrics.

Reads from:
- FleetMetrics singleton (counters, gauges, histograms)
- Daemon /health endpoint (peers, uptime)
- FleetDoctor report (check pass/fail counts)
- ProbeEngine results (findings by severity)
- LeaseManager stats (active leases)
- RebuttalManager (active sessions)

Output: Prometheus text exposition format (text/plain; version=0.0.4)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.prometheus")


def _safe_label(value: str) -> str:
    """Escape a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_metric(
    name: str,
    value: float | int,
    help_text: str = "",
    metric_type: str = "gauge",
    labels: Optional[dict[str, str]] = None,
) -> str:
    """Format a single metric in Prometheus text exposition."""
    lines: list[str] = []
    if help_text:
        lines.append(f"# HELP {name} {help_text}")
    if metric_type:
        lines.append(f"# TYPE {name} {metric_type}")
    label_str = ""
    if labels:
        parts = [f'{k}="{_safe_label(str(v))}"' for k, v in sorted(labels.items())]
        label_str = "{" + ",".join(parts) + "}"
    lines.append(f"{name}{label_str} {value}")
    return "\n".join(lines)


def _fetch_health(daemon_url: str = "http://127.0.0.1:8855", timeout: int = 3) -> dict:
    """Fetch daemon /health endpoint."""
    try:
        resp = urllib.request.urlopen(f"{daemon_url}/health", timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        logger.debug("Health fetch failed: %s", e)
        return {}


def _collect_daemon_metrics(health: dict, node_id: str) -> list[str]:
    """Extract Prometheus metrics from daemon health data."""
    blocks: list[str] = []
    labels = {"node": node_id}

    if "uptime_s" in health:
        blocks.append(_format_metric(
            "fleet_daemon_uptime_seconds", health["uptime_s"],
            "Daemon uptime in seconds", "gauge", labels,
        ))

    peers = health.get("peers", [])
    blocks.append(_format_metric(
        "fleet_peers_connected", len(peers),
        "Number of connected peers", "gauge", labels,
    ))

    if "messages_sent" in health:
        blocks.append(_format_metric(
            "fleet_messages_sent_total", health["messages_sent"],
            "Total messages sent", "counter", labels,
        ))

    if "messages_received" in health:
        blocks.append(_format_metric(
            "fleet_messages_received_total", health["messages_received"],
            "Total messages received", "counter", labels,
        ))

    return blocks


def _collect_metrics_singleton(snapshot: dict, node_id: str) -> list[str]:
    """Convert FleetMetrics snapshot to Prometheus lines."""
    blocks: list[str] = []
    labels = {"node": node_id}

    # Counters
    for name, value in sorted(snapshot.get("counters", {}).items()):
        prom_name = f"fleet_{name.replace('.', '_').replace('-', '_')}"
        blocks.append(_format_metric(prom_name, value, "", "counter", labels))

    # Gauges
    for name, value in sorted(snapshot.get("gauges", {}).items()):
        prom_name = f"fleet_{name.replace('.', '_').replace('-', '_')}"
        blocks.append(_format_metric(prom_name, value, "", "gauge", labels))

    # Histograms (expose as summary-style: min, max, avg, p95, count)
    for name, stats in sorted(snapshot.get("histograms", {}).items()):
        base = f"fleet_{name.replace('.', '_').replace('-', '_')}"
        for stat_name in ("min", "max", "avg", "p95"):
            if stat_name in stats:
                blocks.append(_format_metric(
                    f"{base}_{stat_name}", stats[stat_name], "", "gauge", labels,
                ))
        if "count" in stats:
            blocks.append(_format_metric(
                f"{base}_count", stats["count"], "", "counter", labels,
            ))

    return blocks


def _collect_doctor_metrics(doctor_report: Optional[dict], node_id: str) -> list[str]:
    """Convert doctor report to Prometheus metrics."""
    if not doctor_report:
        return []
    blocks: list[str] = []
    labels = {"node": node_id}

    passed = doctor_report.get("passed", 0)
    failed = doctor_report.get("failed", 0)
    warned = doctor_report.get("warned", 0)

    blocks.append(_format_metric(
        "fleet_doctor_checks_passed", passed,
        "Doctor checks passed", "gauge", labels,
    ))
    blocks.append(_format_metric(
        "fleet_doctor_checks_failed", failed,
        "Doctor checks failed", "gauge", labels,
    ))
    blocks.append(_format_metric(
        "fleet_doctor_checks_warned", warned,
        "Doctor checks with warnings", "gauge", labels,
    ))

    return blocks


def _collect_probe_metrics(probe_results: Optional[list[dict]], node_id: str) -> list[str]:
    """Convert probe findings to Prometheus metrics."""
    if not probe_results:
        return []
    blocks: list[str] = []

    # Count findings by probe_id and severity
    for result in probe_results:
        probe_id = result.get("probe_id", "unknown")
        severity = result.get("severity", "info")
        findings_count = len(result.get("findings", []))
        labels = {"node": node_id, "probe": probe_id, "severity": severity}
        blocks.append(_format_metric(
            "fleet_probe_findings_total", findings_count,
            "Probe findings count", "gauge", labels,
        ))

    return blocks


def _collect_lease_metrics(lease_stats: Optional[dict], node_id: str) -> list[str]:
    """Convert lease stats to Prometheus metrics."""
    if not lease_stats:
        return []
    blocks: list[str] = []
    labels = {"node": node_id}

    for key in ("acquired", "released", "renewed", "denied", "expired_cleaned"):
        if key in lease_stats:
            blocks.append(_format_metric(
                f"fleet_lease_{key}_total", lease_stats[key],
                f"Lease {key} count", "counter", labels,
            ))

    active = lease_stats.get("active", 0)
    blocks.append(_format_metric(
        "fleet_lease_active", active,
        "Currently active leases", "gauge", labels,
    ))

    return blocks


def _collect_rebuttal_metrics(active_count: int, node_id: str) -> list[str]:
    """Rebuttal session count as Prometheus metric."""
    return [_format_metric(
        "fleet_rebuttal_sessions_active", active_count,
        "Active rebuttal sessions", "gauge", {"node": node_id},
    )]


def generate_prometheus_text(
    node_id: str = "unknown",
    daemon_url: str = "http://127.0.0.1:8855",
    metrics_snapshot: Optional[dict] = None,
    doctor_report: Optional[dict] = None,
    probe_results: Optional[list[dict]] = None,
    lease_stats: Optional[dict] = None,
    rebuttal_active: int = 0,
    include_health: bool = True,
) -> str:
    """Generate full Prometheus text exposition for fleet metrics.

    Args:
        node_id: This node's identifier
        daemon_url: URL for the fleet daemon
        metrics_snapshot: Pre-fetched FleetMetrics.snapshot() dict (if None, uses singleton)
        doctor_report: Pre-fetched DoctorReport.to_dict() (if None, skips doctor metrics)
        probe_results: List of ProbeResult.to_dict() dicts (if None, skips probe metrics)
        lease_stats: Lease stats dict with active count (if None, skips lease metrics)
        rebuttal_active: Number of active rebuttal sessions
        include_health: Whether to fetch daemon /health endpoint

    Returns:
        Prometheus text exposition string
    """
    blocks: list[str] = []

    # Daemon health
    if include_health:
        health = _fetch_health(daemon_url)
        blocks.extend(_collect_daemon_metrics(health, node_id))

    # FleetMetrics singleton
    if metrics_snapshot is None:
        try:
            from multifleet.metrics import fleet_metrics
            metrics_snapshot = fleet_metrics.snapshot()
        except Exception:
            metrics_snapshot = {}
    blocks.extend(_collect_metrics_singleton(metrics_snapshot, node_id))

    # Doctor
    blocks.extend(_collect_doctor_metrics(doctor_report, node_id))

    # Probes
    blocks.extend(_collect_probe_metrics(probe_results, node_id))

    # Leases
    blocks.extend(_collect_lease_metrics(lease_stats, node_id))

    # Rebuttals
    blocks.extend(_collect_rebuttal_metrics(rebuttal_active, node_id))

    # Timestamp line
    blocks.append(f"# fleet_scrape_timestamp {int(time.time())}")

    return "\n\n".join(blocks) + "\n"
