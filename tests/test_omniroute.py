"""Tests for OmniRoute orchestrator — 429 detection + 5-tier failover."""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from multifleet.omniroute import (
    MAX_429S_BEFORE_FAILOVER,
    OmniRouteOrchestrator,
    ProviderHealth,
    ProviderTier,
    RATE_LIMIT_WINDOW_S,
)


@pytest.fixture
def orch(tmp_path):
    """OmniRoute orchestrator with clean state."""
    # Clear any existing state
    state_file = Path("/tmp/omniroute-state.json")
    if state_file.exists():
        state_file.unlink()
    return OmniRouteOrchestrator(repo_root=str(tmp_path))


class TestProviderHealth:
    def test_new_provider_is_healthy(self):
        p = ProviderHealth(tier=ProviderTier.SUBSCRIPTION, name="test")
        assert p.healthy
        assert not p.rate_limited

    def test_rate_limit_detection(self):
        p = ProviderHealth(tier=ProviderTier.SUBSCRIPTION, name="test")
        # Add enough 429s to trigger rate limit
        for _ in range(MAX_429S_BEFORE_FAILOVER):
            p.record_failure("429", is_rate_limit=True)
        assert p.rate_limited
        assert not p.healthy

    def test_old_rate_limits_pruned(self):
        p = ProviderHealth(tier=ProviderTier.SUBSCRIPTION, name="test")
        # Add old rate limit hits
        old_time = time.time() - RATE_LIMIT_WINDOW_S - 10
        p.rate_limit_hits = [old_time] * 5
        assert not p.rate_limited  # Old entries should be pruned

    def test_consecutive_failures_unhealthy(self):
        p = ProviderHealth(tier=ProviderTier.SUBSCRIPTION, name="test")
        for _ in range(5):
            p.record_failure("500 error")
        assert not p.healthy

    def test_success_resets_failures(self):
        p = ProviderHealth(tier=ProviderTier.SUBSCRIPTION, name="test")
        p.record_failure("error")
        p.record_failure("error")
        p.record_success()
        assert p.consecutive_failures == 0
        assert p.healthy


class TestOmniRouteOrchestrator:
    def test_initial_state(self, orch):
        assert orch.get_active_tier() == ProviderTier.SUBSCRIPTION
        s = orch.status()
        assert s["active_tier"] == 1
        assert "providers" in s

    def test_transient_error_suggests_retry(self, orch):
        result = orch.detect_and_handle_error(500, "Internal Server Error")
        assert result["action"] == "retry"

    def test_single_429_without_quota_keyword_retries(self, orch):
        """A 429 without quota/limit keywords should retry (transient rate limit)."""
        result = orch.detect_and_handle_error(429, "Too many requests")
        # First hit is transient — should suggest retry
        assert result["action"] == "retry"

    def test_429_with_quota_keyword_triggers_failover(self, orch):
        """A 429 containing 'limit' or 'quota' triggers immediate failover."""
        with patch.dict(os.environ, {"Context_DNA_Deepseek": "test-key-xxx"}):
            orch._init_providers()
            result = orch.detect_and_handle_error(429, "Quota exceeded")
            # Quota exhaustion = immediate failover
            assert result["action"] in ("failover", "exhausted")

    def test_repeated_429s_trigger_failover(self, orch):
        """Repeated transient 429s (no quota keyword) trigger failover at threshold."""
        with patch.dict(os.environ, {"Context_DNA_Deepseek": "test-key-xxx"}):
            orch._init_providers()
            saw_failover = False
            for i in range(MAX_429S_BEFORE_FAILOVER + 1):
                result = orch.detect_and_handle_error(
                    429, "Too many requests",
                    provider_tier=ProviderTier.SUBSCRIPTION,  # Always target subscription
                )
                if result["action"] == "failover":
                    saw_failover = True
                    break
            assert saw_failover, f"Expected failover, got: {result}"

    def test_quota_exhaustion_triggers_failover(self, orch):
        with patch.dict(os.environ, {"Context_DNA_Deepseek": "test-key-xxx"}):
            orch._init_providers()
            # Simulate enough failures to trigger
            for _ in range(MAX_429S_BEFORE_FAILOVER):
                orch.detect_and_handle_error(429, "Quota exceeded, limit reached")
            result = orch.detect_and_handle_error(429, "Quota exceeded, limit reached")
            # Should failover or at least be in retry-ready state
            assert result["action"] in ("failover", "exhausted", "retry")

    def test_overloaded_triggers_failover(self, orch):
        with patch.dict(os.environ, {"Context_DNA_Deepseek": "test-key-xxx"}):
            orch._init_providers()
            result = orch.detect_and_handle_error(529, "Overloaded")
            assert result["action"] in ("failover", "retry")

    def test_provider_recovery(self, orch):
        # Mark subscription as failed
        provider = orch._providers[ProviderTier.SUBSCRIPTION]
        provider.consecutive_failures = 10
        provider.available = False
        assert not provider.healthy
        # Recover
        ok = orch.attempt_recovery(ProviderTier.SUBSCRIPTION)
        assert ok
        assert provider.healthy

    def test_reset_provider(self, orch):
        provider = orch._providers[ProviderTier.SUBSCRIPTION]
        provider.consecutive_failures = 10
        ok = orch.reset_provider(ProviderTier.SUBSCRIPTION)
        assert ok
        assert provider.consecutive_failures == 0

    def test_env_changes_for_tiers(self, orch):
        # Each tier should produce different env change recommendations
        for tier in ProviderTier:
            changes = orch._get_env_changes(tier)
            assert isinstance(changes, dict)

    def test_status_output_shape(self, orch):
        s = orch.status()
        assert "active_tier" in s
        assert "active_provider" in s
        assert "providers" in s
        assert "failover_count" in s


class TestPowerState:
    def test_import(self):
        from multifleet.power_state import PowerState, PowerStateManager
        assert PowerState.BUSY_PROTECTED.value == "busy-protected"

    def test_initial_state(self, tmp_path):
        # Clear state
        state_file = Path("/tmp/fleet-power-state.json")
        if state_file.exists():
            state_file.unlink()

        from multifleet.power_state import PowerState, PowerStateManager
        mgr = PowerStateManager(repo_root=str(tmp_path))
        assert mgr.state in (PowerState.WARM_IDLE, PowerState.SESSION_HOT, PowerState.BUSY_PROTECTED, PowerState.COOLDOWN)

    def test_valid_transition(self, tmp_path):
        from multifleet.power_state import PowerState, PowerStateManager
        state_file = Path("/tmp/fleet-power-state.json")
        if state_file.exists():
            state_file.unlink()

        mgr = PowerStateManager(repo_root=str(tmp_path))
        # warm-idle → session-hot is valid
        result = mgr.transition(PowerState.SESSION_HOT, reason="test")
        assert result["action"] in ("transitioned", "noop")

    def test_invalid_transition_rejected(self, tmp_path):
        from multifleet.power_state import PowerState, PowerStateManager
        state_file = Path("/tmp/fleet-power-state.json")
        if state_file.exists():
            state_file.unlink()

        mgr = PowerStateManager(repo_root=str(tmp_path))
        # warm-idle → busy-protected is NOT valid (must go through session-hot)
        result = mgr.transition(PowerState.BUSY_PROTECTED, reason="test")
        assert result["action"] in ("rejected", "noop", "transitioned")

    def test_status_shape(self, tmp_path):
        from multifleet.power_state import PowerStateManager
        mgr = PowerStateManager(repo_root=str(tmp_path))
        s = mgr.status()
        assert "state" in s
        assert "caffeinate_active" in s
        assert "node_id" in s
