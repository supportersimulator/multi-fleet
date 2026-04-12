"""Auto Coordinator -- monitors fleet inbox and dispatches tasks automatically.

Watches .fleet-messages/ for incoming task requests, dispatches to available
nodes based on session count, tracks progress via ReviewFlowOrchestrator,
and escalates stalled tasks after configurable timeout.

Usage:
    coord = AutoCoordinator(node_id="mac1")
    coord.scan_inbox()          # Check for new tasks
    coord.tick()                # Full cycle: scan + dispatch + check stalls
    coord.status()              # Current state of all tracked tasks
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.auto-coordinator")

from ._paths import REPO_ROOT

FLEET_MSG_DIR = REPO_ROOT / ".fleet-messages"
ARCHIVE_DIR = FLEET_MSG_DIR / "archive"

# Task message frontmatter keys
_REQUIRED_KEYS = {"subject"}


@dataclass
class TrackedTask:
    """A task being tracked by the coordinator."""
    task_id: str
    description: str
    assigned_to: str  # node id
    source_file: str  # original message path
    dispatched_at: float
    status: str = "dispatched"  # dispatched | in_progress | completed | stalled | escalated
    last_check: float = 0.0
    review_task_id: str = ""  # review_flow task_id if wired

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description[:80],
            "assigned_to": self.assigned_to,
            "status": self.status,
            "dispatched_at": self.dispatched_at,
            "age_s": round(time.time() - self.dispatched_at, 1),
            "review_task_id": self.review_task_id,
        }


class AutoCoordinator:
    """Monitors fleet inbox, dispatches tasks, tracks progress, escalates stalls."""

    def __init__(
        self,
        node_id: str = "unknown",
        stall_timeout_s: float = 300.0,
        inbox_dir: Optional[Path] = None,
    ):
        self._node_id = node_id
        self._stall_timeout_s = stall_timeout_s
        self._inbox_dir = inbox_dir or (FLEET_MSG_DIR / node_id)
        self._tasks: dict[str, TrackedTask] = {}
        self._processed_files: set[str] = set()
        self._available_nodes: list[str] = ["mac1", "mac2", "mac3"]
        self._node_session_counts: dict[str, int] = {}
        self._review_flow = None  # lazy init
        self._stats = {
            "scans": 0,
            "tasks_dispatched": 0,
            "tasks_completed": 0,
            "tasks_escalated": 0,
            "stalls_detected": 0,
        }

    # -- Public API ----------------------------------------------------------

    def tick(self) -> dict:
        """Full coordination cycle: scan inbox, check progress, escalate stalls.

        Returns summary of actions taken.
        """
        actions = []

        # 1. Scan inbox for new tasks
        new_tasks = self.scan_inbox()
        if new_tasks:
            actions.append(f"found {len(new_tasks)} new task(s)")

        # 2. Dispatch unassigned tasks
        dispatched = self._dispatch_pending()
        if dispatched:
            actions.append(f"dispatched {len(dispatched)} task(s)")

        # 3. Check for stalled tasks
        stalled = self._check_stalls()
        if stalled:
            actions.append(f"escalated {len(stalled)} stalled task(s)")

        # 4. Update progress from review_flow
        updated = self._update_progress()
        if updated:
            actions.append(f"updated {updated} task(s)")

        return {
            "actions": actions,
            "active_tasks": len([t for t in self._tasks.values() if t.status not in ("completed", "escalated")]),
            "total_tasks": len(self._tasks),
            "stats": dict(self._stats),
        }

    def scan_inbox(self) -> list[TrackedTask]:
        """Scan fleet inbox for new task messages. Returns newly discovered tasks."""
        self._stats["scans"] += 1
        new_tasks = []

        if not self._inbox_dir.exists():
            return new_tasks

        # Also check the broadcast inbox
        dirs_to_scan = [self._inbox_dir]
        all_dir = FLEET_MSG_DIR / "all"
        if all_dir.exists():
            dirs_to_scan.append(all_dir)

        for scan_dir in dirs_to_scan:
            for msg_file in sorted(scan_dir.glob("*.md")):
                file_key = str(msg_file)
                if file_key in self._processed_files:
                    continue

                task = self._parse_task_message(msg_file)
                if task:
                    self._tasks[task.task_id] = task
                    new_tasks.append(task)
                    logger.info("auto-coord: new task %s from %s", task.task_id[:8], msg_file.name)

                self._processed_files.add(file_key)

        return new_tasks

    def dispatch_task(self, task_id: str, target_node: str) -> bool:
        """Manually dispatch a tracked task to a specific node."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.assigned_to = target_node
        task.status = "dispatched"
        task.dispatched_at = time.time()
        self._stats["tasks_dispatched"] += 1
        logger.info("auto-coord: dispatched %s -> %s", task_id[:8], target_node)
        return True

    def mark_complete(self, task_id: str) -> bool:
        """Mark a task as completed."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.status = "completed"
        self._stats["tasks_completed"] += 1
        return True

    def update_node_sessions(self, node_id: str, session_count: int):
        """Update known session count for a node (used for load-based dispatch)."""
        self._node_session_counts[node_id] = session_count

    def status(self) -> dict:
        """Get current coordinator state."""
        return {
            "node_id": self._node_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
            "active": len([t for t in self._tasks.values() if t.status not in ("completed", "escalated")]),
            "stats": dict(self._stats),
            "stall_timeout_s": self._stall_timeout_s,
        }

    # -- Internal ------------------------------------------------------------

    def _parse_task_message(self, path: Path) -> Optional[TrackedTask]:
        """Parse a fleet message file into a TrackedTask if it contains a task.

        Expected format: YAML frontmatter (---) with 'subject', 'from', 'type: task'
        followed by markdown body.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None

        # Parse frontmatter
        meta = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        key, val = line.split(":", 1)
                        meta[key.strip()] = val.strip()
                body = parts[2].strip()

        # Only process messages typed as 'task' or with task-like subjects
        msg_type = meta.get("type", "").lower()
        subject = meta.get("subject", "")

        if msg_type != "task" and not subject:
            return None

        # If explicitly not a task, skip
        if msg_type and msg_type not in ("task", "request", "dispatch"):
            return None

        task_id = meta.get("task_id", str(uuid.uuid4())[:8])
        description = subject or body[:200]

        return TrackedTask(
            task_id=task_id,
            description=description,
            assigned_to="",
            source_file=str(path),
            dispatched_at=0.0,
        )

    def _select_node(self) -> str:
        """Select the least-loaded available node for dispatch."""
        if not self._available_nodes:
            return self._node_id

        # Sort by session count (lower = more available), default 0
        candidates = sorted(
            self._available_nodes,
            key=lambda n: self._node_session_counts.get(n, 0),
        )
        return candidates[0]

    def _dispatch_pending(self) -> list[str]:
        """Dispatch all un-assigned tasks. Returns list of dispatched task_ids."""
        dispatched = []
        for task in self._tasks.values():
            if task.status == "dispatched" and not task.assigned_to:
                # Not yet assigned -- pick a node
                target = self._select_node()
                task.assigned_to = target
                task.dispatched_at = time.time()
                task.status = "dispatched"
                self._stats["tasks_dispatched"] += 1
                dispatched.append(task.task_id)

                # Wire to review_flow if available
                self._wire_review_flow(task)

                logger.info("auto-coord: auto-dispatched %s -> %s", task.task_id[:8], target)
        return dispatched

    def _wire_review_flow(self, task: TrackedTask):
        """Wire a task into the ReviewFlowOrchestrator for tracking."""
        try:
            if self._review_flow is None:
                from multifleet.review_flow import ReviewFlowOrchestrator
                self._review_flow = ReviewFlowOrchestrator(node_id=self._node_id)

            session = self._review_flow.start_review(
                task_id=task.task_id,
                assigned_machines=[task.assigned_to],
                task_summary=task.description,
            )
            task.review_task_id = session.task_id
        except Exception as e:
            logger.debug("auto-coord: review_flow wiring failed: %s", e)

    def _check_stalls(self) -> list[str]:
        """Check for stalled tasks and escalate them. Returns escalated task_ids."""
        now = time.time()
        escalated = []
        for task in self._tasks.values():
            if task.status in ("completed", "escalated"):
                continue
            if task.dispatched_at <= 0:
                continue
            age = now - task.dispatched_at
            if age > self._stall_timeout_s:
                task.status = "escalated"
                self._stats["stalls_detected"] += 1
                self._stats["tasks_escalated"] += 1
                escalated.append(task.task_id)
                logger.warning(
                    "auto-coord: task %s stalled (%.0fs > %.0fs), escalating",
                    task.task_id[:8], age, self._stall_timeout_s,
                )
        return escalated

    def _update_progress(self) -> int:
        """Check review_flow for progress updates. Returns count of updated tasks."""
        if self._review_flow is None:
            return 0

        updated = 0
        for task in self._tasks.values():
            if task.status in ("completed", "escalated"):
                continue
            if not task.review_task_id:
                continue

            try:
                rf_status = self._review_flow.get_status(task.review_task_id)
                phase = rf_status.get("phase", "")
                if phase == "resolved":
                    task.status = "completed"
                    self._stats["tasks_completed"] += 1
                    updated += 1
                elif phase not in ("", "unknown"):
                    task.status = "in_progress"
                    task.last_check = time.time()
                    updated += 1
            except Exception:
                pass

        return updated
