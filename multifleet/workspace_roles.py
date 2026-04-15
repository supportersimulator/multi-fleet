"""Workspace Roles — approach-based differentiation per machine.

NOT role-based specialization (all machines run the same surgeon protocol).
Instead, each machine gets a different APPROACH to the same task:

- Same hospital, same surgical protocol, different operating rooms, same patient.

Approach strategies:
- "straightforward"  — most direct implementation path
- "alternative"      — cleaner abstraction or different approach
- "hardening"        — robustness, edge-cases, simplification
- Custom             — any user-defined approach

The workspace role is NOT about the machine's identity — it's about what
implementation DIRECTION it explores for the current task. Roles can
change per task. The machine itself remains independently capable.

Design from Multi-Fleet architecture chat (2026-04):
"Keep every machine internally identical in role structure.
Vary only: IDE, branch/worktree, implementation direction."
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.workspace-roles")


class ApproachStrategy(Enum):
    """Pre-defined approach strategies for workspace differentiation."""
    STRAIGHTFORWARD = "straightforward"
    ALTERNATIVE = "alternative"
    HARDENING = "hardening"
    EXPLORATORY = "exploratory"
    MINIMAL = "minimal"
    CUSTOM = "custom"


@dataclass
class WorkspaceRole:
    """Describes what a machine/workspace is doing for the current task.

    This is ephemeral — it changes per task assignment. The machine itself
    retains the same surgeon protocol regardless of workspace role.
    """
    machine_id: str
    approach: ApproachStrategy
    custom_approach: str = ""   # Only used when approach == CUSTOM
    task_id: str = ""
    branch: str = ""
    worktree: str = ""
    ide: str = ""               # vscode-claude, codex, antigravity, etc.
    description: str = ""       # Human-readable description of what this workspace explores
    assigned_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "approach": self.approach.value,
            "custom_approach": self.custom_approach,
            "task_id": self.task_id,
            "branch": self.branch,
            "worktree": self.worktree,
            "ide": self.ide,
            "description": self.description,
            "assigned_at": self.assigned_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkspaceRole:
        approach_str = data.get("approach", "straightforward")
        try:
            approach = ApproachStrategy(approach_str)
        except ValueError:
            approach = ApproachStrategy.CUSTOM
        return cls(
            machine_id=data.get("machine_id", ""),
            approach=approach,
            custom_approach=data.get("custom_approach", approach_str),
            task_id=data.get("task_id", ""),
            branch=data.get("branch", ""),
            worktree=data.get("worktree", ""),
            ide=data.get("ide", ""),
            description=data.get("description", ""),
            assigned_at=data.get("assigned_at", time.time()),
        )

    @property
    def approach_label(self) -> str:
        if self.approach == ApproachStrategy.CUSTOM:
            return self.custom_approach or "custom"
        return self.approach.value


# ── Workspace Registry ──────────────────────────────────────────────────────


class WorkspaceRegistry:
    """Manages workspace role assignments across the fleet.

    Each task assigns different approaches to different machines.
    The registry tracks current assignments and supports history.

    Persistence: Optional JSON file at .multifleet/workspace-roles.json.
    Without persistence, assignments are memory-only (suitable for tests).
    """

    def __init__(self, persist_path: Optional[str | Path] = None):
        self._roles: dict[str, WorkspaceRole] = {}  # machine_id -> current role
        self._history: list[dict] = []               # Past assignments
        self._persist_path = Path(persist_path) if persist_path else None

        if self._persist_path and self._persist_path.exists():
            self._load()

    def assign(
        self,
        machine_id: str,
        approach: str | ApproachStrategy,
        task_id: str = "",
        branch: str = "",
        ide: str = "",
        description: str = "",
    ) -> WorkspaceRole:
        """Assign a workspace role to a machine for a task."""
        if isinstance(approach, str):
            try:
                approach_enum = ApproachStrategy(approach)
            except ValueError:
                approach_enum = ApproachStrategy.CUSTOM
        else:
            approach_enum = approach

        role = WorkspaceRole(
            machine_id=machine_id,
            approach=approach_enum,
            custom_approach=approach if approach_enum == ApproachStrategy.CUSTOM else "",
            task_id=task_id,
            branch=branch or f"{machine_id}/{task_id}" if task_id else "",
            ide=ide,
            description=description or self._default_description(approach_enum),
        )

        # Archive current role if exists
        if machine_id in self._roles:
            self._history.append(self._roles[machine_id].to_dict())

        self._roles[machine_id] = role
        self._save()

        logger.info(
            "Workspace role assigned: %s -> %s (task=%s)",
            machine_id, role.approach_label, task_id[:8] if task_id else "none",
        )
        return role

    def assign_fleet(
        self,
        task_id: str,
        machines: list[str],
        approaches: dict[str, str] | None = None,
    ) -> dict[str, WorkspaceRole]:
        """Assign roles to all machines in the fleet for a task.

        If approaches not provided, uses default round-robin assignment.
        """
        if approaches is None:
            default_order = [
                ApproachStrategy.STRAIGHTFORWARD,
                ApproachStrategy.ALTERNATIVE,
                ApproachStrategy.HARDENING,
                ApproachStrategy.EXPLORATORY,
                ApproachStrategy.MINIMAL,
            ]
            approaches = {}
            for i, machine in enumerate(machines):
                approaches[machine] = default_order[i % len(default_order)].value

        results = {}
        for machine in machines:
            approach = approaches.get(machine, "straightforward")
            results[machine] = self.assign(
                machine_id=machine,
                approach=approach,
                task_id=task_id,
            )
        return results

    def get_role(self, machine_id: str) -> Optional[WorkspaceRole]:
        """Get current workspace role for a machine."""
        return self._roles.get(machine_id)

    def get_all_roles(self) -> dict[str, dict]:
        """Get all current workspace roles."""
        return {mid: role.to_dict() for mid, role in self._roles.items()}

    def get_fleet_status(self) -> dict:
        """Get fleet-wide workspace status."""
        roles = self.get_all_roles()
        approaches = [r.get("approach", "") for r in roles.values()]
        return {
            "active_machines": len(roles),
            "roles": roles,
            "approach_diversity": len(set(approaches)),
            "approaches_in_use": list(set(approaches)),
        }

    def clear_task(self, task_id: str) -> int:
        """Clear all roles for a specific task. Returns count cleared."""
        to_clear = [
            mid for mid, role in self._roles.items()
            if role.task_id == task_id
        ]
        for mid in to_clear:
            self._history.append(self._roles[mid].to_dict())
            del self._roles[mid]
        self._save()
        return len(to_clear)

    def _default_description(self, approach: ApproachStrategy) -> str:
        descriptions = {
            ApproachStrategy.STRAIGHTFORWARD: "Exploring the most direct implementation path",
            ApproachStrategy.ALTERNATIVE: "Exploring a cleaner abstraction or different approach",
            ApproachStrategy.HARDENING: "Focusing on robustness, edge-cases, and simplification",
            ApproachStrategy.EXPLORATORY: "Open-ended exploration of the solution space",
            ApproachStrategy.MINIMAL: "Minimum viable change to satisfy requirements",
            ApproachStrategy.CUSTOM: "Custom approach",
        }
        return descriptions.get(approach, "Custom approach")

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "roles": {mid: role.to_dict() for mid, role in self._roles.items()},
                "history_count": len(self._history),
                "updated_at": time.time(),
            }
            self._persist_path.write_text(json.dumps(data, indent=2))
        except OSError as e:
            logger.debug("Workspace roles persist failed: %s", e)

    def _load(self) -> None:
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for mid, role_data in data.get("roles", {}).items():
                self._roles[mid] = WorkspaceRole.from_dict(role_data)
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Workspace roles load failed: %s", e)


# ── Superset integration ────────────────────────────────────────────────────


def detect_workspace_ide() -> str:
    """Detect which IDE/runtime this workspace is using."""
    # Check env vars set by various tools
    if os.environ.get("SUPERSET_WORKSPACE_NAME"):
        return "superset"
    if os.environ.get("CURSOR_SESSION_ID"):
        return "cursor"
    if os.environ.get("CODEX_WORKSPACE"):
        return "codex"
    if os.environ.get("VSCODE_PID") or os.environ.get("TERM_PROGRAM") == "vscode":
        return "vscode-claude"
    return "terminal"


def register_local_workspace(
    machine_id: str,
    task_id: str = "",
    approach: str = "",
    registry_path: str | Path | None = None,
) -> WorkspaceRole:
    """Register this machine's workspace role.

    Called during workspace setup (e.g., from .superset/setup.sh).
    Detects IDE and creates a workspace registration file.
    """
    ide = detect_workspace_ide()
    if not registry_path:
        registry_path = Path.home() / ".fleet-nerve" / "workspace-roles.json"

    registry = WorkspaceRegistry(persist_path=registry_path)
    role = registry.assign(
        machine_id=machine_id,
        approach=approach or "straightforward",
        task_id=task_id,
        ide=ide,
    )
    return role
