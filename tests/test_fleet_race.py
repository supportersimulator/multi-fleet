"""Tests for FleetRace — Competitive Branch Racing."""

import json
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.fleet_race import FleetRace, _RaceStore


def _mock_post(path, body, **kw):
    """Smart mock that returns correct response shape per endpoint."""
    if path == "/lease":
        return {"acquired": True, "holder": "mac1", "expires_at": time.time() + 300, "fence_token": 1}
    if path == "/evidence":
        return {"entry_id": "test", "recorded": True}
    # Default: message sent
    return {"sent": True}


@pytest.fixture(autouse=True)
def _block_superset_discovery():
    """Block Superset primary discovery so tests use daemon /health fallback."""
    mock_bridge = MagicMock()
    mock_bridge.list_devices.side_effect = RuntimeError("Superset disabled in tests")
    with patch("multifleet.superset_bridge.SupersetBridge", return_value=mock_bridge):
        yield


@pytest.fixture
def race(tmp_path):
    """FleetRace with mocked HTTP and temp DB to prevent real daemon calls."""
    db = str(tmp_path / "test_races.db")
    r = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
    # Mock all daemon HTTP calls
    r._daemon_get = MagicMock(return_value={
        "status": "ok",
        "channel_health": {"mac2": {"reachable": True}, "mac3": {"reachable": True}},
    })
    r._daemon_post = MagicMock(side_effect=_mock_post)
    return r


class TestStartRace:

    def test_start_creates_race(self, race):
        result = race.start("Fix the auth flow", nodes=["mac1", "mac2"])
        assert "race_id" in result
        assert result["status"] in ("racing", "starting")
        assert result["task"] == "Fix the auth flow"

    def test_race_has_participants(self, race):
        result = race.start("Optimize queries", nodes=["mac1", "mac2", "mac3"])
        assert len(result["participants"]) == 3
        assert "mac1" in result["participants"]
        assert "mac2" in result["participants"]
        assert "mac3" in result["participants"]

    def test_participant_fields(self, race):
        result = race.start("Task", nodes=["mac1"])
        p = result["participants"]["mac1"]
        assert p["node"] == "mac1"
        assert p["branch"].startswith("race/")
        assert p["status"] == "dispatched"
        assert p["dispatched_at"] is not None
        assert p["lease_acquired"] is True

    def test_race_stored_internally(self, race):
        result = race.start("Task", nodes=["mac1"])
        assert race._races[result["race_id"]] is result


class TestDiscoverNodes:

    def test_discover_nodes_from_health(self, race):
        nodes = race._discover_nodes()
        assert "mac1" in nodes  # self
        assert "mac2" in nodes  # from channel_health
        assert "mac3" in nodes

    def test_discover_fallback_on_error(self, race):
        race._daemon_get = MagicMock(return_value={"error": "connection refused"})
        nodes = race._discover_nodes()
        assert nodes == ["mac1"]

    def test_start_discovers_when_no_nodes(self, race):
        result = race.start("Auto-discover task")
        # Should have discovered mac1, mac2, mac3 from health
        assert len(result["participants"]) == 3


class TestLeasePerParticipant:

    def test_lease_acquired_per_participant(self, race):
        result = race.start("Task", nodes=["mac1", "mac2"])
        for p in result["participants"].values():
            assert p["lease_acquired"] is True

    def test_lease_denied_marks_participant(self, race):
        call_count = 0
        def mock_post(path, body, **kw):
            nonlocal call_count
            if path == "/lease":
                call_count += 1
                if call_count == 2:  # deny second lease
                    return {"acquired": False, "reason": "held by other"}
                return {"acquired": True, "holder": "mac1", "expires_at": time.time() + 300, "fence_token": 1}
            return {"sent": True}
        race._daemon_post = mock_post

        result = race.start("Task", nodes=["mac1", "mac2"])
        statuses = [p["status"] for p in result["participants"].values()]
        assert "lease_denied" in statuses


