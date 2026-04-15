"""Tests for coordinator_health.py — health check subsystem."""

import asyncio
import time

import pytest

from multifleet.coordinator_health import (
    HealthChecker,
    LocalHealthSnapshot,
    ServiceHealth,
)


# ── ServiceHealth ──

class TestServiceHealth:
    def test_to_dict(self):
        sh = ServiceHealth(name="redis", healthy=True, latency_ms=5)
        d = sh.to_dict()
        assert d["name"] == "redis"
        assert d["healthy"] is True
        assert d["latency_ms"] == 5

    def test_defaults(self):
        sh = ServiceHealth(name="test", healthy=False)
        assert sh.consecutive_failures == 0
        assert sh.error == ""
        assert sh.last_check == 0.0


# ── LocalHealthSnapshot ──

class TestLocalHealthSnapshot:
    def test_to_dict_empty(self):
        snap = LocalHealthSnapshot(node_id="mac1", timestamp=time.time())
        d = snap.to_dict()
        assert d["node_id"] == "mac1"
        assert d["overall_healthy"] is True
        assert d["services"] == {}

    def test_to_dict_with_services(self):
        snap = LocalHealthSnapshot(
            node_id="mac2",
            timestamp=time.time(),
            services={
                "redis": ServiceHealth(name="redis", healthy=True),
                "llm": ServiceHealth(name="llm", healthy=False, error="timeout"),
            },
            overall_healthy=False,
            degraded_services=["llm"],
        )
        d = snap.to_dict()
        assert d["overall_healthy"] is False
        assert "llm" in d["degraded_services"]
        assert d["services"]["redis"]["healthy"] is True
        assert d["services"]["llm"]["healthy"] is False


# ── HealthChecker ──

class TestHealthChecker:
    def test_init(self):
        hc = HealthChecker(node_id="mac1")
        assert hc.node_id == "mac1"
        assert hc.check_interval_s == 30.0
        assert hc.latest is None

    def test_custom_ports(self):
        hc = HealthChecker(
            node_id="mac2",
            redis_port=6380,
            llm_port=5045,
            daemon_port=8856,
        )
        assert hc._redis_port == 6380
        assert hc._llm_port == 5045
        assert hc._daemon_port == 8856

    @pytest.mark.asyncio
    async def test_check_all_returns_snapshot(self):
        """check_all should return a LocalHealthSnapshot even when services are down."""
        hc = HealthChecker(
            node_id="mac-test",
            # Use unlikely ports so checks fail fast
            redis_port=19999,
            llm_port=19998,
            webhook_port=19997,
            daemon_port=19996,
            nats_port=19995,
        )
        snapshot = await hc.check_all()
        assert isinstance(snapshot, LocalHealthSnapshot)
        assert snapshot.node_id == "mac-test"
        assert snapshot.timestamp > 0
        # Services that require network will fail, but the check itself shouldn't crash
        assert len(snapshot.services) > 0

    @pytest.mark.asyncio
    async def test_check_all_tracks_failures(self):
        """Consecutive failures should increment."""
        hc = HealthChecker(
            node_id="mac-test",
            redis_port=19999,
            services=("redis",),
        )
        snap1 = await hc.check_all()
        redis1 = snap1.services["redis"]
        assert redis1.consecutive_failures >= 1

        snap2 = await hc.check_all()
        redis2 = snap2.services["redis"]
        assert redis2.consecutive_failures >= 2

    @pytest.mark.asyncio
    async def test_latest_property(self):
        hc = HealthChecker(node_id="mac-test", services=("surgeon_cardiologist",))
        assert hc.latest is None
        await hc.check_all()
        assert hc.latest is not None
        assert hc.latest.node_id == "mac-test"

    @pytest.mark.asyncio
    async def test_stats(self):
        hc = HealthChecker(node_id="mac-test", services=("surgeon_cardiologist",))
        await hc.check_all()
        assert hc.stats["checks_run"] == 1

    @pytest.mark.asyncio
    async def test_start_stop(self):
        hc = HealthChecker(
            node_id="mac-test",
            check_interval_s=1.0,
            services=("surgeon_cardiologist",),
        )
        await hc.start()
        assert hc._check_task is not None
        await asyncio.sleep(0.1)
        await hc.stop()
        assert hc._check_task.done()

    @pytest.mark.asyncio
    async def test_tcp_probe_unreachable(self):
        hc = HealthChecker(node_id="mac-test")
        result = await hc._tcp_probe("test", "127.0.0.1", 19999, timeout=1.0)
        assert not result.healthy
        assert "unreachable" in result.error

    @pytest.mark.asyncio
    async def test_http_probe_unreachable(self):
        hc = HealthChecker(node_id="mac-test")
        result = await hc._http_probe("test", "http://127.0.0.1:19999/health", timeout=1.0)
        assert not result.healthy

    @pytest.mark.asyncio
    async def test_surgeon_cardiologist_no_key(self, monkeypatch):
        """Cardiologist check should fail when no API keys are set."""
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        hc = HealthChecker(node_id="mac-test")
        result = await hc._check_surgeon_cardiologist()
        assert not result.healthy

    @pytest.mark.asyncio
    async def test_surgeon_cardiologist_with_key(self, monkeypatch):
        """Cardiologist check should pass when API key is set."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        hc = HealthChecker(node_id="mac-test")
        result = await hc._check_surgeon_cardiologist()
        assert result.healthy
