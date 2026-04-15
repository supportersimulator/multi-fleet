"""Tests for coordinator_surgeon_proxy.py — 3-Surgeons proxy layer."""

import asyncio
import time

import pytest

from multifleet.coordinator_surgeon_proxy import (
    SurgeonAvailability,
    SurgeonProxy,
    SurgicalOutcome,
)


# ── Data Classes ──

class TestSurgeonAvailability:
    def test_to_dict(self):
        sa = SurgeonAvailability(name="cardiologist", available=True)
        d = sa.to_dict()
        assert d["name"] == "cardiologist"
        assert d["available"] is True

    def test_defaults(self):
        sa = SurgeonAvailability(name="test")
        assert sa.available is False
        assert sa.consecutive_failures == 0


class TestSurgicalOutcome:
    def test_to_dict(self):
        so = SurgicalOutcome(
            operation="cardio_review",
            topic="test topic",
            surgeons_involved=["cardiologist"],
            result={"status": "ok", "output": "all good"},
            success=True,
            duration_ms=150.5,
        )
        d = so.to_dict()
        assert d["operation"] == "cardio_review"
        assert d["success"] is True
        assert d["duration_ms"] == 150.5
        assert "status" in d["result_keys"]

    def test_empty_result(self):
        so = SurgicalOutcome(
            operation="test", topic="t", surgeons_involved=[],
            result={}, success=False, error="fail",
        )
        d = so.to_dict()
        assert d["result_keys"] == []


# ── SurgeonProxy ──

class TestSurgeonProxy:
    def test_init(self):
        sp = SurgeonProxy(node_id="mac1")
        assert sp.node_id == "mac1"
        assert sp.probe_interval_s == 60.0
        assert len(sp._availability) == 3

    def test_get_availability(self):
        sp = SurgeonProxy(node_id="mac1")
        avail = sp.get_availability()
        assert "atlas" in avail
        assert "cardiologist" in avail
        assert "neurologist" in avail

    def test_get_available_count_initial(self):
        sp = SurgeonProxy(node_id="mac1")
        # Initially none are probed so all False
        assert sp.get_available_count() == 0

    def test_recent_outcomes_empty(self):
        sp = SurgeonProxy(node_id="mac1")
        assert sp.recent_outcomes() == []

    @pytest.mark.asyncio
    async def test_probe_availability(self, monkeypatch):
        """Probe should mark atlas as available, cardiologist based on env."""
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        sp = SurgeonProxy(node_id="mac-test")
        avail = await sp.probe_availability()

        # Atlas always available
        assert avail["atlas"].available is True
        # Cardiologist: no key
        assert avail["cardiologist"].available is False
        # Neurologist: probably no local LLM in test
        # (don't assert — depends on test environment)

    @pytest.mark.asyncio
    async def test_probe_cardiologist_with_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        sp = SurgeonProxy(node_id="mac-test")
        avail = await sp.probe_availability()
        assert avail["cardiologist"].available is True

    @pytest.mark.asyncio
    async def test_cardio_review_no_bridge(self):
        """Should return failed outcome when bridge is unavailable."""
        sp = SurgeonProxy(node_id="mac-test")
        sp._bridge = None  # force no bridge
        # Monkey-patch _get_bridge to return None
        sp._get_bridge = lambda: None

        outcome = await sp.cardio_review("test topic")
        assert not outcome.success
        assert "unavailable" in outcome.error.lower()
        assert sp.stats["operations_failed"] == 1

    @pytest.mark.asyncio
    async def test_consensus_no_bridge(self):
        sp = SurgeonProxy(node_id="mac-test")
        sp._get_bridge = lambda: None

        outcome = await sp.consensus("test claim")
        assert not outcome.success

    @pytest.mark.asyncio
    async def test_neurologist_challenge_no_bridge(self):
        sp = SurgeonProxy(node_id="mac-test")
        sp._get_bridge = lambda: None

        outcome = await sp.neurologist_challenge("test topic")
        assert not outcome.success

    @pytest.mark.asyncio
    async def test_run_operation_with_mock_bridge(self):
        sp = SurgeonProxy(node_id="mac-test")
        sp._bridge = {
            "cardio_review": lambda topic, **kw: {"status": "ok", "output": "reviewed"},
            "neurologist_challenge": lambda topic, **kw: {"status": "ok", "output": "challenged"},
            "consensus": lambda topic, **kw: {"status": "ok", "output": "consensus reached"},
            "bridge_status": lambda: {"status": "ok"},
        }
        sp._get_bridge = lambda: sp._bridge

        outcome = await sp.cardio_review("architecture question")
        assert outcome.success
        assert outcome.operation == "cardio_review"
        assert sp.stats["operations_success"] == 1

    @pytest.mark.asyncio
    async def test_outcome_recording(self):
        sp = SurgeonProxy(node_id="mac-test")
        sp._get_bridge = lambda: None

        await sp.cardio_review("t1")
        await sp.consensus("t2")
        outcomes = sp.recent_outcomes()
        assert len(outcomes) == 2
        assert outcomes[0]["topic"] == "t1"

    @pytest.mark.asyncio
    async def test_outcome_eviction(self):
        sp = SurgeonProxy(node_id="mac-test")
        sp._max_outcomes = 5
        sp._get_bridge = lambda: None

        for i in range(8):
            await sp.cardio_review(f"topic-{i}")
        assert len(sp._outcomes) == 5

    @pytest.mark.asyncio
    async def test_start_stop(self):
        sp = SurgeonProxy(node_id="mac-test", probe_interval_s=1.0)
        await sp.start()
        assert sp._probe_task is not None
        await asyncio.sleep(0.1)
        await sp.stop()
        assert sp._probe_task.done()
