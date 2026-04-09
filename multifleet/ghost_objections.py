"""Ghost Objections — stores and retrieves unresolved objections across sessions.

Tracks surgeon reservations, probe red flags, unproven claims, and dissent
that must be carried forward until resolved or explicitly dismissed.

"Unresolved objections are the system's conscience — they prevent premature
convergence and keep the fleet open to correction."
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-objections")


class ObjectionStatus(Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    CARRIED = "carried"  # explicitly carried forward to next session


class ObjectionSource(Enum):
    SURGEON = "surgeon"
    PROBE = "probe"
    PULSE = "pulse"
    MANUAL = "manual"
    DIGEST = "digest"


@dataclass
class Objection:
    """A single unresolved objection."""

    objection_id: str
    source: str  # ObjectionSource value
    source_node: str
    task_id: str
    claim: str
    evidence: list[str] = field(default_factory=list)
    status: str = "open"  # ObjectionStatus value
    created_at: str = ""
    resolved_at: str = ""
    resolution: str = ""
    carry_count: int = 0  # how many sessions this has been carried

    def summary(self) -> str:
        prefix = f"[{self.source}:{self.source_node}]" if self.source_node else f"[{self.source}]"
        return f"{prefix} {self.claim[:120]}"

    def to_dict(self) -> dict:
        return {
            "objection_id": self.objection_id,
            "source": self.source,
            "source_node": self.source_node,
            "task_id": self.task_id,
            "claim": self.claim,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
            "carry_count": self.carry_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Objection":
        return cls(
            objection_id=d.get("objection_id", ""),
            source=d.get("source", "manual"),
            source_node=d.get("source_node", ""),
            task_id=d.get("task_id", ""),
            claim=d.get("claim", ""),
            evidence=d.get("evidence", []),
            status=d.get("status", "open"),
            created_at=d.get("created_at", ""),
            resolved_at=d.get("resolved_at", ""),
            resolution=d.get("resolution", ""),
            carry_count=d.get("carry_count", 0),
        )


class ObjectionStore:
    """Persistent store for unresolved objections.

    Backed by a single JSONL file for append-friendly durability.
    State changes (resolve, dismiss, carry) append new entries — the latest
    entry for a given objection_id wins.
    """

    def __init__(self, store_path: Optional[str] = None):
        self._path = Path(
            store_path
            or str(Path.home() / ".fleet-nerve" / "ghost" / "objections.jsonl")
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        claim: str,
        source: str = "manual",
        source_node: str = "",
        task_id: str = "",
        evidence: Optional[list[str]] = None,
    ) -> Objection:
        """Record a new objection."""
        obj = Objection(
            objection_id=str(uuid.uuid4())[:12],
            source=source,
            source_node=source_node,
            task_id=task_id,
            claim=claim,
            evidence=evidence or [],
            status="open",
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._append(obj)
        logger.info("Objection recorded: %s", obj.summary())
        return obj

    def resolve(self, objection_id: str, resolution: str) -> Optional[Objection]:
        """Mark an objection as resolved with explanation."""
        obj = self._get_latest(objection_id)
        if not obj:
            return None
        obj.status = "resolved"
        obj.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        obj.resolution = resolution
        self._append(obj)
        logger.info("Objection resolved: %s — %s", objection_id, resolution[:80])
        return obj

    def dismiss(self, objection_id: str, reason: str) -> Optional[Objection]:
        """Dismiss an objection (with reason — dismissal without reason is forbidden)."""
        if not reason:
            logger.warning("Cannot dismiss objection without reason")
            return None
        obj = self._get_latest(objection_id)
        if not obj:
            return None
        obj.status = "dismissed"
        obj.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        obj.resolution = f"DISMISSED: {reason}"
        self._append(obj)
        return obj

    def carry_forward(self) -> list[Objection]:
        """Mark all open objections as carried forward (call at session end).

        Returns the list of carried objections.
        """
        carried = []
        for obj in self.list_open():
            obj.status = "carried"
            obj.carry_count += 1
            self._append(obj)
            carried.append(obj)
        if carried:
            logger.info("Carried forward %d objections to next session", len(carried))
        return carried

    def list_open(self) -> list[Objection]:
        """List all open or carried objections (not resolved/dismissed)."""
        all_objs = self._load_latest_states()
        return [o for o in all_objs if o.status in ("open", "carried")]

    def list_all(self) -> list[Objection]:
        """List all objections with latest state."""
        return self._load_latest_states()

    def stats(self) -> dict:
        """Summary stats."""
        all_objs = self._load_latest_states()
        by_status: dict[str, int] = {}
        for o in all_objs:
            by_status[o.status] = by_status.get(o.status, 0) + 1
        return {
            "total": len(all_objs),
            "by_status": by_status,
            "oldest_open": min(
                (o.created_at for o in all_objs if o.status in ("open", "carried")),
                default="",
            ),
        }

    def _append(self, obj: Objection) -> None:
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(obj.to_dict()) + "\n")
        except Exception as e:
            logger.error("Failed to persist objection: %s", e)

    def _load_latest_states(self) -> list[Objection]:
        """Load all entries, return latest state per objection_id."""
        if not self._path.exists():
            return []
        latest: dict[str, Objection] = {}
        try:
            for line in self._path.read_text().strip().splitlines():
                try:
                    d = json.loads(line)
                    obj = Objection.from_dict(d)
                    latest[obj.objection_id] = obj
                except (json.JSONDecodeError, Exception):
                    continue
        except Exception:
            pass
        return list(latest.values())

    def _get_latest(self, objection_id: str) -> Optional[Objection]:
        for obj in self._load_latest_states():
            if obj.objection_id == objection_id:
                return obj
        return None
