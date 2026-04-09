"""Tests for SuperpowersAdapter — workflow phase gating of fleet races."""

import time

import pytest

from multifleet.superpowers_adapter import SuperpowersAdapter, get_adapter


class TestPhaseBlocking:
    """Races are blocked during design/brainstorming phases."""

    @pytest.mark.parametrize("phase", [
        "signal", "brainstorming", "writing-plans",
        "inbox_capture", "surgeon_adjudication", "chief_escalation", "vision_lock",
    ])
    def test_blocking_phases_prevent_dispatch(self, phase):
        adapter = SuperpowersAdapter()
        adapter.update_phase(phase, source="test")
        ok, reason = adapter.can_dispatch_race()
        assert ok is False
        assert phase in reason

    @pytest.mark.parametrize("phase", [
        "executing-plans", "done",
        "execution", "review", "fix_loop", "complete",
    ])
    def test_execution_phases_allow_dispatch(self, phase):
        adapter = SuperpowersAdapter()
        adapter.update_phase(phase, source="test")
        ok, reason = adapter.can_dispatch_race()
        assert ok is True


class TestUnknownPhase:
    """Unknown/default phase should allow dispatch (graceful degradation)."""

    def test_unknown_phase_allows_dispatch(self):
        adapter = SuperpowersAdapter()
        ok, reason = adapter.can_dispatch_race()
        assert ok is True
        assert "unknown" in reason

    def test_arbitrary_phase_allows_dispatch(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("some-new-phase")
        ok, reason = adapter.can_dispatch_race()
        assert ok is True


class TestPhaseTransitions:
    """Phase transitions trigger appropriate behavior."""

    def test_transition_to_execution_clears_queue(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        adapter.queue_race({"task": "fix auth"})
        adapter.queue_race({"task": "add tests"})
        assert len(adapter.get_queued_races()) == 2

        adapter.update_phase("executing-plans")
        assert len(adapter.get_queued_races()) == 0

    def test_transition_between_blocking_phases_keeps_queue(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("signal")
        adapter.queue_race({"task": "fix auth"})
        adapter.update_phase("brainstorming")
        assert len(adapter.get_queued_races()) == 1

    def test_same_phase_update_no_transition(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        adapter.queue_race({"task": "fix auth"})
        adapter.update_phase("brainstorming")  # no change
        assert len(adapter.get_queued_races()) == 1


class TestQueueing:
    """Races queued during blocking phase."""

    def test_queue_race_stores_info(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        adapter.queue_race({"task": "fix auth", "nodes": ["mac1", "mac2"]})
        queued = adapter.get_queued_races()
        assert len(queued) == 1
        assert queued[0]["task"] == "fix auth"
        assert "queued_at" in queued[0]


class TestGetState:
    """Dashboard state reporting."""

    def test_state_includes_all_fields(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming", source="webhook")
        state = adapter.get_state()
        assert state["current_phase"] == "brainstorming"
        assert state["phase_source"] == "webhook"
        assert state["can_dispatch"] is False
        assert state["queued_races"] == 0
        assert state["updated_at"] > 0

    def test_state_reflects_execution_phase(self):
        adapter = SuperpowersAdapter()
        adapter.update_phase("executing-plans")
        state = adapter.get_state()
        assert state["can_dispatch"] is True


class TestSingleton:
    """Module-level singleton accessor."""

    def test_get_adapter_returns_same_instance(self):
        # Reset singleton for isolation
        import multifleet.superpowers_adapter as mod
        mod._adapter = None
        a1 = get_adapter()
        a2 = get_adapter()
        assert a1 is a2
        mod._adapter = None  # cleanup


class TestFleetRaceIntegration:
    """Verify FleetRace.start() respects the adapter."""

    def test_race_blocked_during_brainstorming(self):
        """FleetRace.start() should return blocked status when adapter blocks."""
        import multifleet.superpowers_adapter as mod
        mod._adapter = None
        adapter = get_adapter()
        adapter.update_phase("brainstorming", source="test")

        from multifleet.fleet_race import FleetRace
        race = FleetRace(daemon_port=19999, node_id="test-node")
        result = race.start("test task", nodes=["mac1"])
        assert result["status"] == "blocked_by_workflow"
        assert result["queued"] is True
        assert result["race_id"] is None

        # Cleanup
        mod._adapter = None

    def test_race_allowed_during_execution(self):
        """FleetRace.start() should proceed when adapter allows."""
        import multifleet.superpowers_adapter as mod
        mod._adapter = None
        adapter = get_adapter()
        adapter.update_phase("executing-plans", source="test")

        from multifleet.fleet_race import FleetRace
        race = FleetRace(daemon_port=19999, node_id="test-node")
        # Will fail at node discovery (no daemon) but should get past the adapter check
        result = race.start("test task", nodes=["mac1"], run_probes=False)
        # Should NOT be blocked_by_workflow
        assert result.get("status") != "blocked_by_workflow"
        assert result.get("race_id") is not None

        # Cleanup
        mod._adapter = None
