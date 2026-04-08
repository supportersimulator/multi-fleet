"""Distributed Probe Runner — dispatches probes to fleet peers via NATS.

Chief node collects results from all peers, aggregates into fleet-wide
probe state. Each node runs probes locally and publishes findings.

Usage:
    runner = DistributedProbeRunner(daemon_port=8855, node_id="mac1")
    fleet_results = runner.run_fleet_scan()
"""

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Optional

from multifleet.probe_engine import ProbeResult

logger = logging.getLogger("fleet-nerve.probes.distributed")


class DistributedProbeRunner:
    """Dispatches probe scans to fleet peers and aggregates results."""

    def __init__(self, daemon_port: int = 8855, node_id: str = "unknown"):
        self._port = daemon_port
        self._node_id = node_id

    def run_fleet_scan(self, timeout_s: int = 30) -> dict:
        """Run probes on all fleet nodes and aggregate results.

        Returns dict with per-node results, fleet-wide summary, and
        cross-node comparison (same probe, different findings).
        """
        t0 = time.time()

        # Get peer list from daemon
        health = self._daemon_get("/health")
        if not health:
            return {"error": "daemon unreachable", "results": {}}

        peers = list(health.get("peers", {}).keys())
        all_nodes = [self._node_id] + [p for p in peers if not p.startswith("test")]

        # Run local probes
        local_results = self._run_local()

        # Request probes from peers via fleet message
        peer_results = {}
        for peer in peers:
            if peer.startswith("test"):
                continue
            result = self._request_peer_probes(peer, timeout_s)
            if result:
                peer_results[peer] = result

        # Aggregate
        all_results = {self._node_id: local_results}
        all_results.update(peer_results)

        # Cross-node comparison
        divergences = self._find_divergences(all_results)

        elapsed = time.time() - t0
        return {
            "nodes_scanned": len(all_results),
            "nodes": list(all_results.keys()),
            "results": {
                node: {
                    "total_probes": r.get("total_probes", 0),
                    "total_findings": r.get("total_findings", 0),
                    "surgeon_intensity": r.get("summary", {}).get("surgeon_intensity", "unknown"),
                    "probes": r.get("probes", []),
                }
                for node, r in all_results.items()
            },
            "divergences": divergences,
            "elapsed_s": round(elapsed, 1),
        }

    def _run_local(self) -> dict:
        """Run probes locally via the daemon /probes endpoint."""
        result = self._daemon_get("/probes")
        return result or {"probes": [], "total_probes": 0, "total_findings": 0}

    def _request_peer_probes(self, peer: str, timeout_s: int) -> Optional[dict]:
        """Request probe results from a peer node via daemon."""
        # Try direct HTTP to peer's daemon
        health = self._daemon_get("/health")
        if not health:
            return None

        # Get peer IP from config
        peers_config = health.get("peers", {})
        peer_info = peers_config.get(peer, {})

        # Try the peer's /probes endpoint directly
        peer_url = f"http://{peer}:8855/probes"  # Simplified — real impl uses config IPs
        try:
            req = urllib.request.Request(peer_url, method="GET")
            with urllib.request.urlopen(req, timeout=min(timeout_s, 10)) as r:
                return json.loads(r.read())
        except Exception:
            # Peer unreachable directly — send via fleet message
            self._daemon_post("/message", {
                "type": "task",
                "to": peer,
                "payload": {
                    "subject": "probe-scan-request",
                    "body": json.dumps({"action": "run_probes", "from": self._node_id}),
                },
            })
            return None

    def _find_divergences(self, all_results: dict) -> list[dict]:
        """Find probes where different nodes disagree on status/severity."""
        divergences = []

        # Collect probe results by probe_id across nodes
        by_probe: dict[str, dict[str, dict]] = {}
        for node, result in all_results.items():
            for probe in result.get("probes", []):
                pid = probe.get("probe_id", "")
                if pid not in by_probe:
                    by_probe[pid] = {}
                by_probe[pid][node] = {
                    "status": probe.get("status"),
                    "severity": probe.get("severity"),
                    "findings_count": len(probe.get("findings", [])),
                }

        # Check for divergences
        for probe_id, node_results in by_probe.items():
            statuses = set(r["status"] for r in node_results.values())
            severities = set(r["severity"] for r in node_results.values())

            if len(statuses) > 1 or len(severities) > 1:
                divergences.append({
                    "probe_id": probe_id,
                    "nodes": node_results,
                    "divergence_type": "status" if len(statuses) > 1 else "severity",
                })

        return divergences

    def _daemon_get(self, path: str, timeout: int = 5) -> Optional[dict]:
        try:
            url = f"http://127.0.0.1:{self._port}{path}"
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def _daemon_post(self, path: str, body: dict, timeout: int = 10) -> Optional[dict]:
        try:
            url = f"http://127.0.0.1:{self._port}{path}"
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:
            return None
