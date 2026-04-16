"""Tests for FleetMemoryFeedback -- repair outcomes wired into memory pipeline."""

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure multifleet package is importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset lazy-load state between tests."""
    import multifleet.memory_feedback as mf
    mf._auto_capture = None
    mf._bugfix_tracker = None
    mf._imports_attempted = False
    mf._singleton = None
    yield


@pytest.fixture
def mock_auto_capture():
    """Provide a mock auto_capture module."""
    mock = types.ModuleType("memory.auto_capture")
    mock.capture_success = MagicMock(return_value="session-123")
    mock.capture_failure = MagicMock(return_value="claim-456")
    return mock


@pytest.fixture
def mock_bugfix_tracker():
    """Provide a mock bugfix_tracker module."""
    mock = types.ModuleType("memory.bugfix_tracker")
    mock.record_bugfix_passed = MagicMock(return_value={"status": "passed"})
    mock.record_bugfix_failed = MagicMock(return_value=({"status": "failed"}, False))
    return mock


@pytest.fixture
def feedback_with_mocks(mock_auto_capture, mock_bugfix_tracker):
    """FleetMemoryFeedback with injected mock memory modules."""
    import multifleet.memory_feedback as mf
    mf._auto_capture = mock_auto_capture
    mf._bugfix_tracker = mock_bugfix_tracker
    mf._imports_attempted = True
    return mf.FleetMemoryFeedback()


class TestRecordRepair:
    def test_successful_repair_calls_capture_success(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        result = fb.record_repair(
            channel="P1_nats", action="restart_nats",
            success=True, details="reconnected", target="mac2", level=1,
        )
        assert result == "session-123"
        mock_auto_capture.capture_success.assert_called_once()
        call_kw = mock_auto_capture.capture_success.call_args
        assert "P1_nats" in call_kw.kwargs.get("task", "") or "P1_nats" in str(call_kw)
        assert call_kw.kwargs["area"] == "fleet"

    def test_failed_repair_calls_capture_failure(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        result = fb.record_repair(
            channel="P2_http", action="restart_tunnel",
            success=False, details="timeout", target="mac3", level=3,
        )
        assert result == "claim-456"
        mock_auto_capture.capture_failure.assert_called_once()
        call_kw = mock_auto_capture.capture_failure.call_args
        assert call_kw.kwargs["area"] == "fleet"
        assert "P2_http" in call_kw.kwargs.get("root_cause", "")

    def test_successful_repair_records_bugfix_passed(self, feedback_with_mocks, mock_bugfix_tracker):
        fb = feedback_with_mocks
        fb.record_repair(
            channel="P1_nats", action="restart_nats",
            success=True, target="mac2",
        )
        mock_bugfix_tracker.record_bugfix_passed.assert_called_once()
        args = mock_bugfix_tracker.record_bugfix_passed.call_args
        assert "P1_nats" in args.kwargs["title"]

    def test_failed_repair_records_bugfix_failed(self, feedback_with_mocks, mock_bugfix_tracker):
        fb = feedback_with_mocks
        fb.record_repair(
            channel="P1_nats", action="restart_nats",
            success=False, details="connection refused", target="mac2",
        )
        mock_bugfix_tracker.record_bugfix_failed.assert_called_once()

    def test_updates_channel_pattern_on_failure(self, feedback_with_mocks):
        fb = feedback_with_mocks
        fb.record_repair(channel="P1_nats", action="x", success=False)
        patterns = fb.get_channel_patterns()
        assert patterns["P1_nats"]["failure_count"] == 1
        assert patterns["P1_nats"]["consecutive_failures"] == 1

    def test_resets_consecutive_on_success(self, feedback_with_mocks):
        fb = feedback_with_mocks
        fb.record_repair(channel="P1_nats", action="x", success=False)
        fb.record_repair(channel="P1_nats", action="x", success=False)
        fb.record_repair(channel="P1_nats", action="x", success=True)
        patterns = fb.get_channel_patterns()
        assert patterns["P1_nats"]["consecutive_failures"] == 0
        assert patterns["P1_nats"]["failure_count"] == 2  # total stays


class TestRecordHealthTransition:
    def test_recovery_calls_capture_success(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        result = fb.record_health_transition(
            node="mac2", old_status="offline", new_status="healthy",
        )
        assert result == "session-123"
        mock_auto_capture.capture_success.assert_called_once()

    def test_degradation_calls_capture_failure(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        result = fb.record_health_transition(
            node="mac2", old_status="healthy", new_status="degraded",
            broken_channels=["P1_nats"],
        )
        assert result == "claim-456"
        mock_auto_capture.capture_failure.assert_called_once()

    def test_intermediate_transition_not_recorded(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        result = fb.record_health_transition(
            node="mac2", old_status="offline", new_status="degraded",
        )
        assert result is None
        mock_auto_capture.capture_success.assert_not_called()
        mock_auto_capture.capture_failure.assert_not_called()

    def test_broken_channels_in_details(self, feedback_with_mocks, mock_auto_capture):
        fb = feedback_with_mocks
        fb.record_health_transition(
            node="mac3", old_status="healthy", new_status="offline",
            broken_channels=["P1_nats", "P2_http"],
        )
        call_kw = mock_auto_capture.capture_failure.call_args
        assert "P1_nats" in call_kw.kwargs.get("error", "")


class TestRecordChannelPattern:
    def test_below_threshold_no_bugfix(self, feedback_with_mocks, mock_bugfix_tracker):
        fb = feedback_with_mocks
        fb.record_channel_pattern("P1_nats", failure_count=2, last_failure=1000.0)
        mock_bugfix_tracker.record_bugfix_failed.assert_not_called()

    def test_above_threshold_records_bugfix(self, feedback_with_mocks, mock_bugfix_tracker):
        fb = feedback_with_mocks
        fb.record_channel_pattern("P1_nats", failure_count=3, last_failure=1000.0)
        mock_bugfix_tracker.record_bugfix_failed.assert_called_once()
        args = mock_bugfix_tracker.record_bugfix_failed.call_args
        assert "flaky" in args.kwargs["title"]

    def test_auto_triggers_at_consecutive_threshold(self, feedback_with_mocks, mock_bugfix_tracker):
        fb = feedback_with_mocks
        # 3 consecutive failures should auto-trigger record_channel_pattern
        fb.record_repair(channel="P3_chief", action="x", success=False)
        fb.record_repair(channel="P3_chief", action="x", success=False)
        fb.record_repair(channel="P3_chief", action="x", success=False)
        # At least one call to record_bugfix_failed for the flaky pattern
        assert any(
            "flaky" in str(call)
            for call in mock_bugfix_tracker.record_bugfix_failed.call_args_list
        )


class TestGracefulDegradation:
    def test_no_memory_modules_does_not_raise(self):
        """When memory modules are unavailable, methods return None silently."""
        import multifleet.memory_feedback as mf
        mf._auto_capture = None
        mf._bugfix_tracker = None
        mf._imports_attempted = True
        fb = mf.FleetMemoryFeedback()

        # None of these should raise
        assert fb.record_repair("P1_nats", "restart", True) is None
        assert fb.record_repair("P1_nats", "restart", False) is None
        assert fb.record_health_transition("mac2", "healthy", "offline") is None
        fb.record_channel_pattern("P1_nats", 5, 1000.0)  # no-op

    def test_memory_module_exception_does_not_propagate(self, mock_auto_capture):
        """If a memory module raises, it should not propagate."""
        import multifleet.memory_feedback as mf
        mock_auto_capture.capture_success = MagicMock(side_effect=RuntimeError("boom"))
        mf._auto_capture = mock_auto_capture
        mf._bugfix_tracker = None
        mf._imports_attempted = True
        fb = mf.FleetMemoryFeedback()

        # Should not raise despite capture_success throwing
        result = fb.record_repair("P1_nats", "restart", True)
        assert result is None


class TestSingleton:
    def test_get_feedback_returns_same_instance(self):
        from multifleet.memory_feedback import get_feedback
        a = get_feedback()
        b = get_feedback()
        assert a is b


class TestChannelPatterns:
    def test_get_channel_patterns_returns_copy(self, feedback_with_mocks):
        fb = feedback_with_mocks
        fb.record_repair("P1_nats", "x", success=False)
        p1 = fb.get_channel_patterns()
        p2 = fb.get_channel_patterns()
        assert p1 is not p2  # defensive copy
        assert p1 == p2
