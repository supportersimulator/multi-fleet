"""Session 2 Integration Test — stress rises → full protocol triggers → dashboard shows results.

End-to-end interlock chain verification:
  1. StressScore rises above critical threshold
  2. This triggers full surgeon protocol (FleetRepairGate)
  3. Surgeon cross-exam runs (mocked)
  4. Results appear in theatrical dashboard data (corrigibility gauge)
  5. Gate events appear in EventBus NS_GATE ring

All surgeon calls are mocked — no real LLM needed.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.event_bus import IDEEventBus, NS_GATE
from multifleet.invariance_gates import FleetRepairGate, FleetSendGate, GateResult
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.stress_score import (
    STRESS_CRITICAL,
    STRESS_DEGRADED,
    STRESS_NORMAL,
    compute_stress_score,
    get_stress_level,
)
from multifleet.theatrical.corrigibility_gauge import CorrigibilityGauge
from multifleet.theatrical.theatrical_dashboard import TheatricalDashboard

_SURGEON_ADAPTER_PATCH = "multifleet.surgeon_adapter.SurgeonAdapter"


# ── Helpers ──


def _mock_surgeon(consensus=True, confidence=0.8, dissents=None,
                  review_status="ok", review_output="approved",
                  phase="idle", disagreement=0.0):
    """Create a mock SurgeonAdapter with configurable behavior."""
    adapter = MagicMock()
    adapter.request_cross_exam.return_value = {
        "surgeons_consulted": ["atlas", "cardiologist"],
        "consensus": consensus,
        "dissents": dissents or [],
        "confidence": confidence,
        "output": "cross-exam result",
        "source": "test",
    }
    adapter.check_consensus.return_value = {
        "resolved": consensus,
        "confidence": confidence,
        "dissent": dissents or [],
        "source": "test",
        "cached": False,
    }
    adapter.get_state.return_value = {
        "phase": phase,
        "disagreement_score": disagreement,
        "correction_loops": 0,
        "abstention": False,
    }
    adapter.request_review.return_value = {
        "status": review_status,
        "output": review_output,
        "path": "test",
        "review_type": "cardio_review",
    }
    adapter.is_available.return_value = True
    return adapter


@pytest.fixture(autouse=True)
def _reset_eventbus():
    """Ensure clean EventBus singleton for each test."""
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


def _emit_gate_event(bus, gate_name, passed, failures=None):
    """Emit a gate event into NS_GATE namespace."""
    bus.emit(
        f"gate.{gate_name}",
        {
            "gate": gate_name,
            "passed": passed,
            "failures": failures or [],
            "timestamp": time.time(),
        },
        source="test",
    )


# ══════════════════════════════════════════════════════════════════
# TEST 1: Stress triggers full protocol
# ══════════════════════════════════════════════════════════════════


class TestStressTriggersFullProtocol:
    """When stress crosses critical threshold, surgeon protocol must fire."""

    def test_stress_below_critical_no_surgeon(self):
        """Below critical (0.75) → no surgeon review for destructive repair."""
        gate = FleetRepairGate()
        result = gate.check(
            "mac2", "full_restart",
            channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
            node_id="mac1",
            stress_score=0.40,
        )
        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 0, "No surgeon review at low stress"

    def test_stress_at_critical_triggers_surgeon(self):
        """At critical (>=0.75) + destructive repair → surgeon review fires."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon(review_output="proceed with the repair, approved")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 1, "Surgeon review must fire at critical stress"
        assert review_checks[0].passed is True

    def test_stress_computed_crosses_critical(self):
        """Verify compute_stress_score can cross critical threshold."""
        # Normal: no stress factors
        score_normal = compute_stress_score()
        assert get_stress_level(score_normal) == STRESS_NORMAL

        # Stack factors to reach critical
        score_critical = compute_stress_score(
            fallback_p3plus_count=3,   # 0.30
            chief_sync_failures=2,     # 0.25
            stale_peers=1,             # 0.10
            surgeon_available=False,   # 0.15
        )
        assert get_stress_level(score_critical) == STRESS_CRITICAL
        assert score_critical >= 0.75

    def test_surgeon_unavailability_pushes_stress(self):
        """Losing surgeon pushes stress from degraded to critical."""
        base = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            stale_peers=1,
        )
        assert get_stress_level(base) in (STRESS_DEGRADED, STRESS_CRITICAL)

        with_surgeon_down = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            stale_peers=1,
            surgeon_available=False,
        )
        assert with_surgeon_down - base == pytest.approx(0.15, abs=0.001)
        assert get_stress_level(with_surgeon_down) == STRESS_CRITICAL


