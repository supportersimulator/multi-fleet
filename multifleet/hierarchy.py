"""Fleet hierarchy — Chief / Captain / Worker role management.

Enables scaling from 3 nodes to 100+ via hierarchical routing.
Each node has a role that determines its responsibilities and
communication patterns.
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional


class NodeRole(Enum):
    CHIEF = "chief"       # Synthesis, memory, adjudication
    CAPTAIN = "captain"   # Cluster manager, relay to chief
    WORKER = "worker"     # Local surgeon cell


@dataclass
class NodeInfo:
    node_id: str
    role: NodeRole
    captain_id: Optional[str] = None   # Who is my captain? (workers only)
    cluster_id: Optional[str] = None   # Which cluster am I in?
    workers: list[str] = field(default_factory=list)   # My workers (captains only)
    capabilities: list[str] = field(default_factory=list)  # What can this node do?

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "captain_id": self.captain_id,
            "cluster_id": self.cluster_id,
            "workers": list(self.workers),
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_dict(cls, data: dict) -> NodeInfo:
        return cls(
            node_id=data["node_id"],
            role=NodeRole(data["role"]),
            captain_id=data.get("captain_id"),
            cluster_id=data.get("cluster_id"),
            workers=list(data.get("workers", [])),
            capabilities=list(data.get("capabilities", [])),
        )


class FleetHierarchy:
    """Manages fleet node roles and routing hierarchy."""

    def __init__(self, config: dict | None = None):
        self._nodes: dict[str, NodeInfo] = {}
        self._chief_id: Optional[str] = None
        if config:
            self._load_config(config)

    # ── Registration ──

    def register_node(
        self,
        node_id: str,
        role: NodeRole,
        captain_id: str | None = None,
        cluster_id: str | None = None,
        capabilities: list[str] | None = None,
    ) -> NodeInfo:
        """Register a node in the hierarchy. Returns the created NodeInfo."""
        node = NodeInfo(
            node_id=node_id,
            role=role,
            captain_id=captain_id,
            cluster_id=cluster_id,
            capabilities=list(capabilities or []),
        )

        if role == NodeRole.CHIEF:
            if self._chief_id is not None and self._chief_id != node_id:
                raise ValueError(
                    f"Chief already registered: {self._chief_id}. "
                    "Demote existing chief first."
                )
            self._chief_id = node_id

        if role == NodeRole.WORKER and captain_id:
            captain = self._nodes.get(captain_id)
            if captain and captain.role == NodeRole.CAPTAIN:
                if node_id not in captain.workers:
                    captain.workers.append(node_id)
                node.cluster_id = node.cluster_id or captain.cluster_id

        if role == NodeRole.CAPTAIN and cluster_id is None:
            node.cluster_id = node_id  # Default cluster = captain's own id

        self._nodes[node_id] = node
        return node

    # ── Lookups ──

    def get_node(self, node_id: str) -> Optional[NodeInfo]:
        return self._nodes.get(node_id)

    def get_chief(self) -> Optional[NodeInfo]:
        if self._chief_id:
            return self._nodes.get(self._chief_id)
        return None

    def get_captain_for(self, worker_id: str) -> Optional[NodeInfo]:
        node = self._nodes.get(worker_id)
        if node and node.captain_id:
            return self._nodes.get(node.captain_id)
        return None

    def get_workers_for(self, captain_id: str) -> list[NodeInfo]:
        captain = self._nodes.get(captain_id)
        if not captain or captain.role != NodeRole.CAPTAIN:
            return []
        return [
            self._nodes[wid]
            for wid in captain.workers
            if wid in self._nodes
        ]

    @property
    def all_nodes(self) -> dict[str, NodeInfo]:
        return dict(self._nodes)

    # ── Routing ──

    def route_message(self, from_id: str, to_id: str) -> list[str]:
        """Determine routing path from from_id to to_id.

        Rules:
        - Same node: [from_id]
        - Same cluster workers: worker -> captain -> worker
        - Worker to chief: worker -> captain -> chief
        - Captain to chief: captain -> chief
        - Chief to worker: chief -> captain -> worker
        - Cross-cluster: from -> (captain_from) -> chief -> (captain_to) -> to
        """
        if from_id == to_id:
            return [from_id]

        src = self._nodes.get(from_id)
        dst = self._nodes.get(to_id)
        if not src or not dst:
            return [from_id, to_id]  # best-effort direct

        # Chief is always direct endpoint
        if src.role == NodeRole.CHIEF:
            return self._route_from_chief(from_id, dst)
        if dst.role == NodeRole.CHIEF:
            return self._route_to_chief(src, to_id)

        # Captain <-> Captain goes through chief
        if src.role == NodeRole.CAPTAIN and dst.role == NodeRole.CAPTAIN:
            if self._chief_id:
                return [from_id, self._chief_id, to_id]
            return [from_id, to_id]

        # Captain to own worker: direct
        if src.role == NodeRole.CAPTAIN and dst.role == NodeRole.WORKER:
            if dst.captain_id == from_id:
                return [from_id, to_id]
            # Cross-cluster: captain -> chief -> other_captain -> worker
            if self._chief_id and dst.captain_id:
                return [from_id, self._chief_id, dst.captain_id, to_id]
            return [from_id, to_id]

        # Worker to captain
        if src.role == NodeRole.WORKER and dst.role == NodeRole.CAPTAIN:
            if src.captain_id == to_id:
                return [from_id, to_id]
            # Worker -> own captain -> chief -> target captain
            path = [from_id]
            if src.captain_id:
                path.append(src.captain_id)
            if self._chief_id:
                path.append(self._chief_id)
            path.append(to_id)
            return path

        # Worker to worker
        if src.role == NodeRole.WORKER and dst.role == NodeRole.WORKER:
            # Same cluster: worker -> captain -> worker
            if src.cluster_id and src.cluster_id == dst.cluster_id and src.captain_id:
                return [from_id, src.captain_id, to_id]
            # Cross-cluster
            path = [from_id]
            if src.captain_id:
                path.append(src.captain_id)
            if self._chief_id:
                path.append(self._chief_id)
            if dst.captain_id and dst.captain_id != self._chief_id:
                path.append(dst.captain_id)
            path.append(to_id)
            return path

        return [from_id, to_id]

    def _route_from_chief(self, chief_id: str, dst: NodeInfo) -> list[str]:
        if dst.role == NodeRole.CAPTAIN:
            return [chief_id, dst.node_id]
        # Chief -> captain -> worker
        if dst.captain_id:
            return [chief_id, dst.captain_id, dst.node_id]
        return [chief_id, dst.node_id]

    def _route_to_chief(self, src: NodeInfo, chief_id: str) -> list[str]:
        if src.role == NodeRole.CAPTAIN:
            return [src.node_id, chief_id]
        # Worker -> captain -> chief
        if src.captain_id:
            return [src.node_id, src.captain_id, chief_id]
        return [src.node_id, chief_id]

    # ── NATS Subject Partitioning ──

    def nats_subject(self, node_id: str, msg_type: str = "message") -> str:
        """Generate NATS subject for a node based on its role and cluster."""
        node = self._nodes.get(node_id)
        if not node:
            return f"fleet.unknown.{node_id}.{msg_type}"
        if node.role == NodeRole.CHIEF:
            return f"fleet.chief.{msg_type}"
        if node.role == NodeRole.CAPTAIN:
            return f"fleet.captain.{node.cluster_id}.{msg_type}"
        return f"fleet.worker.{node.cluster_id}.{node_id}.{msg_type}"

    def nats_subscribe_subjects(self, node_id: str) -> list[str]:
        """Return NATS subjects a node should subscribe to."""
        node = self._nodes.get(node_id)
        if not node:
            return []

        subjects = [self.nats_subject(node_id)]  # Own subject

        if node.role == NodeRole.CHIEF:
            # Chief also listens to all captain subjects
            for n in self._nodes.values():
                if n.role == NodeRole.CAPTAIN:
                    subjects.append(
                        f"fleet.captain.{n.cluster_id}.message"
                    )
        elif node.role == NodeRole.CAPTAIN:
            # Captain listens to its cluster's worker subjects
            subjects.append(f"fleet.worker.{node.cluster_id}.*.message")

        return subjects

    # ── Promotion / Election ──

    def promote_to_captain(
        self, node_id: str, cluster_id: str | None = None
    ) -> bool:
        """Promote a worker to captain. Returns True on success."""
        node = self._nodes.get(node_id)
        if not node:
            return False
        if node.role == NodeRole.CHIEF:
            return False  # Can't demote chief to captain this way

        # Remove from old captain's worker list
        if node.captain_id:
            old_captain = self._nodes.get(node.captain_id)
            if old_captain and node_id in old_captain.workers:
                old_captain.workers.remove(node_id)

        node.role = NodeRole.CAPTAIN
        node.cluster_id = cluster_id or node_id
        node.captain_id = None
        node.workers = []
        return True

    def elect_chief(self, candidates: list[str]) -> str:
        """Simple chief election — most capabilities wins, tie-break by node_id sort.

        Returns the elected chief's node_id.
        Raises ValueError if no valid candidates.
        """
        valid = [
            (nid, self._nodes[nid])
            for nid in candidates
            if nid in self._nodes
        ]
        if not valid:
            raise ValueError("No valid candidates for chief election")

        # Sort: most capabilities (desc), then node_id (asc) for tie-break
        valid.sort(key=lambda x: (-len(x[1].capabilities), x[0]))
        winner_id = valid[0][0]

        # Demote old chief if different
        if self._chief_id and self._chief_id != winner_id:
            old_chief = self._nodes.get(self._chief_id)
            if old_chief:
                old_chief.role = NodeRole.WORKER

        # Promote winner
        winner = self._nodes[winner_id]
        winner.role = NodeRole.CHIEF
        winner.captain_id = None
        self._chief_id = winner_id
        return winner_id

    # ── Serialization ──

    def to_dict(self) -> dict:
        return {
            "chief_id": self._chief_id,
            "nodes": {
                nid: node.to_dict() for nid, node in self._nodes.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> FleetHierarchy:
        h = cls()
        h._chief_id = data.get("chief_id")
        for nid, ndata in data.get("nodes", {}).items():
            h._nodes[nid] = NodeInfo.from_dict(ndata)
        return h

    # ── Config Loading ──

    def _load_config(self, config: dict) -> None:
        """Load hierarchy from a config dict (e.g., from config.json)."""
        nodes = config.get("nodes", {})
        for nid, ncfg in nodes.items():
            role_str = ncfg.get("role", "worker")
            try:
                role = NodeRole(role_str)
            except ValueError:
                role = NodeRole.WORKER

            caps = ncfg.get("capabilities", [])
            cluster = ncfg.get("region", ncfg.get("cluster_id"))
            captain = ncfg.get("captain_id")

            self.register_node(
                node_id=nid,
                role=role,
                captain_id=captain,
                cluster_id=cluster,
                capabilities=caps,
            )
