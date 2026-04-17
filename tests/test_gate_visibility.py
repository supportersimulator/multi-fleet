"""Tests for gate visibility — when gates fire, events appear in the dashboard.

Session 2 Deliverable 1: Gates emit visible events, corrigibility gauge consumes them.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.event_bus import IDEEventBus
from multifleet.invariance_gates import (
    FleetSendGate,
    FleetRepairGate,
    GateCheck,
    GateResult,
)
from multifleet.theatrical.corrigibility_gauge import CorrigibilityGauge


@pytest.fixture(autouse=True)
def reset_event_bus():
    """Reset singleton between tests."""
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


@pytest.fixture
def bus():
    return IDEEventBus.get()


@pytest.fixture
def collected_events(bus):
    """Subscribe to gate events and collect them."""
    events = []
    bus.on("gate.*", lambda e: events.append(e))
    return events


# ── Gate event emission tests ──


class TestGateEmitsEvents:
    """Verify that gate firings produce events on the EventBus."""

    @patch("multifleet.invariance_gates.FleetSecurity", create=True)
    @patch("multifleet.invariance_gates.load_config", create=True)
    @patch("multifleet.invariance_gates.validate_before_action", create=True)
    def test_send_gate_pass_emits_gate_checked_and_result(
        self, mock_constitution, mock_config, mock_security, collected_events
    ):
        """FleetSendGate.check() emits both gate.checked and gate.result."""
        # Patch imports used inside the gate
        mock_constitution_result = MagicMock()
        mock_constitution_result.hard_failures = []
        mock_constitution_result.summary = "All OK"
        mock_constitution_result.advisories = []
        mock_constitution.return_value = mock_constitution_result

        mock_config.return_value = {"nodes": {"mac1": {}, "mac2": {}}}
        mock_security_inst = MagicMock()
        mock_security_inst.has_hmac_key = True
        mock_security.return_value = mock_security_inst

        packet = {
            "from_node": "mac1",
            "to_node": "mac2",
            "type": "heartbeat",
            "id": "test-123",
        }

        with patch("multifleet.invariance_gates.validate_before_action",
                    return_value=mock_constitution_result):
            gate = FleetSendGate()
            result = gate.check(packet)

        # Filter for our event types
        checked = [e for e in collected_events if e.get("type") == "gate.checked"]
        results = [e for e in collected_events if e.get("type") == "gate.result"]

        assert len(checked) >= 1, "Expected gate.checked event"
        assert len(results) >= 1, "Expected gate.result event"

        # Verify gate.checked payload
        payload = checked[0]["payload"]
        assert payload["gate"] == "FleetSendGate"
        assert payload["passed"] is True
        assert "timestamp" in payload

        # Verify gate.result payload has detailed checks
        result_payload = results[0]["payload"]
        assert result_payload["gate"] == "FleetSendGate"
        assert result_payload["passed"] is True
        assert "checks" in result_payload
        assert isinstance(result_payload["checks"], list)
        assert len(result_payload["checks"]) > 0
        assert "duration_ms" in result_payload
        assert "summary" in result_payload

    def test_repair_gate_fail_emits_event_with_failures(self, collected_events):
        """FleetRepairGate blocking a repair emits gate.result with failure details."""
        gate = FleetRepairGate()
        # Use an invalid action to guarantee failure
        result = gate.check(
            target="mac2",
            action="invalid_action_xyz",
            channel_health={"mac2": {"score": 20, "last_seen": time.time()}},
        )

        assert not result.passed

        results = [e for e in collected_events if e.get("type") == "gate.result"]
        assert len(results) >= 1, "Expected gate.result event on failure"

        payload = results[0]["payload"]
        assert payload["gate"] == "FleetRepairGate"
        assert payload["passed"] is False
        assert len(payload["critical_failures"]) > 0
        assert "valid_action" in payload["critical_failures"]

    def test_gate_events_land_in_namespace_ring(self, bus):
        """Gate events should be queryable from the 'gate' namespace history."""
        gate = FleetRepairGate()
        gate.check(
            target="mac2",
            action="clear_dedup",
            channel_health={"mac2": {"score": 20, "last_seen": time.time()}},
        )

        history = bus.history("gate", limit=10)
        assert len(history) > 0, "Gate events should appear in namespace ring"

        # Should have both gate.checked and gate.result
        types = {e.get("type") for e in history}
        assert "gate.checked" in types
        assert "gate.result" in types

    def test_gate_result_contains_check_details(self, collected_events):
        """gate.result event includes per-check name/passed/message/critical."""
        gate = FleetRepairGate()
        gate.check(
            target="mac2",
            action="clear_dedup",
            channel_health={"mac2": {"score": 20, "last_seen": time.time()}},
        )

        results = [e for e in collected_events if e.get("type") == "gate.result"]
        assert len(results) >= 1

        checks = results[0]["payload"]["checks"]
        for check in checks:
            assert "name" in check
            assert "passed" in check
            assert "message" in check
            assert "critical" in check


# ── Corrigibility Gauge rendering tests ──


class TestCorrigibilityGaugeRendering:
    """Verify the gauge renders gate history and disagreements."""

    def test_gauge_renders_with_gate_history(self):
        """Gauge shows gate activity when gate_history is provided."""
        gauge = CorrigibilityGauge()
        data = {
            "score": 0.75,
            "trend": [],
            "factors": {"gate_pass_rate": 0.8},
            "last_check": time.time(),
            "gate_history": [
                {
                    "gate": "FleetSendGate",
                    "passed": True,
                    "summary": "PASSED (5/5)",
                    "failures": [],
                    "duration_ms": 12.3,
                    "timestamp": time.time(),
                },
                {
                    "gate": "FleetRepairGate",
                    "passed": False,
                    "summary": "FAILED: valid_action",
                    "failures": ["valid_action"],
                    "duration_ms": 5.1,
                    "timestamp": time.time() - 60,
                },
            ],
        }

        html = gauge.render(data)
        assert "Gate Activity" in html
        assert "FleetSendGate" in html
        assert "FleetRepairGate" in html
        # Failed gate should show "Blocked:" text
        assert "Blocked:" in html
        assert "valid_action" in html

    def test_gauge_renders_with_disagreements(self):
        """Gauge shows disagreement section when disagreements exist."""
        gauge = CorrigibilityGauge()
        data = {
            "score": 0.6,
            "trend": [],
            "factors": {},
            "last_check": time.time(),
            "recent_disagreements": [
                {
                    "topic": "Deploy strategy",
                    "surgeon_a_position": "Roll forward",
                    "surgeon_b_position": "Rollback first",
                    "resolution": "Rollback chosen after evidence review",
                    "ts": time.time(),
                },
            ],
        }

        html = gauge.render(data)
        assert "Disagreements" in html
        assert "Deploy strategy" in html
        assert "Roll forward" in html
        assert "Rollback" in html

    def test_gauge_renders_impact_stats(self):
        """Gauge shows decision impact stats."""
        gauge = CorrigibilityGauge()
        data = {
            "score": 0.85,
            "trend": [],
            "factors": {},
            "last_check": time.time(),
            "stats": {
                "total_decisions": 42,
                "changed_by_surgeon": 7,
                "total_disagreements": 3,
            },
        }

        html = gauge.render(data)
        assert "42 decisions" in html
        assert "7 changed by surgeon" in html
        assert "3 disagreements" in html

    def test_gauge_renders_empty_gate_history_gracefully(self):
        """Gauge renders without gate section when no gate history."""
        gauge = CorrigibilityGauge()
        data = {
            "score": 0.5,
            "trend": [],
            "factors": {},
            "last_check": time.time(),
        }

        html = gauge.render(data)
        assert "Corrigibility Gauge" in html
        # No gate activity section
        assert "Gate Activity" not in html

    def test_gauge_unavailable_without_data(self):
        """Gauge renders unavailable state when data is None."""
        gauge = CorrigibilityGauge()
        html = gauge.render(None)
        assert "unavailable" in html.lower() or "No corrigibility data" in html


# ── End-to-end: gate fires -> event -> gauge can render ──


class TestGateToGaugeEndToEnd:
    """Integration: gate fires, event lands in bus, gauge data is populated."""

    def test_gate_fire_produces_renderable_gauge_data(self, bus):
        """Fire a gate, read events from bus, build gauge data, render it."""
        # Fire a gate
        gate = FleetRepairGate()
        gate.check(
            target="mac2",
            action="clear_dedup",
            channel_health={"mac2": {"score": 20, "last_seen": time.time()}},
        )

        # Read events from bus (simulating what gather_data does)
        gate_events = bus.history("gate", limit=20, pattern="gate.result")
        assert len(gate_events) > 0

        # Build gauge data from events
        gate_history = []
        for evt in gate_events:
            payload = evt.get("payload", {})
            gate_history.append({
                "gate": payload.get("gate", "?"),
                "passed": payload.get("passed", True),
                "summary": payload.get("summary", ""),
                "failures": payload.get("critical_failures", []),
                "duration_ms": payload.get("duration_ms", 0),
                "timestamp": payload.get("timestamp", 0),
            })

        gauge_data = {
            "score": 0.7,
            "trend": [],
            "factors": {"gate_pass_rate": 1.0},
            "last_check": time.time(),
            "gate_history": gate_history,
        }

        # Render gauge
        gauge = CorrigibilityGauge()
        html = gauge.render(gauge_data)
        assert "FleetRepairGate" in html
        assert "Corrigibility Gauge" in html
