"""Tests for multifleet.merge_adjudicator — cherry-pick execution layer.

Uses real git repos in tmp dirs (via the `git_repo` fixture) so we
verify actual cherry-pick behavior, not just subprocess mocking.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multifleet.merge_adjudicator import (
    AdjudicationResult,
    MergeAdjudicator,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def git_repo(tmp_path):
    """Build a real git repo with 2 feature branches + an empty winner branch.

    Layout:
        main                  base commit (file.txt)
        feature-a             main + a.txt (commit sha = a_commit)
        feature-b             main + b.txt (commit sha = b_commit)
        feature-b-conflict    main + file.txt overwritten (conflict with winner)
        winner                identical to main, target for cherries
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "test@example.com")
    _run(repo, "git", "config", "user.name", "Test")
    _run(repo, "git", "config", "commit.gpgsign", "false")

    # base on main
    (repo / "file.txt").write_text("base\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "base commit")

    # feature-a: adds a.txt
    _run(repo, "git", "checkout", "-q", "-b", "feature-a")
    (repo / "a.txt").write_text("feature a\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "add feature a")
    a_commit = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()

    # feature-b: adds b.txt
    _run(repo, "git", "checkout", "-q", "main")
    _run(repo, "git", "checkout", "-q", "-b", "feature-b")
    (repo / "b.txt").write_text("feature b\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "add feature b")
    b_commit = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()

    # feature-b-conflict: modifies file.txt (will conflict with winner's later modification)
    _run(repo, "git", "checkout", "-q", "main")
    _run(repo, "git", "checkout", "-q", "-b", "feature-b-conflict")
    (repo / "file.txt").write_text("base\nfrom feature-b\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "edit file from feature-b")
    conflict_commit = _run(repo, "git", "rev-parse", "HEAD").stdout.strip()

    # winner: modify file.txt differently
    _run(repo, "git", "checkout", "-q", "main")
    _run(repo, "git", "checkout", "-q", "-b", "winner")
    (repo / "file.txt").write_text("base\nfrom winner\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-q", "-m", "winner edits file")

    return {
        "path": str(repo),
        "a_commit": a_commit,
        "b_commit": b_commit,
        "conflict_commit": conflict_commit,
    }


class _FakeLedger:
    """In-memory stand-in for EvidenceLedger.record()."""

    def __init__(self):
        self.entries = []
        self._counter = 0

    def record(self, *, event_type, node_id, subject, payload):
        self._counter += 1
        entry = {
            "entry_id": f"evt-{self._counter}",
            "event_type": event_type,
            "node_id": node_id,
            "subject": subject,
            "payload": payload,
            "entry_hash": f"hash-{self._counter}",
        }
        self.entries.append(entry)
        return entry


# ── Guards (no-op paths) ────────────────────────────────────────────────────


class TestGuards:
    def test_skipped_when_decision_is_merge(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "t1",
            "decision": "merge",
            "winner_branch": "winner",
            "cherry_pick_from": [{"machine": "mac2", "commit": "HEAD", "reason": "x"}],
        })
        assert result.status == "skipped"
        assert "not 'cherry_pick'" in result.error

    def test_noop_when_cherry_pick_list_empty(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "t2",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [],
        })
        assert result.status == "noop"

    def test_failed_when_repo_not_git(self, tmp_path):
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        adj = MergeAdjudicator(repo_path=str(not_a_repo))
        result = adj.execute({
            "task_id": "t3",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [{"machine": "mac2", "commit": "deadbeef", "reason": "x"}],
        })
        assert result.status == "failed"
        assert "not a git repository" in result.error


# ── Happy-path cherry-picks ────────────────────────────────────────────────


class TestCherryPickExecution:
    def test_single_cherry_pick_applies_cleanly(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "task-a",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "unique strength"},
            ],
        })
        assert result.status == "ok"
        assert len(result.applied) == 1
        assert result.applied[0]["machine"] == "mac2"
        assert result.failed == []
        assert result.conflicts == []
        # a.txt should now exist on winner branch
        assert (Path(git_repo["path"]) / "a.txt").is_file()

    def test_multiple_cherry_picks_all_apply(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "task-multi",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "adds a.txt"},
                {"machine": "mac3", "commit": git_repo["b_commit"], "reason": "adds b.txt"},
            ],
        })
        assert result.status == "ok"
        assert len(result.applied) == 2
        # Both files now on winner branch
        assert (Path(git_repo["path"]) / "a.txt").is_file()
        assert (Path(git_repo["path"]) / "b.txt").is_file()

    def test_skips_pick_without_commit(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "task-empty-commit",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": "", "reason": "bad"},
                {"machine": "mac3", "commit": git_repo["a_commit"], "reason": "good"},
            ],
        })
        assert result.status == "partial"
        assert len(result.applied) == 1
        assert len(result.failed) == 1
        assert "no commit" in result.failed[0]["error"]


