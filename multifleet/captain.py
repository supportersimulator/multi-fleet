"""Captain Role -- manages a cluster of workers, relays to chief.

When a node is promoted to Captain:
1. Subscribes to fleet.captain.<cluster>.* NATS subjects
2. Routes worker messages to chief
3. Aggregates worker heartbeats
4. Manages local cluster health
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class WorkerHeartbeat:
    """Last-known heartbeat from a worker."""
    worker_id: str
    timestamp: float
    status: str = "unknown"
    payload: dict = field(default_factory=dict)


class CaptainRole:
    """Captain manages a cluster of workers and relays to chief."""

    def __init__(self, node_id: str, cluster_id: str, chief_id: str):
        self.node_id = node_id
        self.cluster_id = cluster_id
        self.chief_id = chief_id
        self._workers: list[str] = []
        self._heartbeats: dict[str, WorkerHeartbeat] = {}
        self._active = False
        self._activated_at: float | None = None
        self._relay_log: list[dict] = []

    # ── Lifecycle ──

    def activate(self) -> bool:
        """Activate captain role. Returns True if newly activated."""
        if self._active:
            return False
        self._active = True
        self._activated_at = time.time()
        return True

    def deactivate(self) -> bool:
        """Deactivate captain role. Returns True if was active."""
        if not self._active:
            return False
        self._active = False
        return True

    @property
    def is_active(self) -> bool:
        return self._active

    # ── Worker management ──

    def add_worker(self, worker_id: str) -> bool:
        """Add a worker to this cluster. Returns False if already present."""
        if worker_id in self._workers:
            return False
        self._workers.append(worker_id)
        return True

    def remove_worker(self, worker_id: str) -> bool:
        """Remove a worker from this cluster. Returns False if not found."""
        if worker_id not in self._workers:
            return False
        self._workers.remove(worker_id)
        self._heartbeats.pop(worker_id, None)
        return True

    @property
    def workers(self) -> list[str]:
        return list(self._workers)

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    # ── Message handling ──

    def relay_to_chief(self, message: dict) -> dict:
        """Relay a message to the chief, adding captain metadata.

        Returns the wrapped message ready for dispatch.
        """
        wrapped = {
            "type": message.get("type", "relay"),
            "from_node": self.node_id,
            "to_node": self.chief_id,
            "payload": {
                "relayed_from": message.get("from_node", "unknown"),
                "cluster_id": self.cluster_id,
                "original": message,
            },
        }
        self._relay_log.append({
            "timestamp": time.time(),
            "from": message.get("from_node", "unknown"),
            "type": message.get("type", "unknown"),
        })
        return wrapped

    def handle_worker_message(self, msg: dict) -> dict:
        """Process a message from a worker.

        - Heartbeats are stored locally
        - Other messages are wrapped for relay to chief
        """
        msg_type = msg.get("type", "")
        from_node = msg.get("from_node", "unknown")

        if msg_type == "heartbeat":
            self._heartbeats[from_node] = WorkerHeartbeat(
                worker_id=from_node,
                timestamp=time.time(),
                status=msg.get("payload", {}).get("status", "ok"),
                payload=msg.get("payload", {}),
            )
            return {"action": "stored", "worker": from_node}

        # Non-heartbeat: relay to chief
        return {"action": "relay", "message": self.relay_to_chief(msg)}

    # ── Heartbeat aggregation ──

    def aggregate_heartbeats(self) -> dict:
        """Aggregate all worker heartbeats into a summary for the chief."""
        now = time.time()
        workers_status: list[dict] = []
        stale_count = 0
        healthy_count = 0

        for wid in self._workers:
            hb = self._heartbeats.get(wid)
            if hb is None:
                workers_status.append({
                    "worker_id": wid,
                    "status": "no_heartbeat",
                    "age_s": None,
                })
                stale_count += 1
            else:
                age = round(now - hb.timestamp, 1)
                is_stale = age > 60  # >60s = stale
                workers_status.append({
                    "worker_id": wid,
                    "status": "stale" if is_stale else hb.status,
                    "age_s": age,
                })
                if is_stale:
                    stale_count += 1
                else:
                    healthy_count += 1

        return {
            "cluster_id": self.cluster_id,
            "captain": self.node_id,
            "worker_count": len(self._workers),
            "healthy": healthy_count,
            "stale": stale_count,
            "workers": workers_status,
        }

    # ── Cluster health ──

    def cluster_health(self) -> dict:
        """Return cluster health summary."""
        agg = self.aggregate_heartbeats()
        total = agg["worker_count"]
        healthy_pct = (agg["healthy"] / total * 100) if total > 0 else 0.0

        if healthy_pct >= 80:
            health = "healthy"
        elif healthy_pct >= 50:
            health = "degraded"
        else:
            health = "critical"

        return {
            "cluster_id": self.cluster_id,
            "captain": self.node_id,
            "chief": self.chief_id,
            "active": self._active,
            "activated_at": self._activated_at,
            "health": health,
            "healthy_pct": round(healthy_pct, 1),
            "worker_count": total,
            "healthy_workers": agg["healthy"],
            "stale_workers": agg["stale"],
            "relay_count": len(self._relay_log),
        }

    # ── Status / serialization ──

    def status(self) -> dict:
        """Full captain status for MCP tool."""
        return {
            "node_id": self.node_id,
            "cluster_id": self.cluster_id,
            "chief_id": self.chief_id,
            "active": self._active,
            "activated_at": self._activated_at,
            "workers": list(self._workers),
            "health": self.cluster_health(),
        }

    # ── NATS subjects ──

    def nats_subjects(self) -> list[str]:
        """Return NATS subjects this captain should subscribe to."""
        return [
            f"fleet.captain.{self.cluster_id}.*",
            f"fleet.worker.{self.cluster_id}.*.message",
        ]
