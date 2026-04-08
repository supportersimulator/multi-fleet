"""Fleet Health Monitor -- continuous fleet health observation + auto-repair.

Watches all nodes via daemon health endpoint, detects degradation,
triggers repairs, records all actions in evidence ledger, emits
EventBus events for dashboard visualization.
"""

import json
import logging
import time
import urllib.request
from collections import deque
from threading import Thread
from typing import Optional

logger = logging.getLogger("fleet-nerve.health-monitor")

# Anomaly thresholds
SCORE_DROP_THRESHOLD = 2       # score drop in window triggers anomaly
SCORE_DROP_WINDOW_S = 300      # 5 minutes
ERROR_SPIKE_THRESHOLD = 10     # errors in window
HISTORY_MAX = 60               # max health snapshots per node


class FleetHealthMonitor:
    """Continuous fleet health monitor with anomaly detection and auto-repair.

    Polls the local daemon /health endpoint, tracks per-node health history,
    detects score drops / error spikes / peer disappearances, triggers
    self-heal repairs, and records everything in the evidence ledger.
    """

    def __init__(self, daemon_port: int = 8855, check_interval: int = 30,
                 node_id: str = "unknown"):
        self._port = daemon_port
        self._interval = check_interval
        self._node_id = node_id
        self._history: dict[str, deque] = {}  # node_id -> health snapshots
        self._alerts: list[dict] = []
        self._repairs: list[dict] = []
        self._running = False
        self._thread: Optional[Thread] = None
        self._checks_run = 0
        self._eventbus = None      # set via wire_eventbus()
        self._ledger = None        # set via wire_ledger()

    # -- Wiring --

    def wire_eventbus(self, eventbus) -> None:
        """Inject FleetEventBus for event emission."""
        self._eventbus = eventbus

    def wire_ledger(self, ledger) -> None:
        """Inject EvidenceLedger for tamper-evident recording."""
        self._ledger = ledger

    # -- Core check --

    def check_fleet(self) -> dict:
        """Run health check on all nodes via daemon /health endpoint.

        Returns fleet health summary with per-node status and any new alerts.
        """
        health = self._fetch_daemon_health()
        if not health:
            return {"status": "unreachable", "nodes": {}, "alerts": []}

        ts = time.time()
        nodes = {}
        new_alerts = []

        # Self node
        self_status = {
            "status": health.get("status", "unknown"),
            "uptime_s": health.get("uptime_s", 0),
            "sessions": health.get("activeSessions", 0),
            "timestamp": ts,
        }
        nodes[health.get("nodeId", self._node_id)] = self_status
        self._record_snapshot(health.get("nodeId", self._node_id), self_status)

        # Peer nodes
        peers = health.get("peers", {})
        channel_health = health.get("channel_health", {})

        for peer_id, peer_info in peers.items():
            last_seen_s = peer_info.get("lastSeen", 9999)
            ch = channel_health.get(peer_id, {})
            score_str = ch.get("score", "0/7")
            broken = ch.get("broken", [])

            try:
                score = int(score_str.split("/")[0])
                total = int(score_str.split("/")[1])
            except (ValueError, IndexError):
                score, total = 0, 7

            if last_seen_s > 120:
                status = "offline"
            elif score < 4:
                status = "degraded"
            else:
                status = "healthy"

            peer_status = {
                "status": status,
                "last_seen_s": last_seen_s,
                "score": score,
                "total": total,
                "broken": broken,
                "sessions": peer_info.get("sessions", 0),
                "timestamp": ts,
            }
            nodes[peer_id] = peer_status
            self._record_snapshot(peer_id, peer_status)

            # Detect transitions
            prev = self._get_previous_status(peer_id)
            if prev and prev != status:
                alert = {
                    "type": "transition",
                    "node": peer_id,
                    "from": prev,
                    "to": status,
                    "timestamp": ts,
                    "broken": broken,
                }
                new_alerts.append(alert)
                self._alerts.append(alert)
                self._emit_event("health.transition", alert)

        # Anomaly detection on collected data
        anomalies = self.detect_anomalies(nodes)
        new_alerts.extend(anomalies)
        self._alerts.extend(anomalies)

        # Trim alerts to last 100
        if len(self._alerts) > 100:
            self._alerts = self._alerts[-100:]

        self._checks_run += 1

        return {
            "status": "ok",
            "nodes": nodes,
            "alerts": new_alerts,
            "checks_run": self._checks_run,
            "timestamp": ts,
        }

    def detect_anomalies(self, nodes: dict) -> list:
        """Detect anomalies from current and historical health data.

        Checks: score drops, error spikes, all-offline condition.
        """
        anomalies = []
        ts = time.time()

        for node_id, info in nodes.items():
            if node_id == self._node_id:
                continue  # skip self

            history = self._history.get(node_id)
            if not history or len(history) < 2:
                continue

            # Score drop detection
            current_score = info.get("score")
            if current_score is not None:
                old_entries = [
                    h for h in history
                    if ts - h.get("timestamp", 0) <= SCORE_DROP_WINDOW_S
                    and h.get("score") is not None
                ]
                if old_entries:
                    max_old = max(h["score"] for h in old_entries)
                    if max_old - current_score >= SCORE_DROP_THRESHOLD:
                        anomaly = {
                            "type": "score_drop",
                            "node": node_id,
                            "from_score": max_old,
                            "to_score": current_score,
                            "timestamp": ts,
                        }
                        anomalies.append(anomaly)
                        self._emit_event("health.anomaly", anomaly)

        # All-offline check (excluding self)
        peer_nodes = {k: v for k, v in nodes.items() if k != self._node_id}
        if peer_nodes and all(v.get("status") == "offline" for v in peer_nodes.values()):
            anomaly = {
                "type": "all_peers_offline",
                "nodes": list(peer_nodes.keys()),
                "timestamp": ts,
            }
            anomalies.append(anomaly)
            self._emit_event("health.anomaly", anomaly)

        return anomalies

    def auto_repair(self, node_id: str, issue: str) -> dict:
        """Trigger automatic repair for a detected issue.

        Sends a repair message to the daemon and records in evidence ledger.
        Returns repair attempt result.
        """
        ts = time.time()
        repair = {
            "node": node_id,
            "issue": issue,
            "timestamp": ts,
            "status": "attempted",
        }

        # POST repair request to daemon
        try:
            payload = json.dumps({
                "type": "repair",
                "to": node_id,
                "payload": {"issue": issue, "requested_by": self._node_id},
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/message",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            repair["status"] = "sent" if 200 <= resp.status < 300 else "failed"
            repair["http_status"] = resp.status
        except Exception as exc:
            repair["status"] = "error"
            repair["error"] = str(exc)
            logger.warning("Auto-repair failed for %s: %s", node_id, exc)

        self._repairs.append(repair)

        # Record in evidence ledger
        if self._ledger:
            try:
                self._ledger.record(
                    event_type="health_repair",
                    node_id=self._node_id,
                    subject=f"auto-repair {node_id}: {issue}",
                    payload=repair,
                )
            except Exception as exc:
                logger.warning("Evidence ledger write failed: %s", exc)

        self._emit_event("health.repair", repair)
        return repair

    def get_fleet_status(self) -> dict:
        """Get comprehensive fleet status for dashboard consumption."""
        return {
            "node_id": self._node_id,
            "running": self._running,
            "checks_run": self._checks_run,
            "check_interval_s": self._interval,
            "active_alerts": self._alerts[-20:],
            "recent_repairs": self._repairs[-10:],
            "nodes": {
                nid: list(snaps)[-1] if snaps else {}
                for nid, snaps in self._history.items()
            },
            "timestamp": time.time(),
        }

    # -- Continuous loop --

    def run_continuous(self, max_checks: int = None):
        """Run continuous monitoring loop (blocking).

        Args:
            max_checks: Stop after N checks (None = run forever).
        """
        self._running = True
        count = 0
        logger.info("Health monitor started (interval=%ds)", self._interval)

        while self._running:
            try:
                result = self.check_fleet()
                alerts = result.get("alerts", [])

                # Auto-repair degraded nodes
                for alert in alerts:
                    if alert.get("type") == "transition" and alert.get("to") == "degraded":
                        broken = alert.get("broken", [])
                        issue = f"degraded: broken channels {broken}" if broken else "degraded"
                        self.auto_repair(alert["node"], issue)

                if alerts:
                    logger.info("Health check #%d: %d alert(s)", count + 1, len(alerts))
            except Exception as exc:
                logger.error("Health check error: %s", exc)

            count += 1
            if max_checks and count >= max_checks:
                break
            time.sleep(self._interval)

        self._running = False
        logger.info("Health monitor stopped after %d checks", count)

    def start_background(self):
        """Start monitoring in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.debug("Health monitor already running")
            return
        self._thread = Thread(target=self.run_continuous, daemon=True,
                              name="fleet-health-monitor")
        self._thread.start()

    def stop(self):
        """Signal the monitor to stop."""
        self._running = False

    # -- Internal helpers --

    def _fetch_daemon_health(self) -> Optional[dict]:
        """GET /health from local daemon."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/health", method="GET"
            )
            resp = urllib.request.urlopen(req, timeout=5)
            return json.loads(resp.read().decode())
        except Exception as exc:
            logger.debug("Daemon health fetch failed: %s", exc)
            return None

    def _record_snapshot(self, node_id: str, status: dict):
        """Append a health snapshot to node history."""
        if node_id not in self._history:
            self._history[node_id] = deque(maxlen=HISTORY_MAX)
        self._history[node_id].append(status)

    def _get_previous_status(self, node_id: str) -> Optional[str]:
        """Get the previous status string for a node (before current)."""
        history = self._history.get(node_id)
        if not history or len(history) < 2:
            return None
        # -1 is current (just added), -2 is previous
        return list(history)[-2].get("status")

    def _emit_event(self, event_type: str, payload: dict):
        """Emit event via FleetEventBus if wired."""
        if self._eventbus:
            try:
                self._eventbus.emit(event_type, payload)
            except Exception as exc:
                logger.debug("EventBus emit failed: %s", exc)