# ── Conflict handling ──────────────────────────────────────────────────────


class TestConflictHandling:
    def test_conflict_is_aborted_and_recorded(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "task-conflict",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac3", "commit": git_repo["conflict_commit"],
                 "reason": "edits file.txt differently"},
            ],
        })
        assert result.status == "conflict"
        assert len(result.conflicts) == 1
        assert result.conflicts[0]["aborted"] is True
        # Repo must be in a clean state after abort — no merge in progress
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo["path"], capture_output=True, text=True, check=True,
        )
        assert status.stdout.strip() == "", "working tree must be clean after abort"

    def test_conflict_plus_clean_pick_is_partial(self, git_repo):
        """When one cherry succeeds and another conflicts, overall status is partial.

        Order matters: apply the clean one first, then the conflicting one
        (which is aborted). We should end up with applied=1, conflicts=1.
        """
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute({
            "task_id": "task-mixed",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "clean"},
                {"machine": "mac3", "commit": git_repo["conflict_commit"],
                 "reason": "conflict"},
            ],
        })
        assert result.status == "partial"
        assert len(result.applied) == 1
        assert len(result.conflicts) == 1
        # Clean cherry still landed
        assert (Path(git_repo["path"]) / "a.txt").is_file()


# ── Evidence ledger integration ────────────────────────────────────────────


