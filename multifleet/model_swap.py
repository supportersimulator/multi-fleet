"""Model Swap Checkpoint — invariance layer for model fallback events.

Captures full project state BEFORE a model switch (e.g., Opus → DeepSeek)
so the new model can restore the project's spine: active tasks, git state,
CLAUDE.md awareness, and session context. Without this, model downgrades
lose the thread of thought.

Trigger points: before large edits, before model fallback, after fallback,
at periodic checkpoints, on session close/reopen.

Output: /tmp/model-swap-checkpoint.json (re-injectable on resume).
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.model-swap")

CHECKPOINT_PATH = Path("/tmp/model-swap-checkpoint.json")
HISTORY_DIR = Path("/tmp/model-swap-history")


@dataclass
class SwapCheckpointData:
    """Snapshot of project state at model swap boundary."""

    checkpoint_id: str = ""
    timestamp: str = ""
    reason: str = ""
    source_model: str = ""
    target_model: str = ""

    # Git state
    branch: str = ""
    last_5_commits: list[str] = field(default_factory=list)
    dirty_files: list[str] = field(default_factory=list)
    staged_files: list[str] = field(default_factory=list)

    # Active task from fleet messages
    active_task: str = ""
    fleet_inbox_count: int = 0

    # CLAUDE.md awareness summary
    claude_md_hash: str = ""
    claude_md_summary: str = ""

    # Session context
    session_id: str = ""
    node_id: str = ""
    working_dir: str = ""

    # Evidence / historian (best-effort)
    last_session_summary: str = ""
    pending_critical_findings: int = 0

    def to_dict(self) -> dict:
        return {
            "checkpoint_id": self.checkpoint_id,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "source_model": self.source_model,
            "target_model": self.target_model,
            "branch": self.branch,
            "last_5_commits": self.last_5_commits,
            "dirty_files": self.dirty_files,
            "staged_files": self.staged_files,
            "active_task": self.active_task,
            "fleet_inbox_count": self.fleet_inbox_count,
            "claude_md_hash": self.claude_md_hash,
            "claude_md_summary": self.claude_md_summary,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "working_dir": self.working_dir,
            "last_session_summary": self.last_session_summary,
            "pending_critical_findings": self.pending_critical_findings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SwapCheckpointData":
        return cls(
            checkpoint_id=d.get("checkpoint_id", ""),
            timestamp=d.get("timestamp", ""),
            reason=d.get("reason", ""),
            source_model=d.get("source_model", ""),
            target_model=d.get("target_model", ""),
            branch=d.get("branch", ""),
            last_5_commits=d.get("last_5_commits", []),
            dirty_files=d.get("dirty_files", []),
            staged_files=d.get("staged_files", []),
            active_task=d.get("active_task", ""),
            fleet_inbox_count=d.get("fleet_inbox_count", 0),
            claude_md_hash=d.get("claude_md_hash", ""),
            claude_md_summary=d.get("claude_md_summary", ""),
            session_id=d.get("session_id", ""),
            node_id=d.get("node_id", ""),
            working_dir=d.get("working_dir", ""),
            last_session_summary=d.get("last_session_summary", ""),
            pending_critical_findings=d.get("pending_critical_findings", 0),
        )


class ModelSwapCheckpoint:
    """Captures and restores project state across model swap boundaries.

    All capture methods are defensive — partial data always better than crash.
    """

    def __init__(
        self,
        repo_root: str = ".",
        checkpoint_path: Optional[str] = None,
        fleet_messages_dir: Optional[str] = None,
    ):
        self._repo = Path(repo_root).resolve()
        self._checkpoint_path = Path(checkpoint_path) if checkpoint_path else CHECKPOINT_PATH
        self._fleet_dir = (
            Path(fleet_messages_dir)
            if fleet_messages_dir
            else self._repo / ".fleet-messages"
        )
        self._node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    def capture(
        self,
        reason: str = "model-swap",
        source_model: str = "",
        target_model: str = "",
    ) -> SwapCheckpointData:
        """Snapshot current state. Never raises — partial data is fine."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cp_id = f"swap-{ts.replace(':', '').replace('-', '').replace('T', '-').replace('Z', '')}"

        data = SwapCheckpointData(
            checkpoint_id=cp_id,
            timestamp=ts,
            reason=reason,
            source_model=source_model or os.environ.get("ANTHROPIC_MODEL", "unknown"),
            target_model=target_model,
            node_id=self._node_id,
            session_id=os.environ.get("CLAUDE_SESSION_ID", ""),
            working_dir=str(self._repo),
        )

        # Git state
        data.branch = self._git("branch", "--show-current")
        raw_log = self._git("log", "--oneline", "-5")
        data.last_5_commits = [l for l in raw_log.splitlines() if l.strip()] if raw_log else []
        dirty = self._git("status", "--porcelain")
        data.dirty_files = [l.strip() for l in dirty.splitlines() if l.strip()] if dirty else []
        staged = self._git("diff", "--cached", "--name-only")
        data.staged_files = [l.strip() for l in staged.splitlines() if l.strip()] if staged else []

        # Fleet messages — active task
        data.active_task, data.fleet_inbox_count = self._scan_fleet_inbox()

        # CLAUDE.md awareness
        data.claude_md_hash, data.claude_md_summary = self._read_claude_md()

        # Session historian (best-effort)
        data.last_session_summary = self._get_session_summary()

        # Critical findings count
        data.pending_critical_findings = self._count_criticals()

        # Persist
        self._save(data)
        self._archive(data)

        logger.info("model_swap checkpoint captured: %s (%s)", cp_id, reason)
        return data

    def restore(self, path: Optional[str] = None) -> Optional[SwapCheckpointData]:
        """Load checkpoint from disk. Returns None if unavailable."""
        target = Path(path) if path else self._checkpoint_path
        if not target.exists():
            logger.warning("No model-swap checkpoint at %s", target)
            return None
        try:
            raw = json.loads(target.read_text())
            data = SwapCheckpointData.from_dict(raw)
            logger.info("model_swap checkpoint restored: %s", data.checkpoint_id)
            return data
        except Exception as e:
            logger.error("Failed to restore model-swap checkpoint: %s", e)
            return None

    def diff_from_checkpoint(
        self, checkpoint: Optional[SwapCheckpointData] = None
    ) -> dict[str, Any]:
        """Compare current state against a checkpoint. Shows what changed."""
        if checkpoint is None:
            checkpoint = self.restore()
        if checkpoint is None:
            return {"error": "no checkpoint available", "changes": []}

        current = self.capture(reason="diff-probe")
        changes: list[str] = []

        if current.branch != checkpoint.branch:
            changes.append(f"branch: {checkpoint.branch} -> {current.branch}")

        old_commits = set(checkpoint.last_5_commits)
        new_commits = [c for c in current.last_5_commits if c not in old_commits]
        if new_commits:
            changes.append(f"new_commits: {len(new_commits)}")

        old_dirty = set(checkpoint.dirty_files)
        new_dirty = set(current.dirty_files)
        added_dirty = new_dirty - old_dirty
        removed_dirty = old_dirty - new_dirty
        if added_dirty:
            changes.append(f"newly_dirty: {len(added_dirty)} files")
        if removed_dirty:
            changes.append(f"cleaned: {len(removed_dirty)} files")

        if current.claude_md_hash != checkpoint.claude_md_hash:
            changes.append("CLAUDE.md modified since checkpoint")

        if current.active_task != checkpoint.active_task:
            changes.append(f"active_task changed: {checkpoint.active_task!r} -> {current.active_task!r}")

        return {
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_ts": checkpoint.timestamp,
            "current_ts": current.timestamp,
            "changes": changes,
            "new_commits": new_commits,
            "files_changed": list(added_dirty),
        }

    # ── Internal helpers ──

    def _git(self, *args: str) -> str:
        try:
            r = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=str(self._repo),
                timeout=5,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def _scan_fleet_inbox(self) -> tuple[str, int]:
        """Read fleet inbox for active task. Returns (task_summary, message_count)."""
        try:
            inbox = self._fleet_dir / self._node_id
            if not inbox.exists():
                return "", 0
            msgs = sorted(inbox.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            count = len(msgs)
            if not msgs:
                return "", 0
            # Read newest message subject line
            first_line = msgs[0].read_text(errors="replace").split("\n")
            for line in first_line:
                if line.startswith("subject:"):
                    return line.split(":", 1)[1].strip(), count
            return msgs[0].stem, count
        except Exception:
            return "", 0

    def _read_claude_md(self) -> tuple[str, str]:
        """Hash + first-line summary of CLAUDE.md. Returns (hash, summary)."""
        try:
            claude_md = self._repo / "CLAUDE.md"
            if not claude_md.exists():
                return "", ""
            import hashlib

            content = claude_md.read_text(errors="replace")
            h = hashlib.sha256(content.encode()).hexdigest()[:16]
            # Summary = first non-empty, non-comment line
            for line in content.split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    return h, stripped[:200]
            return h, "(CLAUDE.md present)"
        except Exception:
            return "", ""

    def _get_session_summary(self) -> str:
        """Best-effort session historian integration."""
        try:
            from memory.session_historian import SessionHistorian

            h = SessionHistorian()
            latest = h.get_latest_summary()
            if latest:
                return str(latest)[:500]
        except Exception:
            pass
        return ""

    def _count_criticals(self) -> int:
        """Count pending critical findings (best-effort)."""
        try:
            from memory.session_gold_passes import get_critical_findings

            findings = get_critical_findings()
            return len(findings) if findings else 0
        except Exception:
            return 0

    def _save(self, data: SwapCheckpointData) -> None:
        try:
            self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_path.write_text(json.dumps(data.to_dict(), indent=2))
        except Exception as e:
            logger.error("Failed to save model-swap checkpoint: %s", e)

    def _archive(self, data: SwapCheckpointData) -> None:
        """Keep history of swaps in /tmp/model-swap-history/."""
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            path = HISTORY_DIR / f"{data.checkpoint_id}.json"
            path.write_text(json.dumps(data.to_dict(), indent=2))
            # Prune to last 20
            files = sorted(HISTORY_DIR.glob("*.json"))
            while len(files) > 20:
                files.pop(0).unlink()
        except Exception:
            pass