# ══════════════════════════════════════════════════════════════════
# TEST 2: Gate events reach dashboard
# ══════════════════════════════════════════════════════════════════


class TestGateEventsReachDashboard:
    """Gate events emitted to EventBus must appear in theatrical dashboard data."""

    def test_gate_event_appears_in_eventbus_ring(self):
        """Emitted gate event is retrievable from NS_GATE ring."""
        bus = IDEEventBus.get()
        _emit_gate_event(bus, "fleet_repair", True)

        history = bus.history(NS_GATE, limit=10, pattern="gate.*")
        assert len(history) >= 1
        latest = history[0]
        assert latest["payload"]["gate"] == "fleet_repair"
        assert latest["payload"]["passed"] is True

    def test_failed_gate_event_in_ring(self):
        """Failed gate event with failures list appears correctly."""
        bus = IDEEventBus.get()
        _emit_gate_event(bus, "fleet_send", False, failures=["no_quorum", "surgeon_reject"])

        history = bus.history(NS_GATE, limit=10, pattern="gate.*")
        assert len(history) >= 1
        latest = history[0]
        assert latest["payload"]["passed"] is False
        assert "no_quorum" in latest["payload"]["failures"]

    def test_corrigibility_gauge_renders_with_gate_data(self):
        """CorrigibilityGauge renders when given gate event data."""
        gauge = CorrigibilityGauge()
        data = {
            "score": 0.85,
            "trend": [
                {"timestamp": time.time() - 60, "score": 0.80},
                {"timestamp": time.time(), "score": 0.85},
            ],
            "factors": {"gate_pass_rate": 0.9},
            "last_check": time.time(),
        }
        html = gauge.render(data)
        assert "tc-corrigibility-gauge" in html
        assert "85%" in html  # score rendered

    def test_dashboard_gather_includes_corrigibility(self):
        """TheatricalDashboard.gather_data() includes corrigibility section."""
        bus = IDEEventBus.get()
        # Emit some gate events so the dashboard has data
        _emit_gate_event(bus, "fleet_send", True)
        _emit_gate_event(bus, "fleet_repair", False, ["stress_policy"])
        _emit_gate_event(bus, "fleet_merge", True)

        dash = TheatricalDashboard()
        data = dash.gather_data()

        corr = data.get("corrigibility")
        assert corr is not None, "corrigibility data must be present"
        assert "score" in corr
        assert "gate_history" in corr
        assert len(corr["gate_history"]) >= 2  # at least some events
        assert corr["block_count"] >= 1  # one failed gate


# ══════════════════════════════════════════════════════════════════
# TEST 3: Dissent creates proposal
# ══════════════════════════════════════════════════════════════════


