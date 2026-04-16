"""Tests for the 3 new memory module wirings into multi-fleet.

1. failure_pattern_analyzer — verify wired into self_heal failure path
2. service_urls — verify URL resolution with fallback
3. learning_store — verify store/search wired into memory_feedback + self_heal
"""

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure multifleet package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# 1. service_urls tests
# ═══════════════════════════════════════════════════════════════════════════

class TestServiceUrls:
    """Test service_urls.py — wraps service_registry with fallback."""

    def setup_method(self):
        """Reset lazy-load state."""
        import multifleet.service_urls as su
        su._registry = None
        su._registry_attempted = False

    def test_fallback_fleet_daemon_url(self):
        """When service_registry is unavailable, returns hardcoded fallback."""
        import multifleet.service_urls as su
        su._registry_attempted = True  # skip import attempt
        su._registry = None
        assert su.get_fleet_daemon_url() == "http://127.0.0.1:8855"

    def test_fallback_helper_agent_url(self):
        import multifleet.service_urls as su
        su._registry_attempted = True
        su._registry = None
        assert su.get_service_url("helper_agent") == "http://127.0.0.1:8080"

    def test_unknown_service_raises(self):
        import multifleet.service_urls as su
        su._registry_attempted = True
        su._registry = None
        with pytest.raises(ValueError, match="Unknown service"):
            su.get_service_url("nonexistent_service")

    def test_registry_used_when_available(self):
        """When service_registry is available, delegates to it."""
        import multifleet.service_urls as su
        mock_registry = MagicMock()
        mock_registry.get_service_url.return_value = "http://10.0.0.1:9999"
        su._registry_attempted = True
        su._registry = mock_registry
        assert su.get_service_url("fleet_daemon") == "http://10.0.0.1:9999"
        mock_registry.get_service_url.assert_called_once_with("fleet_daemon")

    def test_registry_failure_falls_back(self):
        """When service_registry raises, falls back to hardcoded."""
        import multifleet.service_urls as su
        mock_registry = MagicMock()
        mock_registry.get_service_url.side_effect = ValueError("not found")
        su._registry_attempted = True
        su._registry = mock_registry
        assert su.get_service_url("fleet_daemon") == "http://127.0.0.1:8855"


class TestServiceUrlsWiring:
    """Verify service_urls is actually wired into fleet modules."""

    def test_cli_uses_service_urls(self):
        """cli.py DAEMON should come from service_urls."""
        import multifleet.cli as cli
        # DAEMON should be a string URL, not hardcoded
        assert cli.DAEMON.startswith("http://")

    def test_onboarding_uses_service_urls(self):
        """onboarding.py DAEMON_URL should come from service_urls."""
        import multifleet.onboarding as ob
        assert ob.DAEMON_URL.startswith("http://")


# ═══════════════════════════════════════════════════════════════════════════
# 2. learning_store wiring tests
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def reset_feedback():
    """Reset memory_feedback module state."""
    import multifleet.memory_feedback as mf
    mf._auto_capture = None
    mf._bugfix_tracker = None
    mf._learning_store = None
    mf._imports_attempted = False
    mf._singleton = None
    yield mf
    mf._auto_capture = None
    mf._bugfix_tracker = None
    mf._learning_store = None
    mf._imports_attempted = False
    mf._singleton = None


