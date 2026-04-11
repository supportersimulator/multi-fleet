"""Ghost Contradictions — detects and clusters conflicting truths across evidence.

When multiple nodes or sessions produce conflicting decisions about the same
topic, contradictions must be surfaced rather than silently overwritten.

"The value is in the disagreements, not the agreements."
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-contradictions")


@dataclass
class Contradiction:
    """Two or more evidence items that conflict."""

    contradiction_id: str
    topic: str
    claims: list[dict] = field(default_factory=list)
    # Each claim: {"node": str, "task_id": str, "claim": str, "timestamp": str, "evidence_ref": str}
    severity: str = "medium"  # low, medium, high
    detected_at: str = ""
    resolved: bool = False
    resolution: str = ""

    def to_dict(self) -> dict:
        return {
            "contradiction_id": self.contradiction_id,
            "topic": self.topic,
            "claims": self.claims,
            "severity": self.severity,
            "detected_at": self.detected_at,
            "resolved": self.resolved,
            "resolution": self.resolution,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Contradiction":
        return cls(
            contradiction_id=d.get("contradiction_id", ""),
            topic=d.get("topic", ""),
            claims=d.get("claims", []),
            severity=d.get("severity", "medium"),
            detected_at=d.get("detected_at", ""),
            resolved=d.get("resolved", False),
            resolution=d.get("resolution", ""),
        )


class ContradictionDetector:
    """Detects and stores contradiction clusters from evidence ledger entries.

    Scans for same task_id with conflicting decisions, same topic with
    opposing claims from different nodes, and repeated flip-flops.
    """

    def __init__(self, store_path: Optional[str] = None, ledger_path: Optional[str] = None):
        self._store_path = Path(
            store_path
            or str(Path.home() / ".fleet-nerve" / "ghost" / "contradictions.json")
        )
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger_path = Path(
            ledger_path
            or str(Path.home() / ".fleet-nerve" / "decisions.jsonl")
        )

    def scan(self) -> list[Contradiction]:
        """Scan evidence ledger for contradictions. Returns newly found ones."""
        existing = self._load()
        existing_topics = {c.topic for c in existing if not c.resolved}

        new_contradictions = []

        # 1. Scan decisions ledger for task_id conflicts
        task_conflicts = self._scan_task_conflicts()
        for topic, claims in task_conflicts.items():
            if topic not in existing_topics:
                c = Contradiction(
                    contradiction_id=f"task-{hash(topic) % 100000:05d}",
                    topic=topic,
                    claims=claims,
                    severity=self._assess_severity(claims),
                    detected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                new_contradictions.append(c)

        # 2. Scan evidence candidates for cross-node disagreements
        node_conflicts = self._scan_cross_node_conflicts()
        for topic, claims in node_conflicts.items():
            if topic not in existing_topics:
                c = Contradiction(
                    contradiction_id=f"node-{hash(topic) % 100000:05d}",
                    topic=topic,
                    claims=claims,
                    severity=self._assess_severity(claims),
                    detected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                new_contradictions.append(c)

        # Persist
        if new_contradictions:
            self._save(existing + new_contradictions)
            logger.info("Found %d new contradictions", len(new_contradictions))

        return new_contradictions

    def list_unresolved(self) -> list[Contradiction]:
        """List all unresolved contradictions."""
        return [c for c in self._load() if not c.resolved]

    def resolve(self, contradiction_id: str, resolution: str) -> bool:
        """Mark a contradiction as resolved."""
        all_c = self._load()
        for c in all_c:
            if c.contradiction_id == contradiction_id:
                c.resolved = True
                c.resolution = resolution
                self._save(all_c)
                logger.info("Resolved contradiction: %s", contradiction_id)
                return True
        return False

    def stats(self) -> dict:
        all_c = self._load()
        return {
            "total": len(all_c),
            "unresolved": sum(1 for c in all_c if not c.resolved),
            "by_severity": {
                s: sum(1 for c in all_c if c.severity == s and not c.resolved)
                for s in ("low", "medium", "high")
            },
        }

    def _scan_task_conflicts(self) -> dict[str, list[dict]]:
        """Find tasks with conflicting decisions."""
        if not self._ledger_path.exists():
            return {}

        by_task: dict[str, list[dict]] = {}
        try:
            for line in self._ledger_path.read_text().strip().splitlines()[-200:]:
                try:
                    entry = json.loads(line)
                    task_id = entry.get("task_id", "")
                    if task_id:
                        by_task.setdefault(task_id, []).append({
                            "node": entry.get("node_id", "?"),
                            "task_id": task_id,
                            "claim": entry.get("decision", ""),
                            "timestamp": entry.get("timestamp", ""),
                            "evidence_ref": f"ledger:{task_id}",
                        })
                except json.JSONDecodeError:
                    continue
        except Exception:
            return {}

        # Only keep tasks with genuinely conflicting decisions
        conflicts = {}
        for task_id, entries in by_task.items():
            decisions = set(e["claim"] for e in entries if e["claim"])
            if len(decisions) > 1:
                conflicts[f"task:{task_id[:20]}"] = entries
        return conflicts

    def _scan_cross_node_conflicts(self) -> dict[str, list[dict]]:
        """Find evidence candidates where different nodes disagree."""
        evidence_dir = Path.home() / ".fleet-nerve" / "evidence-candidates"
        if not evidence_dir.exists():
            return {}

        by_title: dict[str, list[dict]] = {}
        try:
            for f in sorted(evidence_dir.glob("*.json"))[-50:]:
                try:
                    data = json.loads(f.read_text())
                    for candidate in data.get("evidence_candidates", []):
                        title = candidate.get("title", "")[:60]
                        if title:
                            by_title.setdefault(title, []).append({
                                "node": candidate.get("source_node", "?"),
                                "task_id": candidate.get("source_task_id", ""),
                                "claim": candidate.get("content", "")[:200],
                                "timestamp": candidate.get("created_at", ""),
                                "evidence_ref": candidate.get("candidate_id", ""),
                            })
                except (json.JSONDecodeError, Exception):
                    continue
        except Exception:
            return {}

        # Only keep where different nodes produced evidence on same topic
        conflicts = {}
        for title, entries in by_title.items():
            nodes = set(e["node"] for e in entries)
            if len(nodes) > 1:
                conflicts[f"evidence:{title}"] = entries
        return conflicts

    def _assess_severity(self, claims: list[dict]) -> str:
        """Assess severity based on number of conflicting claims and nodes."""
        nodes = set(c.get("node", "") for c in claims)
        if len(nodes) >= 3:
            return "high"
        if len(claims) >= 4:
            return "high"
        if len(nodes) >= 2:
            return "medium"
        return "low"

    def _load(self) -> list[Contradiction]:
        if not self._store_path.exists():
            return []
        try:
            data = json.loads(self._store_path.read_text())
            return [Contradiction.from_dict(c) for c in data]
        except (json.JSONDecodeError, Exception):
            return []

    def _save(self, contradictions: list[Contradiction]) -> None:
        try:
            self._store_path.write_text(
                json.dumps([c.to_dict() for c in contradictions], indent=2)
            )
        except Exception as e:
            logger.error("Failed to save contradictions: %s", e)
