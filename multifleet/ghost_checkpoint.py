"""Ghost Checkpoint — captures and restores full system state across session boundaries.

Enables continuity across session crashes, model switches, and fleet reconfigurations.
Saves as JSON files in ~/.fleet-nerve/ghost/checkpoints/. Keeps last 10.

"Cross-IDE continuity: preserves active thread of thought, unresolved decisions,
last surgeon objections, fresh warnings, relevant context packet."
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-checkpoint")

MAX_CHECKPOINTS = 10


@dataclass
class CheckpointData:
    """Full system state at a point in time."""

    checkpoint_id: str
    timestamp: str
    reason: str
    node_id: str

    # Git state
    branch: str = ""
    dirty_files: int = 0
    working_dir: str = ""
    last_commit: str = ""

    # Session state
    session_id: str = ""
    model: str = ""

    # Fleet state (best-effort)
    daemon_healthy: Optional[bool] = None
    peer_count: int = 0
    active_peers: list[str] = field(default_factory=list)

    # Ghost state
    last_pulse_summary: str = ""
    unresolved_objections: list[str] = field(default_factory=list)
    active_warnings: list[str] = field(default_factory=list)
    pending_evidence_candidates: int = 0

    # Workflow state
    workflow_phase: str = ""
    last_synthesis_task: str = ""

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "node_id": self.node_id,
            "branch": self.branch,
            "dirty_files": self.dirty_files,
            "working_dir": self.working_dir,
            "last_commit": self.last_commit,
            "session_id": self.session_id,
            "model": self.model,
            "daemon_healthy": self.daemon_healthy,
            "peer_count": self.peer_count,
            "active_peers": self.active_peers,
            "last_pulse_summary": self.last_pulse_summary,
            "unresolved_objections": self.unresolved_objections,
            "active_warnings": self.active_warnings,
            "pending_evidence_candidates": self.pending_evidence_candidates,
            "workflow_phase": self.workflow_phase,
            "last_synthesis_task": self.last_synthesis_task,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckpointData":
        return cls(
            checkpoint_id=d.get("checkpoint_id", ""),
            timestamp=d.get("timestamp", ""),
            reason=d.get("reason", ""),
            node_id=d.get("node_id", ""),
            branch=d.get("branch", ""),
            dirty_files=d.get("dirty_files", 0),
            working_dir=d.get("working_dir", ""),
            last_commit=d.get("last_commit", ""),
            session_id=d.get("session_id", ""),
            model=d.get("model", ""),
            daemon_healthy=d.get("daemon_healthy"),
            peer_count=d.get("peer_count", 0),
            active_peers=d.get("active_peers", []),
            last_pulse_summary=d.get("last_pulse_summary", ""),
            unresolved_objections=d.get("unresolved_objections", []),
            active_warnings=d.get("active_warnings", []),
            pending_evidence_candidates=d.get("pending_evidence_candidates", 0),
            workflow_phase=d.get("workflow_phase", ""),
            last_synthesis_task=d.get("last_synthesis_task", ""),
        )


class GhostCheckpoint:
    """Captures full system state at a point in time.

    Partial checkpoint is always better than no checkpoint — all data
    gathering uses try/except to ensure we save whatever we can collect.
    """

    def __init__(self, checkpoint_dir: Optional[str] = None, repo_root: str = "."):
        self._dir = Path(
            checkpoint_dir
            or str(Path.home() / ".fleet-nerve" / "ghost" / "checkpoints")
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        self._repo_root = Path(repo_root)
        self._node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    def capture(self, reason: str = "manual") -> CheckpointData:
        """Capture current system state. Never raises — partial data is fine."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cp_id = f"{ts.replace(':', '').replace('-', '').replace('T', '-').replace('Z', '')}-{reason[:20]}"

        data = CheckpointData(
            checkpoint_id=cp_id,
            timestamp=ts,
            reason=reason,
            node_id=self._node_id,
        )

        # Git state
        data.branch = self._git_cmd(["branch", "--show-current"])
        data.dirty_files = len(self._git_cmd(["status", "--porcelain"]).splitlines())
        data.working_dir = str(self._repo_root.resolve())
        data.last_commit = self._git_cmd(["log", "-1", "--oneline"])

        # Session/model
        data.session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        data.model = os.environ.get("ANTHROPIC_MODEL", "")

        # Fleet daemon health
        data.daemon_healthy, data.peer_count, data.active_peers = self._check_daemon()

        # Ghost state — pulse
        data.last_pulse_summary = self._get_last_pulse_summary()

        # Ghost state — objections
        data.unresolved_objections = self._get_unresolved_objections()

        # Ghost state — warnings
        data.active_warnings = self._get_active_warnings()

        # Evidence candidates count
        data.pending_evidence_candidates = self._count_evidence_candidates()

        # Persist
        self._save(data)
        self._prune()

        logger.info("ghost_checkpoint captured: %s (%s)", cp_id, reason)
        return data

    def restore(self, checkpoint_id: str = "latest") -> Optional[CheckpointData]:
        """Restore system state from checkpoint. Returns None if not found."""
        if checkpoint_id == "latest":
            checkpoints = self.list_checkpoints()
            if not checkpoints:
                logger.warning("No checkpoints available to restore")
                return None
            checkpoint_id = checkpoints[0]["checkpoint_id"]

        path = self._dir / f"{checkpoint_id}.json"
        if not path.exists():
            # Try fuzzy match
            for f in sorted(self._dir.glob("*.json"), reverse=True):
                if checkpoint_id in f.stem:
                    path = f
                    break
            else:
                logger.warning("Checkpoint not found: %s", checkpoint_id)
                return None

        try:
            data = CheckpointData.from_dict(json.loads(path.read_text()))
            logger.info("ghost_checkpoint restored: %s", data.checkpoint_id)
            return data
        except Exception as e:
            logger.error("Failed to restore checkpoint %s: %s", checkpoint_id, e)
            return None

    def list_checkpoints(self) -> list[dict]:
        """List available checkpoints, newest first."""
        checkpoints = []
        for f in sorted(self._dir.glob("*.json"), reverse=True):
            try:
                d = json.loads(f.read_text())
                checkpoints.append({
                    "checkpoint_id": d.get("checkpoint_id", f.stem),
                    "timestamp": d.get("timestamp", ""),
                    "reason": d.get("reason", ""),
                    "node_id": d.get("node_id", ""),
                    "branch": d.get("branch", ""),
                })
            except (json.JSONDecodeError, Exception):
                continue
        return checkpoints

    def _save(self, data: CheckpointData) -> None:
        path = self._dir / f"{data.checkpoint_id}.json"
        try:
            path.write_text(json.dumps(data.to_dict(), indent=2))
        except Exception as e:
            logger.error("Failed to save checkpoint: %s", e)

    def _prune(self) -> None:
        """Remove oldest checkpoints beyond MAX_CHECKPOINTS."""
        files = sorted(self._dir.glob("*.json"))
        while len(files) > MAX_CHECKPOINTS:
            oldest = files.pop(0)
            try:
                oldest.unlink()
                logger.debug("Pruned old checkpoint: %s", oldest.name)
            except Exception:
                pass

    def _git_cmd(self, args: list[str]) -> str:
        try:
            r = subprocess.run(
                ["git"] + args,
                capture_output=True, text=True,
                cwd=str(self._repo_root), timeout=5,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def _check_daemon(self) -> tuple[Optional[bool], int, list[str]]:
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=2)
            data = json.loads(resp.read())
            peers = data.get("peers", [])
            return True, len(peers), peers
        except Exception:
            return None, 0, []

    def _get_last_pulse_summary(self) -> str:
        try:
            from multifleet.ghost_pulse import GhostPulse
            pulse = GhostPulse(repo_root=str(self._repo_root))
            result = pulse.pulse()
            return result.summary()
        except Exception:
            return ""

    def _get_unresolved_objections(self) -> list[str]:
        try:
            from multifleet.ghost_objections import ObjectionStore
            store = ObjectionStore()
            return [o.summary() for o in store.list_open()[:10]]
        except Exception:
            return []

    def _get_active_warnings(self) -> list[str]:
        try:
            from multifleet.ghost_pulse import GhostPulse
            pulse = GhostPulse(repo_root=str(self._repo_root))
            return pulse._generate_warnings()[:5]
        except Exception:
            return []

    def _count_evidence_candidates(self) -> int:
        try:
            evidence_dir = Path.home() / ".fleet-nerve" / "evidence-candidates"
            if evidence_dir.exists():
                return len(list(evidence_dir.glob("*.json")))
            return 0
        except Exception:
            return 0
