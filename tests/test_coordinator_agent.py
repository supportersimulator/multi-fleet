"""Tests for coordinator_agent.py — the main coordinator daemon."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from multifleet.coordinator_agent import CoordinatorAgent


class MockLedger:
    """Mock evidence ledger."""
    def __init__(self):
        self.entries = []

    def record(self, event_type, node_id, subject, payload):
        entry = {"event_type": event_type, "node_id": node_id,
                 "subject": subject, "payload": payload}
        self.entries.append(entry)
        return entry


@pytest.fixture
def agent():
    """Create a CoordinatorAgent with test config."""
    config = {
        "fleetId": "test-fleet",
        "chief": {
            "nodeId": "mac1",
            "host": "192.168.1.144",
            "ingestUrl": "http://192.168.1.144:8844/packets",
        },
        "nodes": {
            "mac1": {"host": "192.168.1.144", "role": "chief"},
            "mac2": {"host": "192.168.1.254", "role": "worker"},
        },
    }
    a = CoordinatorAgent(
        node_id="mac-test",
        heartbeat_interval_s=1.0,
        health_check_interval_s=1.0,
        surgeon_probe_interval_s=1.0,
        config=config,
    )
    # Inject mock evidence ledger
    a.evidence._ledger = MockLedger()
    return a


class TestCoordinatorAgentInit:
    def test_default_init(self):
        a = CoordinatorAgent(node_id="mac1")
        assert a.node_id == "mac1"
        assert a.heartbeat_interval_s == 30.0
        assert not a.running

    def test_custom_init(self):
        a = CoordinatorAgent(
            node_id="mac3",
            heartbeat_interval_s=15.0,
            health_check_interval_s=10.0,
        )
        assert a.node_id == "mac3"
        assert a.heartbeat_interval_s == 15.0

    def test_status_before_start(self, agent):
        status = agent.status()
        assert status["node_id"] == "mac-test"
        assert status["running"] is False
        assert status["nats_connected"] is False


class TestCoordinatorAgentLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self, agent):
        """Agent should start and stop cleanly."""
        # Patch NATS to avoid real connection
        with patch.object(agent, "_connect_nats", new_callable=AsyncMock):
            await agent.start()
            assert agent.running
            status = agent.status()
            assert status["running"] is True

            await agent.stop()
            assert not agent.running

    @pytest.mark.asyncio
    async def test_double_start(self, agent):
        """Starting twice should not crash."""
        with patch.object(agent, "_connect_nats", new_callable=AsyncMock):
            await agent.start()
            await agent.start()  # should log warning but not crash
            assert agent.running
            await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, agent):
        """Stopping without starting should be a no-op."""
        await agent.stop()  # should not crash


class TestCoordinatorAgentDecisions:
    @pytest.mark.asyncio
    async def test_handle_fleet_decision(self, agent):
        agent._running = True
        data = {
            "type": "chief_decision",
            "decision": "merge",
            "task_id": "task-abc",
            "winner_branch": "feature/test",
        }
        await agent._handle_fleet_decision(data)
        assert agent._stats["decisions_received"] == 1

        # Evidence should be recorded
        entries = agent.evidence._ledger.entries
        assert any(e["event_type"] == "coordinator_fleet_decision" for e in entries)

    @pytest.mark.asyncio
    async def test_handle_reject_decision(self, agent):
        agent._running = True
        data = {
            "type": "chief_decision",
            "decision": "reject",
            "task_id": "task-xyz",
        }
        await agent._handle_fleet_decision(data)
        assert agent._stats["decisions_received"] == 1

    @pytest.mark.asyncio
    async def test_handle_cherry_pick(self, agent):
        agent._running = True
        data = {
            "type": "chief_decision",
            "decision": "cherry_pick",
            "task_id": "task-cp",
            "cherry_pick_from": [{"machine": "mac2", "commit": "abc123"}],
        }
        await agent._handle_fleet_decision(data)
        assert agent._stats["decisions_received"] == 1


class TestCoordinatorAgentMessages:
    @pytest.mark.asyncio
    async def test_on_nats_message_decision(self, agent):
        agent._running = True
        msg = MagicMock()
        msg.data = json.dumps({
            "type": "chief_decision",
            "decision": "merge",
            "task_id": "t-1",
            "winner_branch": "main",
        }).encode()
        msg.subject = "fleet.decisions.merge"
        await agent._on_nats_message(msg)
        assert agent._stats["decisions_received"] == 1

    @pytest.mark.asyncio
    async def test_on_nats_message_invalid_json(self, agent):
        msg = MagicMock()
        msg.data = b"not json"
        msg.subject = "fleet.all.test"
        # Should not crash
        await agent._on_nats_message(msg)

    @pytest.mark.asyncio
    async def test_on_message_handler(self, agent):
        received = []
        agent.on_message("custom_type", lambda data: received.append(data))

        msg = MagicMock()
        msg.data = json.dumps({"type": "custom_type", "value": 42}).encode()
        msg.subject = "fleet.mac-test.custom"
        await agent._on_nats_message(msg)
        assert len(received) == 1
        assert received[0]["value"] == 42


class TestCoordinatorAgentRepair:
    @pytest.mark.asyncio
    async def test_handle_repair_request(self, agent):
        agent._running = True
        agent._repair_gate = None  # skip gate check

        data = {
            "type": "repair",
            "from": "mac1",
            "payload": {
                "body": json.dumps({
                    "action": "reconnect_nats",
                    "reporter": "mac1",
                }),
            },
        }

        # Patch NATS reconnect
        with patch.object(agent, "_disconnect_nats", new_callable=AsyncMock):
            with patch.object(agent, "_connect_nats", new_callable=AsyncMock):
                await agent._handle_repair_request(data)

        # Evidence should record
        entries = agent.evidence._ledger.entries
        assert any(e["event_type"] == "coordinator_self_heal" for e in entries)

    @pytest.mark.asyncio
    async def test_repair_unknown_action(self, agent):
        agent._running = True
        agent._repair_gate = None
        result = await agent._execute_repair("unknown_action")
        assert result is False


class TestCoordinatorAgentSurgeons:
    @pytest.mark.asyncio
    async def test_handle_surgeon_request(self, agent):
        agent._running = True
        agent._nats_conn = None  # no NATS response

        # Mock the surgeon proxy
        mock_outcome = MagicMock()
        mock_outcome.success = True
        mock_outcome.duration_ms = 100.0
        mock_outcome.to_dict.return_value = {
            "operation": "cardio_review",
            "success": True,
        }
        agent.surgeons.cardio_review = AsyncMock(return_value=mock_outcome)

        data = {
            "type": "surgeon_request",
            "from": "mac1",
            "payload": {
                "operation": "cardio_review",
                "topic": "test architecture",
            },
        }
        await agent._handle_surgeon_request(data)

        agent.surgeons.cardio_review.assert_called_once_with("test architecture")


class TestCoordinatorAgentSyncHold:
    @pytest.mark.asyncio
    async def test_handle_sync_hold(self, agent):
        agent._running = True
        data = {
            "type": "sync_hold",
            "from": "mac1",
            "payload": {"reason": "schema migration in progress"},
        }
        await agent._handle_sync_hold(data)
        entries = agent.evidence._ledger.entries
        assert any("SYNC HOLD" in e["subject"] for e in entries)


class TestCoordinatorAgentHealthChanges:
    @pytest.mark.asyncio
    async def test_detect_health_changes(self, agent):
        from multifleet.coordinator_health import LocalHealthSnapshot, ServiceHealth

        prev = LocalHealthSnapshot(
            node_id="mac-test",
            timestamp=time.time() - 30,
            services={
                "redis": ServiceHealth(name="redis", healthy=True),
                "llm": ServiceHealth(name="llm", healthy=True),
            },
            degraded_services=[],
        )
        current = LocalHealthSnapshot(
            node_id="mac-test",
            timestamp=time.time(),
            services={
                "redis": ServiceHealth(name="redis", healthy=False, error="conn refused"),
                "llm": ServiceHealth(name="llm", healthy=True),
            },
            degraded_services=["redis"],
        )

        agent._prev_health = prev
        await agent._detect_health_changes(current)

        entries = agent.evidence._ledger.entries
        assert any(
            e["event_type"] == "coordinator_health_change"
            and e["payload"].get("service") == "redis"
            and e["payload"].get("healthy") is False
            for e in entries
        )

    @pytest.mark.asyncio
    async def test_detect_recovery(self, agent):
        from multifleet.coordinator_health import LocalHealthSnapshot, ServiceHealth

        prev = LocalHealthSnapshot(
            node_id="mac-test",
            timestamp=time.time() - 30,
            services={"redis": ServiceHealth(name="redis", healthy=False)},
            degraded_services=["redis"],
        )
        current = LocalHealthSnapshot(
            node_id="mac-test",
            timestamp=time.time(),
            services={"redis": ServiceHealth(name="redis", healthy=True)},
            degraded_services=[],
        )

        agent._prev_health = prev
        await agent._detect_health_changes(current)

        entries = agent.evidence._ledger.entries
        assert any(
            e["event_type"] == "coordinator_health_change"
            and e["payload"].get("healthy") is True
            for e in entries
        )