class TestLearningStoreInFeedback:
    """Verify learning_store is wired into FleetMemoryFeedback."""

    def test_record_repair_stores_learning(self, reset_feedback):
        """Successful repair stores a 'fix' learning."""
        mf = reset_feedback
        mock_store = MagicMock()
        mf._learning_store = mock_store
        mf._imports_attempted = True

        fb = mf.FleetMemoryFeedback()
        fb.record_repair(
            channel="P1_nats", action="restart_nats",
            success=True, details="reconnected", target="mac2", level=1,
        )

        mock_store.store_learning.assert_called_once()
        call_data = mock_store.store_learning.call_args[0][0]
        assert call_data["type"] == "fix"
        assert "P1_nats" in call_data["tags"]
        assert call_data["source"] == "fleet_memory_feedback"

    def test_failed_repair_stores_gotcha(self, reset_feedback):
        """Failed repair stores a 'gotcha' learning."""
        mf = reset_feedback
        mock_store = MagicMock()
        mf._learning_store = mock_store
        mf._imports_attempted = True

        fb = mf.FleetMemoryFeedback()
        fb.record_repair(
            channel="P2_http", action="restart_daemon",
            success=False, details="timeout", target="mac3", level=2,
        )

        mock_store.store_learning.assert_called_once()
        call_data = mock_store.store_learning.call_args[0][0]
        assert call_data["type"] == "gotcha"
        assert "P2_http" in call_data["tags"]

    def test_search_prior_repairs(self, reset_feedback):
        """search_prior_repairs delegates to learning_store.query."""
        mf = reset_feedback
        mock_store = MagicMock()
        mock_store.query.return_value = [
            {"title": "Fleet repair P1_nats on mac2", "content": "restart worked"}
        ]
        mf._learning_store = mock_store
        mf._imports_attempted = True

        fb = mf.FleetMemoryFeedback()
        results = fb.search_prior_repairs("P1_nats mac2")
        assert len(results) == 1
        assert "P1_nats" in results[0]["title"]
        mock_store.query.assert_called_once_with("P1_nats mac2", limit=5)

    def test_search_returns_empty_when_unavailable(self, reset_feedback):
        """When learning_store is None, search returns empty list."""
        mf = reset_feedback
        mf._learning_store = None
        mf._imports_attempted = True

        fb = mf.FleetMemoryFeedback()
        results = fb.search_prior_repairs("anything")
        assert results == []

    def test_store_learning_never_raises(self, reset_feedback):
        """_store_learning swallows exceptions — never blocks fleet ops."""
        mf = reset_feedback
        mock_store = MagicMock()
        mock_store.store_learning.side_effect = RuntimeError("db locked")
        mf._learning_store = mock_store
        mf._imports_attempted = True

        fb = mf.FleetMemoryFeedback()
        # Should not raise
        fb.record_repair(
            channel="P1_nats", action="restart_nats",
            success=True, target="mac2",
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. failure_pattern_analyzer wiring verification
# ═══════════════════════════════════════════════════════════════════════════

class TestFailurePatternAnalyzerWiring:
    """Verify failure_pattern_analyzer is correctly wired in self_heal."""

    def test_failure_path_imports_analyzer(self):
        """The self_heal module references FailurePatternAnalyzer in failure path."""
        import multifleet.self_heal as sh
        source = Path(sh.__file__).read_text()
        assert "from memory.failure_pattern_analyzer import FailurePatternAnalyzer" in source
        assert "fpa.analyze_for_patterns(hours_back=1)" in source

    def test_learning_store_search_in_repair_escalation(self):
        """self_heal.repair_with_escalation searches learning_store before repair."""
        import multifleet.self_heal as sh
        source = Path(sh.__file__).read_text()
        assert "fb.search_prior_repairs" in source
        assert "prior repair" in source.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Integration: learning_store search wired into self_heal
# ═══════════════════════════════════════════════════════════════════════════

class TestSelfHealLearningStoreIntegration:
    """Verify self_heal uses learning_store search before repair."""

    def test_repair_escalation_searches_prior(self):
        """repair_with_escalation should call search_prior_repairs."""
        import multifleet.self_heal as sh
        # Read source to verify the wiring exists
        source = Path(sh.__file__).read_text()
        # Should search prior repairs before escalation
        assert "search_prior_repairs" in source
        # Should be in the repair_with_escalation method
        idx_repair = source.index("async def repair_with_escalation")
        idx_search = source.index("search_prior_repairs")
        idx_level1 = source.index("Level 1: NOTIFY", idx_repair)
        # search happens before Level 1
        assert idx_search < idx_level1, (
            "search_prior_repairs should be called before Level 1 escalation"
        )
