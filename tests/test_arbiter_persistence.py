"""Tests for HumanArbiter persistence — decisions survive daemon restart."""

import json
import time

import pytest

from multifleet.arbiter import HumanArbiter, PendingDecision


def _make_arbiter(tmp_path, **kwargs) -> HumanArbiter:
    """Create arbiter with persistence to tmp_path."""
    persist = tmp_path / "arbiter-pending.json"
    return HumanArbiter(daemon_port=19999, persist_path=str(persist), **kwargs)


def _queue_sample(arbiter: HumanArbiter, task: str = "Fix auth flow") -> PendingDecision:
    return arbiter.queue_decision(
        decision_type="race",
        task=task,
        recommendation={"approach": "rewrite", "confidence": 0.65},
        alternatives=[{"approach": "patch", "node": "mac1"}],
        dissent=[{"surgeon": "neurologist", "concern": "perf"}],
        evidence=[{"type": "probe", "result": "passed"}],
        source_id="race-abc",
    )


class TestPersistOnQueue:
    def test_queue_writes_json_file(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        _queue_sample(arbiter)

        persist = tmp_path / "arbiter-pending.json"
        assert persist.exists()
        data = json.loads(persist.read_text())
        assert len(data) == 1
        assert data[0]["task_description"] == "Fix auth flow"

    def test_queue_multiple_persists_all(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        _queue_sample(arbiter, task="Task A")
        _queue_sample(arbiter, task="Task B")

        data = json.loads((tmp_path / "arbiter-pending.json").read_text())
        assert len(data) == 2
        tasks = {d["task_description"] for d in data}
        assert tasks == {"Task A", "Task B"}


class TestRestoreOnInit:
    def test_new_arbiter_loads_persisted_decisions(self, tmp_path):
        arbiter1 = _make_arbiter(tmp_path)
        decision = _queue_sample(arbiter1)

        # Simulate restart — new instance, same persist path
        arbiter2 = _make_arbiter(tmp_path)
        pending = arbiter2.get_pending()
        assert len(pending) == 1
        assert pending[0]["id"] == decision.id
        assert pending[0]["task_description"] == "Fix auth flow"

    def test_restored_decision_is_decidable(self, tmp_path):
        arbiter1 = _make_arbiter(tmp_path)
        decision = _queue_sample(arbiter1)

        arbiter2 = _make_arbiter(tmp_path)
        result = arbiter2.decide(decision.id, "accept_recommendation")
        assert result["decided"] is True

    def test_no_file_starts_empty(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        assert arbiter.get_pending() == []


class TestRemoveOnResolve:
    def test_decide_removes_from_persist_file(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        d1 = _queue_sample(arbiter, task="Task A")
        d2 = _queue_sample(arbiter, task="Task B")

        arbiter.decide(d1.id, "accept_recommendation")

        data = json.loads((tmp_path / "arbiter-pending.json").read_text())
        assert len(data) == 1
        assert data[0]["id"] == d2.id

    def test_decide_all_leaves_empty_file(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        d1 = _queue_sample(arbiter)

        arbiter.decide(d1.id, "accept_recommendation")

        data = json.loads((tmp_path / "arbiter-pending.json").read_text())
        assert data == []

    def test_expire_stale_updates_persist_file(self, tmp_path):
        arbiter = _make_arbiter(tmp_path, max_age_s=1)
        decision = _queue_sample(arbiter)
        decision.created_at = time.time() - 10

        arbiter.expire_stale()

        data = json.loads((tmp_path / "arbiter-pending.json").read_text())
        assert data == []


class TestCorruptFile:
    def test_corrupt_json_starts_fresh(self, tmp_path):
        persist = tmp_path / "arbiter-pending.json"
        persist.write_text("{{{not valid json")

        arbiter = HumanArbiter(daemon_port=19999, persist_path=str(persist))
        assert arbiter.get_pending() == []

    def test_missing_keys_starts_fresh(self, tmp_path):
        persist = tmp_path / "arbiter-pending.json"
        persist.write_text(json.dumps([{"id": "x"}]))  # missing required fields

        arbiter = HumanArbiter(daemon_port=19999, persist_path=str(persist))
        assert arbiter.get_pending() == []

    def test_empty_array_is_valid(self, tmp_path):
        persist = tmp_path / "arbiter-pending.json"
        persist.write_text("[]")

        arbiter = HumanArbiter(daemon_port=19999, persist_path=str(persist))
        assert arbiter.get_pending() == []


class TestAtomicWrite:
    def test_no_tmp_file_left_after_save(self, tmp_path):
        arbiter = _make_arbiter(tmp_path)
        _queue_sample(arbiter)

        tmp_file = tmp_path / "arbiter-pending.tmp"
        assert not tmp_file.exists()
        assert (tmp_path / "arbiter-pending.json").exists()
