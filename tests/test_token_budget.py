"""Tests for TokenBudget — consumption tracking, warn levels, persistence."""

import json
import time
from unittest.mock import MagicMock

import pytest

from multifleet.token_budget import BudgetLevel, TokenBudget


class TestBudgetLevels:
    """Verify GREEN/YELLOW/RED/CRITICAL thresholds."""

    def test_green_under_50_pct(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(20_000, 5_000)  # 25% of 100k
        assert tb.get_status() == BudgetLevel.GREEN

    def test_yellow_at_50_pct(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(40_000, 10_000)  # 50%
        assert tb.get_status() == BudgetLevel.YELLOW

    def test_red_at_80_pct(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(60_000, 20_000)  # 80%
        assert tb.get_status() == BudgetLevel.RED

    def test_critical_at_95_pct(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(80_000, 16_000)  # 96%
        assert tb.get_status() == BudgetLevel.CRITICAL

    def test_green_at_zero(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        assert tb.get_status() == BudgetLevel.GREEN


class TestRecordTurn:
    """Verify turn recording and accumulation."""

    def test_accumulates_tokens(self, tmp_path):
        tb = TokenBudget(weekly_limit=1_000_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(10_000, 2_000)
        tb.record_turn(15_000, 3_000)
        assert tb.total_tokens_this_week() == 30_000

    def test_returns_current_level(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        level = tb.record_turn(90_000, 6_000)
        assert level == BudgetLevel.CRITICAL

    def test_model_recorded(self, tmp_path):
        tb = TokenBudget(weekly_limit=1_000_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(1000, 500, model="claude-opus-4-5")
        assert tb._turns[-1].model == "claude-opus-4-5"


class TestEstimateRemaining:
    """Verify projection calculations."""

    def test_remaining_tokens(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(30_000, 10_000)
        est = tb.estimate_remaining()
        assert est["remaining_tokens"] == 60_000

    def test_custom_weekly_limit(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(30_000, 10_000)
        est = tb.estimate_remaining(weekly_limit=200_000)
        assert est["remaining_tokens"] == 160_000

    def test_turns_until_limit(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        # Two turns of 10k each = avg 10k/turn, 80k remaining
        tb.record_turn(5_000, 5_000)
        tb.record_turn(5_000, 5_000)
        est = tb.estimate_remaining()
        assert est["turns_until_limit"] == 8  # 80k / 10k


class TestShouldSwitchModel:
    def test_false_when_green(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(10_000, 5_000)
        assert tb.should_switch_model() is False

    def test_true_when_red(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(60_000, 25_000)
        assert tb.should_switch_model() is True


class TestPersistence:
    """Verify save/load across instances."""

    def test_json_roundtrip(self, tmp_path):
        path = tmp_path / "budget.json"
        tb1 = TokenBudget(weekly_limit=100_000, persistence_path=path)
        tb1.record_turn(30_000, 10_000)

        # New instance loads from same file
        tb2 = TokenBudget(weekly_limit=100_000, persistence_path=path)
        assert tb2.total_tokens_this_week() == 40_000
        assert tb2.get_status() == BudgetLevel.GREEN

    def test_redis_roundtrip(self, tmp_path):
        mock_redis = MagicMock()
        store = {}

        def mock_set(key, val, ex=None):
            store[key] = val

        def mock_get(key):
            return store.get(key)

        mock_redis.set = mock_set
        mock_redis.get = mock_get

        tb1 = TokenBudget(
            weekly_limit=100_000,
            redis_client=mock_redis,
            persistence_path=tmp_path / "s.json",
        )
        tb1.record_turn(50_000, 10_000)

        tb2 = TokenBudget(
            weekly_limit=100_000,
            redis_client=mock_redis,
            persistence_path=tmp_path / "s.json",
        )
        assert tb2.total_tokens_this_week() == 60_000

    def test_redis_failure_falls_back_to_file(self, tmp_path):
        mock_redis = MagicMock()
        mock_redis.set.side_effect = ConnectionError("Redis down")
        mock_redis.get.side_effect = ConnectionError("Redis down")

        path = tmp_path / "budget.json"
        tb = TokenBudget(
            weekly_limit=100_000,
            redis_client=mock_redis,
            persistence_path=path,
        )
        tb.record_turn(20_000, 5_000)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["status"] == "GREEN"


class TestSnapshot:
    def test_snapshot_keys(self, tmp_path):
        tb = TokenBudget(weekly_limit=100_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(10_000, 5_000)
        snap = tb.get_snapshot()
        expected_keys = {
            "status", "usage_fraction", "total_tokens_this_week",
            "weekly_limit", "turn_count_this_week", "burn_rate_per_hour",
            "hours_until_limit", "turns_until_limit",
            "should_switch_model", "timestamp",
        }
        assert expected_keys.issubset(snap.keys())
        assert snap["status"] == "GREEN"
        assert snap["total_tokens_this_week"] == 15_000

    def test_usage_fraction_accuracy(self, tmp_path):
        tb = TokenBudget(weekly_limit=200_000, persistence_path=tmp_path / "s.json")
        tb.record_turn(50_000, 50_000)
        assert tb.usage_fraction() == pytest.approx(0.5, abs=0.01)