class TestEvidenceRecording:
    def test_records_evidence_entry_on_success(self, git_repo):
        ledger = _FakeLedger()
        adj = MergeAdjudicator(
            repo_path=git_repo["path"],
            evidence_ledger=ledger,
            node_id="mac1",
        )
        result = adj.execute({
            "task_id": "task-recorded",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        assert result.evidence_id is not None
        assert len(ledger.entries) == 1
        entry = ledger.entries[0]
        assert entry["event_type"] == "merge_decision"
        assert entry["node_id"] == "mac1"
        assert "cherry_pick ok" in entry["subject"]
        assert entry["payload"]["status"] == "ok"
        assert len(entry["payload"]["applied"]) == 1

    def test_records_evidence_on_conflict(self, git_repo):
        ledger = _FakeLedger()
        adj = MergeAdjudicator(
            repo_path=git_repo["path"],
            evidence_ledger=ledger,
        )
        adj.execute({
            "task_id": "task-conflict-ledger",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac3", "commit": git_repo["conflict_commit"], "reason": "c"},
            ],
        })
        assert len(ledger.entries) == 1
        assert ledger.entries[0]["payload"]["status"] == "conflict"

    def test_no_evidence_for_skipped_decisions(self, git_repo):
        ledger = _FakeLedger()
        adj = MergeAdjudicator(
            repo_path=git_repo["path"],
            evidence_ledger=ledger,
        )
        adj.execute({
            "task_id": "t",
            "decision": "merge",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        # Skipped decisions don't record — the merge itself would be recorded elsewhere
        assert ledger.entries == []


# ── Dry run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_does_not_mutate_repo(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"], dry_run=True)
        result = adj.execute({
            "task_id": "task-dry",
            "decision": "cherry_pick",
            "winner_branch": "winner",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        assert result.status == "ok"
        assert len(result.applied) == 1
        # a.txt must NOT be present — dry run didn't actually apply
        assert not (Path(git_repo["path"]) / "a.txt").exists()


# ── Race merge (branch creation + cherry-pick automation) ──────────────────


class TestExecuteRaceMerge:
    def test_creates_merge_branch_and_cherry_picks(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute_race_merge({
            "task_id": "race-42",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "adds a"},
            ],
        })
        assert result.status == "ok"
        assert result.winner_branch == "merge/race-42"
        assert len(result.applied) == 1
        # Verify branch exists and a.txt is on it
        branches = _run(Path(git_repo["path"]), "git", "branch", "--list", "merge/race-42")
        assert "merge/race-42" in branches.stdout
        _run(Path(git_repo["path"]), "git", "checkout", "merge/race-42")
        assert (Path(git_repo["path"]) / "a.txt").is_file()

    def test_multiple_picks_on_merge_branch(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute_race_merge({
            "task_id": "race-multi",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "a"},
                {"machine": "mac3", "commit": git_repo["b_commit"], "reason": "b"},
            ],
        })
        assert result.status == "ok"
        assert len(result.applied) == 2
        _run(Path(git_repo["path"]), "git", "checkout", "merge/race-multi")
        assert (Path(git_repo["path"]) / "a.txt").is_file()
        assert (Path(git_repo["path"]) / "b.txt").is_file()

    def test_conflict_deletes_merge_branch(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        # Fork from "winner" branch where file.txt differs — conflict_commit
        # also modifies file.txt, so cherry-pick will conflict.
        result = adj.execute_race_merge({
            "task_id": "race-conflict",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac3", "commit": git_repo["conflict_commit"],
                 "reason": "conflicts"},
            ],
        }, base_branch="winner")
        assert result.status == "conflict"
        assert result.winner_branch == ""  # cleaned up
        # Merge branch should not exist
        branches = _run(Path(git_repo["path"]), "git", "branch", "--list", "merge/race-conflict")
        assert "merge/race-conflict" not in branches.stdout

    def test_partial_keeps_merge_branch(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        # Fork from "winner" so conflict_commit will conflict on file.txt
        # but a_commit (adds a.txt) applies cleanly.
        result = adj.execute_race_merge({
            "task_id": "race-partial",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "clean"},
                {"machine": "mac3", "commit": git_repo["conflict_commit"],
                 "reason": "conflicts"},
            ],
        }, base_branch="winner")
        assert result.status == "partial"
        assert result.winner_branch == "merge/race-partial"
        assert len(result.applied) == 1
        assert len(result.conflicts) == 1

    def test_skipped_for_non_cherry_pick_decision(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"])
        result = adj.execute_race_merge({
            "task_id": "race-merge",
            "decision": "merge",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        assert result.status == "skipped"

    def test_records_evidence_for_race_merge(self, git_repo):
        ledger = _FakeLedger()
        adj = MergeAdjudicator(
            repo_path=git_repo["path"],
            evidence_ledger=ledger,
            node_id="mac1",
        )
        result = adj.execute_race_merge({
            "task_id": "race-ev",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        assert result.evidence_id is not None
        assert len(ledger.entries) == 1
        assert ledger.entries[0]["event_type"] == "race_merge"
        assert "race-ev" in ledger.entries[0]["subject"]

    def test_dry_run_does_not_create_branch(self, git_repo):
        adj = MergeAdjudicator(repo_path=git_repo["path"], dry_run=True)
        result = adj.execute_race_merge({
            "task_id": "race-dry",
            "decision": "cherry_pick",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": git_repo["a_commit"], "reason": "x"},
            ],
        })
        assert result.status == "ok"
        # In dry_run, branch creation is simulated, so the actual branch won't exist
        branches = _run(Path(git_repo["path"]), "git", "branch", "--list", "merge/race-dry")
        assert "merge/race-dry" not in branches.stdout


# ── Result serialization ────────────────────────────────────────────────────


class TestAdjudicationResult:
    def test_to_dict_shape(self):
        r = AdjudicationResult(
            status="ok",
            applied=[{"machine": "mac2", "commit": "abc", "reason": "x"}],
            winner_branch="winner",
            task_id="t1",
            evidence_id="evt-1",
        )
        d = r.to_dict()
        assert d["status"] == "ok"
        assert d["applied"][0]["machine"] == "mac2"
        assert d["winner_branch"] == "winner"
        assert d["task_id"] == "t1"
        assert d["evidence_id"] == "evt-1"
        assert d["failed"] == []
        assert d["conflicts"] == []