class TestDissentCreatesProposal:
    """Surgeon dissent must create a strategic proposal in QuorumRegistry."""

    def test_dissent_creates_proposal(self):
        """Mocked surgeon dissent → QuorumRegistry gets a proposal."""
        registry = QuorumRegistry()
        dissent = {
            "surgeons_consulted": ["atlas", "cardiologist", "neurologist"],
            "consensus": False,
            "confidence": 0.2,
            "dissents": ["Race condition in shared state mutation"],
        }

        proposal = registry.record_surgeon_dissent("shared state design", dissent, "mac1")

        assert proposal.proposal_id.startswith("surgeon-dissent-")
        assert "SURGEON DISSENT" in proposal.topic
        assert "mac1" in proposal.supporting_nodes

    def test_dissent_needs_fleet_support_for_quorum(self):
        """Dissent proposal alone does not reach quorum."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.3, "dissents": ["concern"]}

        proposal = registry.record_surgeon_dissent("test topic", dissent, "mac1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            MockAdapter.return_value = _mock_surgeon(confidence=0.9)
            assert registry.has_quorum(proposal.proposal_id) is False

    def test_dissent_reaches_quorum_with_fleet_support(self):
        """Dissent + fleet support + chief confirmation → quorum reached."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.2, "dissents": ["Architecture concern"]}

        proposal = registry.record_surgeon_dissent("arch concern", dissent, "mac1")
        registry.record_support(proposal.proposal_id, "arch concern", "mac2")
        registry.record_chief_confirmation(proposal.proposal_id)

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            MockAdapter.return_value = _mock_surgeon(confidence=0.9)
            assert registry.has_quorum(proposal.proposal_id) is True

    def test_dissent_visible_in_pending(self):
        """Dissent proposal appears in pending_proposals list."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.5, "dissents": ["issue found"]}
        registry.record_surgeon_dissent("visibility test", dissent, "mac1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            MockAdapter.return_value = _mock_surgeon(confidence=0.9)
            pending = registry.pending_proposals()

        assert len(pending) == 1
        assert "SURGEON DISSENT" in pending[0]["topic"]


# ══════════════════════════════════════════════════════════════════
# TEST 4: End-to-end — stress → protocol → dissent → proposal → dashboard
# ══════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """Full interlock chain: stress → surgeon → dissent → quorum → dashboard."""

    def test_full_chain(self):
        """Stress rises → surgeon fires → dissent → proposal → dashboard shows all."""
        bus = IDEEventBus.get()

        # ── Step 1: Compute stress that crosses critical ──
        stress = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            stale_peers=1,
            surgeon_available=False,
        )
        assert get_stress_level(stress) == STRESS_CRITICAL

        # ── Step 2: Critical stress triggers surgeon review on repair ──
        gate = FleetRepairGate()
        adapter = _mock_surgeon(
            consensus=False,
            confidence=0.3,
            dissents=["Destructive repair risky under current conditions"],
            review_output="deny this repair, too risky",
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            gate_result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 5, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=stress,
            )

        # Surgeon review fired and blocked
        review_checks = [c for c in gate_result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 1

        # ── Step 3: Emit gate event to EventBus ──
        _emit_gate_event(
            bus, "fleet_repair",
            passed=gate_result.passed,
            failures=[c.name for c in gate_result.checks if not c.passed],
        )

        # ── Step 4: Surgeon dissent becomes quorum proposal ──
        registry = QuorumRegistry()
        dissent_data = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": False,
            "confidence": 0.3,
            "dissents": ["Destructive repair risky under current conditions"],
        }
        proposal = registry.record_surgeon_dissent(
            "repair risk under critical stress", dissent_data, "mac1",
        )
        assert proposal.proposal_id.startswith("surgeon-dissent-")

        # ── Step 5: Verify dashboard sees everything ──
        dash = TheatricalDashboard()
        data = dash.gather_data()

        # Corrigibility gauge has gate events
        corr = data["corrigibility"]
        assert corr is not None
        assert len(corr["gate_history"]) >= 1
        assert corr["block_count"] >= 1  # repair was blocked

        # Score reflects the failure
        assert corr["score"] < 1.0  # not perfect — there was a block

        # Corrigibility gauge renders without error
        gauge_html = dash.corrigibility_gauge.render(corr)
        assert "tc-corrigibility-gauge" in gauge_html

    def test_full_chain_with_approval(self):
        """Stress critical + surgeon approves → repair passes → dashboard reflects."""
        bus = IDEEventBus.get()

        stress = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            surgeon_available=True,
        )
        # Should be degraded or higher
        assert stress >= 0.50

        # Push to critical for test
        stress_critical = max(stress, 0.80)

        gate = FleetRepairGate()
        adapter = _mock_surgeon(
            consensus=True,
            confidence=0.9,
            review_output="repair approved, proceed with caution",
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            gate_result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 5, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=stress_critical,
            )

        # Emit pass event
        _emit_gate_event(bus, "fleet_repair", passed=gate_result.passed)

        # Dashboard shows pass
        dash = TheatricalDashboard()
        data = dash.gather_data()
        corr = data["corrigibility"]
        assert corr is not None
        # At least one event recorded
        assert len(corr["gate_history"]) >= 1

    def test_multiple_gates_aggregate_in_dashboard(self):
        """Multiple gate events accumulate and score reflects them."""
        bus = IDEEventBus.get()

        # Emit a mix of pass/fail
        _emit_gate_event(bus, "fleet_send", True)
        _emit_gate_event(bus, "fleet_send", True)
        _emit_gate_event(bus, "fleet_repair", False, ["stress_policy"])
        _emit_gate_event(bus, "fleet_merge", True)
        _emit_gate_event(bus, "fleet_repair", False, ["surgeon_block"])

        dash = TheatricalDashboard()
        data = dash.gather_data()
        corr = data["corrigibility"]

        assert corr is not None
        assert len(corr["gate_history"]) >= 5
        assert corr["block_count"] >= 2
        # Gate pass rate should reflect failures (not 1.0)
        assert corr["factors"]["gate_pass_rate"] < 1.0
        # Score should be less than perfect due to failures
        assert corr["score"] < 1.0