class TestDispatch:

    def test_dispatch_sends_to_each_node(self, race):
        race.start("Task", nodes=["mac1", "mac2", "mac3"])
        # _daemon_post should be called for: broadcast + (lease+dispatch)*3 + evidence
        assert race._daemon_post.call_count >= 3

    def test_dispatch_includes_race_id_in_prompt(self, race):
        calls = []
        orig_post = race._daemon_post
        def capture_post(path, body, **kw):
            calls.append((path, body))
            return {"sent": True, "acquired": True}
        race._daemon_post = capture_post

        result = race.start("Fix auth", nodes=["mac1"])
        race_id = result["race_id"]

        # Find the task dispatch call
        task_calls = [c for c in calls if c[1].get("type") == "task"]
        assert len(task_calls) >= 1
        body = task_calls[0][1]["payload"]["body"]
        assert race_id in body
        assert "Fix auth" in body


class TestEvidence:

    def test_evidence_recorded_on_start(self, race):
        calls = []
        def capture_post(path, body, **kw):
            calls.append((path, body))
            return {"sent": True, "acquired": True}
        race._daemon_post = capture_post

        race.start("Task", nodes=["mac1"])
        evidence_calls = [c for c in calls if c[0] == "/evidence"]
        assert len(evidence_calls) >= 1
        assert evidence_calls[0][1]["event_type"] == "race_started"


class TestStatus:

    def test_status_returns_race_state(self, race):
        result = race.start("Task", nodes=["mac1"])
        status = race.status(result["race_id"])
        assert status["race_id"] == result["race_id"]
        assert status["task"] == "Task"

    def test_status_unknown_race(self, race):
        status = race.status("nonexistent")
        assert "error" in status

    def test_status_detects_timeout(self, race):
        result = race.start("Task", nodes=["mac1", "mac2"], timeout_s=1)
        rid = result["race_id"]
        assert result["status"] == "racing"
        # Force elapsed > timeout
        race._races[rid]["started_at"] = time.time() - 10
        status = race.status(rid)
        assert status["status"] == "timed_out"


class TestListRaces:

    def test_list_races(self, race):
        race.start("Task 1", nodes=["mac1"])
        race.start("Task 2", nodes=["mac2"])
        races = race.list_races()
        assert len(races) == 2
        assert races[0]["task"].startswith("Task")

    def test_list_races_empty(self, race):
        assert race.list_races() == []


class TestSubmitResult:

    def test_submit_marks_completed(self, race):
        result = race.start("Task", nodes=["mac1", "mac2"])
        rid = result["race_id"]

        updated = race.submit_result(rid, "mac1", {
            "summary": "Fixed it",
            "findings": "Root cause was X",
        })
        assert updated["participants"]["mac1"]["status"] == "completed"
        assert updated["status"] == "cross_review"

    def test_submit_unknown_race(self, race):
        result = race.submit_result("bad", "mac1", {})
        assert "error" in result

    def test_submit_unknown_node(self, race):
        r = race.start("Task", nodes=["mac1"])
        result = race.submit_result(r["race_id"], "mac99", {})
        assert "error" in result

    def test_all_complete_triggers_synthesis(self, race):
        result = race.start("Task", nodes=["mac1", "mac2"])
        rid = result["race_id"]

        race.submit_result(rid, "mac1", {"summary": "Approach A"})
        race.submit_result(rid, "mac2", {"summary": "Approach B"})

        status = race.status(rid)
        assert status["status"] in ("done", "blocked", "awaiting_arbiter")  # AcceptanceGate may block, arbiter may escalate
        assert status["winner"] is not None
        assert status["synthesis"] is not None


class TestNoNodes:

    def test_start_no_nodes_available(self, race):
        race._daemon_get = MagicMock(return_value={"error": "down"})
        # With no nodes discovered and none passed, uses fallback [self]
        result = race.start("Task")
        # Should still work with just self
        assert result["status"] in ("racing", "failed")


