"""Merge Adjudicator — executes cherry-pick decisions from ChiefSynthesis.

ChiefSynthesis picks winners and produces SynthesisResult with
`decision="cherry_pick"` and `cherry_pick_from=[{machine, commit, reason}]`.
Before this module, nothing actually ran the git cherry-pick — the decision
was decided but not executed.

MergeAdjudicator closes that loop: given a decision dict, it runs
`git cherry-pick <commit>` for each entry, handles conflicts by aborting
cleanly, and records the outcome to the evidence ledger for provenance.

Usage:
    adjudicator = MergeAdjudicator(
        repo_path="/path/to/repo",
        evidence_ledger=ledger,
    )
    result = adjudicator.execute(synthesis_result.to_chief_decision())
    # result: {status, applied, failed, conflicts, winner_branch, evidence_id}
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.merge-adjudicator")

# Max seconds any single git invocation may take
GIT_TIMEOUT_S = 30


@dataclass
class AdjudicationResult:
    """Outcome of running MergeAdjudicator.execute()."""

    status: str  # "ok" | "partial" | "conflict" | "failed" | "skipped" | "noop"
    applied: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    winner_branch: str = ""
    task_id: str = ""
    evidence_id: Optional[str] = None
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "applied": self.applied,
            "failed": self.failed,
            "conflicts": self.conflicts,
            "winner_branch": self.winner_branch,
            "task_id": self.task_id,
            "evidence_id": self.evidence_id,
            "error": self.error,
        }


class MergeAdjudicator:
    """Executes cherry-pick merge decisions produced by ChiefSynthesis.

    Args:
        repo_path: Path to the git repo where cherries will be applied.
        evidence_ledger: Optional EvidenceLedger instance for provenance.
        dry_run: If True, no git commands are actually run — useful for tests
            and for showing a plan without mutating the repo.
        node_id: Node identifier for evidence ledger entries (defaults to
            MULTIFLEET_NODE_ID env var).
    """

    def __init__(
        self,
        repo_path: str,
        evidence_ledger: Any = None,
        dry_run: bool = False,
        node_id: str = "",
    ):
        self._repo = Path(repo_path)
        self._ledger = evidence_ledger
        self._dry_run = dry_run
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    # ── Public API ──

    def execute(self, decision: dict) -> AdjudicationResult:
        """Execute a cherry-pick decision.

        Args:
            decision: Dict matching SynthesisResult.to_chief_decision() shape.
                Required keys: decision, cherry_pick_from.
                Optional keys: winner_branch, task_id.

        Returns:
            AdjudicationResult with status and per-pick details.
        """
        task_id = str(decision.get("task_id", ""))
        winner_branch = str(decision.get("winner_branch", ""))
        result = AdjudicationResult(
            status="skipped",
            task_id=task_id,
            winner_branch=winner_branch,
        )

        # Guard 1: decision must actually be a cherry_pick
        if decision.get("decision") != "cherry_pick":
            result.status = "skipped"
            result.error = f"decision is '{decision.get('decision')}', not 'cherry_pick'"
            return result

        picks = decision.get("cherry_pick_from", []) or []
        if not picks:
            result.status = "noop"
            result.error = "cherry_pick_from list is empty"
            return result

        # Guard 2: repo must exist and be a git worktree
        if not self._is_git_repo():
            result.status = "failed"
            result.error = f"{self._repo} is not a git repository"
            return result

        # Checkout the winner branch first so cherries land on top of it.
        if winner_branch:
            checkout = self._git("checkout", winner_branch)
            if not checkout["ok"]:
                result.status = "failed"
                result.error = f"checkout {winner_branch} failed: {checkout['stderr'].strip()}"
                return result

        # Apply each cherry-pick in order
        for pick in picks:
            commit = str(pick.get("commit", "")).strip()
            if not commit:
                result.failed.append({"pick": pick, "error": "no commit specified"})
                continue

            cp = self._git("cherry-pick", commit)
            if cp["ok"]:
                result.applied.append(pick)
                logger.info("cherry-picked %s (%s)", commit[:12], pick.get("reason", ""))
                continue

            # Detect merge conflict
            stderr = (cp.get("stderr") or "").lower()
            is_conflict = any(
                marker in stderr
                for marker in (
                    "conflict",
                    "could not apply",
                    "merge conflict",
                )
            )
            if is_conflict:
                # Abort cleanly to leave the repo in a usable state
                abort = self._git("cherry-pick", "--abort")
                result.conflicts.append({
                    "pick": pick,
                    "error": cp["stderr"].strip(),
                    "aborted": abort["ok"],
                })
                logger.warning(
                    "cherry-pick conflict on %s (aborted=%s)",
                    commit[:12], abort["ok"],
                )
            else:
                result.failed.append({
                    "pick": pick,
                    "error": cp["stderr"].strip() or "unknown failure",
                })

        # Derive overall status
        if result.conflicts and not result.applied:
            result.status = "conflict"
        elif result.conflicts or result.failed:
            result.status = "partial" if result.applied else "failed"
        else:
            result.status = "ok"

        # Record to evidence ledger (best-effort)
        if self._ledger is not None:
            try:
                subject = (
                    f"cherry_pick {result.status}: "
                    f"{len(result.applied)}/{len(picks)} applied "
                    f"for task {task_id or '?'}"
                )
                entry = self._ledger.record(
                    event_type="merge_decision",
                    node_id=self._node_id,
                    subject=subject,
                    payload=result.to_dict(),
                )
                # ledger.record() may return an entry dict or id
                if isinstance(entry, dict):
                    result.evidence_id = entry.get("entry_id") or entry.get("entry_hash")
                elif entry:
                    result.evidence_id = str(entry)
            except Exception as e:
                logger.warning("Evidence ledger recording failed: %s", e)

        return result

    def execute_race_merge(
        self,
        decision: dict,
        *,
        base_branch: str = "main",
        push_remote: str = "",
    ) -> AdjudicationResult:
        """Create a merge branch from a race verdict and cherry-pick winners.

        High-level automation: given a chief synthesis decision from a fleet
        race, creates ``merge/<race_id>`` off *base_branch*, cherry-picks the
        winning commits onto it, and optionally pushes for human review.

        Safety invariants:
        - Never force-pushes
        - Never merges to main (branch is for review only)
        - Always creates a new branch
        - Aborts cleanly on conflicts
        - Records the attempt in evidence ledger

        Args:
            decision: Chief decision dict (from SynthesisResult.to_chief_decision()).
            base_branch: Branch to fork the merge branch from (default "main").
            push_remote: If non-empty, push the merge branch to this remote
                (e.g. "origin"). Empty string = skip push.

        Returns:
            AdjudicationResult with winner_branch set to the merge branch name.
        """
        task_id = str(decision.get("task_id", "unknown"))
        merge_branch = f"merge/{task_id}"

        result = AdjudicationResult(
            status="failed",
            task_id=task_id,
            winner_branch=merge_branch,
        )

        # Guard: must be a cherry_pick decision
        if decision.get("decision") != "cherry_pick":
            result.status = "skipped"
            result.error = f"decision is '{decision.get('decision')}', not 'cherry_pick'"
            return result

        picks = decision.get("cherry_pick_from", []) or []
        if not picks:
            result.status = "noop"
            result.error = "cherry_pick_from list is empty"
            return result

        if not self._is_git_repo():
            result.error = f"{self._repo} is not a git repository"
            return result

        # 1. Ensure we're on a clean state and create the merge branch
        checkout_base = self._git("checkout", base_branch)
        if not checkout_base["ok"]:
            result.error = f"checkout {base_branch} failed: {checkout_base['stderr'].strip()}"
            return result

        create_branch = self._git("checkout", "-b", merge_branch)
        if not create_branch["ok"]:
            result.error = f"create branch {merge_branch} failed: {create_branch['stderr'].strip()}"
            return result

        # 2. Cherry-pick each winning commit
        for pick in picks:
            commit = str(pick.get("commit", "")).strip()
            if not commit:
                result.failed.append({"pick": pick, "error": "no commit specified"})
                continue

            cp = self._git("cherry-pick", commit)
            if cp["ok"]:
                result.applied.append(pick)
                logger.info("cherry-picked %s onto %s", commit[:12], merge_branch)
                continue

            stderr = (cp.get("stderr") or "").lower()
            is_conflict = any(
                marker in stderr
                for marker in ("conflict", "could not apply", "merge conflict")
            )
            if is_conflict:
                self._git("cherry-pick", "--abort")
                result.conflicts.append({
                    "pick": pick,
                    "error": cp["stderr"].strip(),
                })
                logger.warning("conflict on %s — aborted", commit[:12])
            else:
                result.failed.append({
                    "pick": pick,
                    "error": cp["stderr"].strip() or "unknown failure",
                })

        # 3. Derive status
        if not result.applied:
            result.status = "conflict" if result.conflicts else "failed"
            # Clean up: delete the empty merge branch
            self._git("checkout", base_branch)
            self._git("branch", "-D", merge_branch)
            result.winner_branch = ""
        elif result.conflicts or result.failed:
            result.status = "partial"
        else:
            result.status = "ok"

        # 4. Push for human review (never force, never to main)
        if push_remote and result.applied:
            push = self._git("push", push_remote, merge_branch)
            if not push["ok"]:
                logger.warning("push failed (non-fatal): %s", push["stderr"].strip())

        # 5. Record to evidence ledger
        if self._ledger is not None:
            try:
                subject = (
                    f"race_merge {result.status}: "
                    f"{len(result.applied)}/{len(picks)} applied "
                    f"on {merge_branch} for race {task_id}"
                )
                entry = self._ledger.record(
                    event_type="race_merge",
                    node_id=self._node_id,
                    subject=subject,
                    payload=result.to_dict(),
                )
                if isinstance(entry, dict):
                    result.evidence_id = entry.get("entry_id") or entry.get("entry_hash")
                elif entry:
                    result.evidence_id = str(entry)
            except Exception as e:
                logger.warning("Evidence ledger recording failed: %s", e)

        return result

    # ── Internals ──

    def _is_git_repo(self) -> bool:
        if not self._repo.is_dir():
            return False
        check = self._git("rev-parse", "--git-dir", _raise_on_error=False)
        return check["ok"]

    def _git(self, *args: str, _raise_on_error: bool = False) -> dict:
        """Run a git command inside the adjudicator's repo."""
        if self._dry_run:
            return {
                "ok": True,
                "stdout": f"[dry-run] git {' '.join(args)}",
                "stderr": "",
                "returncode": 0,
            }
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self._repo),
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
            )
            return {
                "ok": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"git {args[0]} timed out after {GIT_TIMEOUT_S}s",
                "returncode": -1,
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "git executable not found in PATH",
                "returncode": -1,
            }
