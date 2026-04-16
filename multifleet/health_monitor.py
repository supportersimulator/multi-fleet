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

    Integrates ChannelHealthInvariant for continuous channel health enforcement:
    broken channels auto-trigger repair with L1-L4 escalation.
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

        # Channel health invariance — auto-repair broken channels
        from multifleet.invariance_rules import ChannelHealthInvariant
        self._channel_invariant = ChannelHealthInvariant()
        # Track previous channel state per peer for transition detection
        self._prev_channel_state: dict[str, str] = {}  # peer -> "healthy"|"broken"
        # Track previous peer reachability for partition heal detection
        self._prev_peer_status: dict[str, str] = {}  # peer -> "offline"|"degraded"|"healthy"
        self._lease_manager = None  # set via wire_lease_manager()

    # -- Wiring --

    def wire_eventbus(self, eventbus) -> None:
        """Inject FleetEventBus for event emission."""
        self._eventbus = eventbus

    def wire_ledger(self, ledger) -> None:
        """Inject EvidenceLedger for tamper-evident recording."""
        self._ledger = ledger

    def wire_lease_manager(self, lease_manager) -> None:
        """Inject LeaseManager for automatic partition-heal reconciliation."""
        self._lease_manager = lease_manager

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

                # Feed transition into memory learning pipeline
                try:
                    from multifleet.memory_feedback import get_feedback
                    get_feedback().record_health_transition(
                        node=peer_id, old_status=prev,
                        new_status=status, broken_channels=broken,
                    )
                except Exception:
                    pass  # never block health monitor

            # Detect partition heal: peer was offline, now reachable
            prev_peer = self._prev_peer_status.get(peer_id)
            if prev_peer == "offline" and status in ("healthy", "degraded"):
                self._handle_partition_heal(peer_id, ts, new_alerts)
            self._prev_peer_status[peer_id] = status

        # Channel health invariance check — auto-repair broken channels
        if channel_health:
            violations = self._channel_invariant.check(channel_health)
            for v in violations:
                peer_id = v["peer"]
                # Emit fleet.channel.broken event
                self._emit_event("channel.broken", {
                    "peer": peer_id,
                    "score": v["score"],
                    "broken": v.get("broken", []),
                    "severity": v["severity"],
                    "escalation_level": v.get("escalation_level", 1),
                })
                # Track state transition
                prev = self._prev_channel_state.get(peer_id, "healthy")
                if prev != "broken":
                    self._prev_channel_state[peer_id] = "broken"

                # Emit fleet.channel.healing and trigger repair
                self._emit_event("channel.healing", {
                    "peer": peer_id,
                    "score": v["score"],
                    "action": "repair",
                    "escalation_level": v.get("escalation_level", 1),
                })
                repair_result = self._channel_invariant.auto_repair(v, health_monitor=self)
                new_alerts.append({
                    "type": "channel_invariance",
                    "peer": peer_id,
                    "score": v["score"],
                    "severity": v["severity"],
                    "repair_status": repair_result.get("status", "unknown"),
                    "timestamp": ts,
                })

            # Check for restored channels (was broken, now healthy)
            for peer_id in list(self._prev_channel_state.keys()):
                if self._prev_channel_state[peer_id] == "broken":
                    peer_ch = channel_health.get(peer_id, {})
                    if isinstance(peer_ch, dict):
                        score_str = peer_ch.get("score", "0/7")
                    else:
                        score_str = str(peer_ch)
                    try:
                        parts = score_str.split("/")
                        up, total = int(parts[0]), int(parts[1])
                    except (ValueError, IndexError):
                        continue
                    if total > 0 and up >= total // 2:
                        self._prev_channel_state[peer_id] = "healthy"
                        self._channel_invariant.clear_escalation(peer_id)
                        self._emit_event("channel.restored", {
                            "peer": peer_id,
                            "score": score_str,
                        })

        # Anomaly detection on collected data
        anomalies = self.detect_anomalies(nodes)
        new_alerts.extend(anomalies)
        self._alerts.extend(anomalies)

        # Trim alerts to last 100
        if len(self._alerts) > 100:
            self._alerts = self._alerts[-100:]

        self._checks_run += 1

        # Push fleet health to admin panel via product bridge (MF→AP)
        self._route_health_to_admin(nodes, new_alerts, ts)

        return {
            "status": "ok",
            "nodes": nodes,
            "alerts": new_alerts,
            "checks_run": self._checks_run,
            "timestamp": ts,
        }

    def _route_health_to_admin(self, nodes: dict, alerts: list, ts: float) -> None:
        """Push fleet health summary to admin panel via ProductBridge (MF→AP).

        Best-effort, never blocks the health check loop.
        """
        try:
            from multifleet.product_bridge import get_bridge
            bridge = get_bridge()
            bridge.route("multi-fleet", "admin-panel", {
                "event_type": "fleet.health.check",
                "node_id": self._node_id,
                "data": {
                    "nodes": {nid: info for nid, info in nodes.items()},
                    "alert_count": len(alerts),
                    "checks_run": self._checks_run,
                    "timestamp": ts,
                },
            })
        except Exception as e:
            logger.debug("health->admin route (non-blocking): %s", e)

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

    # Map ad-hoc string event types to typed EventType enum values
    _EVENT_TYPE_MAP: dict = {}  # populated lazily to avoid circular imports

    @classmethod
    def _get_event_type_map(cls):
        if not cls._EVENT_TYPE_MAP:
            from multifleet.event_schema import EventType as ET
            cls._EVENT_TYPE_MAP = {
                "health.transition": ET.HEALTH_TRANSITION,
                "health.anomaly": ET.HEALTH_ANOMALY,
                "health.repair": ET.HEALTH_REPAIR,
                "channel.broken": ET.HEALTH_CHANNEL_BROKEN,
                "channel.healing": ET.HEALTH_CHANNEL_HEALING,
                "channel.restored": ET.HEALTH_CHANNEL_RESTORED,
                "health.partition.healed": ET.HEALTH_PARTITION_HEALED,
            }
        return cls._EVENT_TYPE_MAP

    def _handle_partition_heal(self, peer_id: str, ts: float, alerts: list):
        """Trigger lease reconciliation when a previously-offline peer becomes reachable.

        Collects provisional leases for the healed peer and calls
        LeaseManager.reconcile(). Emits fleet.partition.healed event.
        Reconciliation failures are logged but never crash the monitor.
        """
        if not self._lease_manager:
            logger.debug("Partition heal detected for %s but no LeaseManager wired", peer_id)
            return

        heal_event = {
            "peer": peer_id,
            "timestamp": ts,
            "provisional_count": 0,
            "verdicts": [],
        }

        try:
            provisionals = self._lease_manager.list_provisional(node_id=peer_id)
            heal_event["provisional_count"] = len(provisionals)

            if provisionals:
                verdicts = self._lease_manager.reconcile(provisionals)
                heal_event["verdicts"] = verdicts
                logger.info(
                    "Partition heal: reconciled %d provisional leases for %s",
                    len(provisionals), peer_id,
                )
            else:
                logger.info("Partition heal: no provisional leases for %s", peer_id)
        except Exception as exc:
            logger.error("Partition heal reconciliation failed for %s: %s", peer_id, exc)
            heal_event["error"] = str(exc)

        alerts.append({
            "type": "partition_healed",
            "peer": peer_id,
            "provisional_count": heal_event["provisional_count"],
            "timestamp": ts,
        })
        self._emit_event("health.partition.healed", heal_event)

    def _emit_event(self, event_type: str, payload: dict):
        """Emit event via typed EventEmitter (preferred) with FleetEventBus fallback."""
        # Try typed emit first
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventSource
            type_map = self._get_event_type_map()
            et = type_map.get(event_type)
            if et is not None:
                emit_typed(et, EventSource.MULTI_FLEET, payload=payload)
                return
        except (ImportError, Exception):
            pass
        # Fallback to old EventBus path
        if self._eventbus:
            try:
                self._eventbus.emit(event_type, payload)
            except Exception as exc:
                logger.debug("EventBus emit failed: %s", exc)