class TestRaceStore:
    """Tests for _RaceStore SQLite persistence."""

    def test_store_save_and_load(self, tmp_path):
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        race = {"race_id": "r1", "status": "racing", "task": "test", "started_at": 1.0}
        store.save(race)
        active = store.load_active()
        assert "r1" in active
        assert active["r1"]["status"] == "racing"

    def test_terminal_races_excluded_from_active(self, tmp_path):
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        for state in ("done", "failed", "timed_out"):
            store.save({"race_id": f"r-{state}", "status": state, "task": "t"})
        assert store.load_active() == {}

    def test_load_all_includes_terminal(self, tmp_path):
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        store.save({"race_id": "r1", "status": "done", "task": "t"})
        store.save({"race_id": "r2", "status": "racing", "task": "t"})
        all_races = store.load_all()
        assert len(all_races) == 2

    def test_upsert_updates_state(self, tmp_path):
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        store.save({"race_id": "r1", "status": "racing", "task": "t"})
        store.save({"race_id": "r1", "status": "done", "task": "t"})
        assert store.load_active() == {}
        assert "r1" in store.load_all()

    def test_corrupt_json_skipped(self, tmp_path):
        """Corrupt data rows are skipped, not crash."""
        import sqlite3
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        # Insert corrupt row directly
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO races (id, state, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("bad", "racing", "NOT JSON{{{", 1.0, 1.0),
            )
        active = store.load_active()
        assert "bad" not in active


class TestPersistence:
    """Tests that FleetRace persists state to SQLite."""

    def test_race_persisted_on_start(self, tmp_path):
        db = str(tmp_path / "races.db")
        r = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        r._daemon_get = MagicMock(return_value={"status": "ok", "channel_health": {}})
        r._daemon_post = MagicMock(side_effect=_mock_post)

        result = r.start("Persist test", nodes=["mac1"])
        rid = result["race_id"]

        # Verify it's in the DB
        store = _RaceStore(db_path=db)
        all_races = store.load_all()
        assert rid in all_races
        assert all_races[rid]["task"] == "Persist test"

    def test_recover_races_on_init(self, tmp_path):
        db = str(tmp_path / "races.db")

        # Create a race in a first instance
        r1 = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        r1._daemon_get = MagicMock(return_value={"status": "ok", "channel_health": {}})
        r1._daemon_post = MagicMock(side_effect=_mock_post)
        result = r1.start("Recover test", nodes=["mac1"])
        rid = result["race_id"]

        # Create a new instance (simulates daemon restart)
        r2 = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        assert rid in r2._races
        assert r2._races[rid]["task"] == "Recover test"

    def test_failed_race_not_recovered(self, tmp_path):
        db = str(tmp_path / "races.db")
        store = _RaceStore(db_path=db)
        store.save({"race_id": "old", "status": "done", "task": "finished"})

        r = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        assert "old" not in r._races

    def test_persist_on_submit_result(self, tmp_path):
        db = str(tmp_path / "races.db")
        r = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        r._daemon_get = MagicMock(return_value={"status": "ok", "channel_health": {}})
        r._daemon_post = MagicMock(side_effect=_mock_post)

        result = r.start("Submit test", nodes=["mac1"])
        rid = result["race_id"]

        r.submit_result(rid, "mac1", {"summary": "Done"})

        # Check DB was updated
        store = _RaceStore(db_path=db)
        all_races = store.load_all()
        assert all_races[rid]["participants"]["mac1"]["status"] == "completed"

    def test_persist_on_timeout(self, tmp_path):
        db = str(tmp_path / "races.db")
        r = FleetRace(daemon_port=8855, node_id="mac1", db_path=db)
        r._daemon_get = MagicMock(return_value={"status": "ok", "channel_health": {}})
        r._daemon_post = MagicMock(side_effect=_mock_post)

        result = r.start("Timeout test", nodes=["mac1", "mac2"], timeout_s=1)
        rid = result["race_id"]
        r._races[rid]["started_at"] = time.time() - 10

        r.status(rid)

        store = _RaceStore(db_path=db)
        all_races = store.load_all()
        assert all_races[rid]["status"] == "timed_out"
