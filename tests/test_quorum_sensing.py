"""Quorum Sensing Tests — verifying the bacterial communication model.

Tests the 4-layer signaling system inspired by P. aeruginosa:
    Las  -> STRATEGIC  : rare, authoritative, requires corroboration
    Rhl  -> TACTICAL   : frequent, local, execution flow
    PQS  -> STATE      : contextual broadcast, modulates behavior
    IQS  -> STRESS     : environmental override, shifts hierarchy

"Not every delivered message deserves equal authority."
"""

import time

import pytest

from multifleet.quorum import (
    SignalClass,
    classify_message,
    compute_stress_score,
    apply_stress_policy,
    get_stress_level,
    QuorumRegistry,
    compute_node_reputation,
    STRESS_NORMAL,
    STRESS_GUARDED,
    STRESS_DEGRADED,
    STRESS_CRITICAL,
)


# ════════════════════════════════════════════════════════════════════
# Signal Classification (Las/Rhl/PQS/IQS mapping)
# ════════════════════════════════════════════════════════════════════

class TestSignalClassification:
    """Verify messages are classified into the correct biological layer."""

    def test_chief_decision_is_strategic(self):
        """Chief decisions are Las-like: rare, authoritative."""
        assert classify_message("chief.decision") == SignalClass.STRATEGIC
        assert classify_message("chief_decision") == SignalClass.STRATEGIC

    def test_dao_promoted_is_strategic(self):
        """DAO promotions are strategic — fleet-shaping truth."""
        assert classify_message("dao.promoted") == SignalClass.STRATEGIC

    def test_invariant_updated_is_strategic(self):
        assert classify_message("invariant.updated") == SignalClass.STRATEGIC

    def test_task_dispatch_is_tactical(self):
        """Task dispatch is Rhl-like: frequent, execution-facing."""
        assert classify_message("task.dispatch") == SignalClass.TACTICAL
        assert classify_message("task_dispatch") == SignalClass.TACTICAL

    def test_fleet_task_events_are_tactical(self):
        """Fleet transport task events map to tactical."""
        assert classify_message("fleet.task.created") == SignalClass.TACTICAL
        assert classify_message("fleet.task.dispatched") == SignalClass.TACTICAL
        assert classify_message("fleet.task.delivered") == SignalClass.TACTICAL

    def test_worker_result_is_tactical(self):
        assert classify_message("worker.result") == SignalClass.TACTICAL

    def test_node_freshness_is_state(self):
        """Node freshness is PQS-like: contextual broadcast."""
        assert classify_message("node.freshness") == SignalClass.STATE
        assert classify_message("node_freshness") == SignalClass.STATE

    def test_heartbeat_is_state(self):
        """Heartbeats are colony state signals."""
        assert classify_message("fleet.node.heartbeat") == SignalClass.STATE

    def test_transport_degraded_is_state(self):
        assert classify_message("transport.degraded") == SignalClass.STATE
        assert classify_message("fleet.transport.degraded") == SignalClass.STATE

    def test_quota_low_is_stress(self):
        """Resource exhaustion is IQS-like: environmental override."""
        assert classify_message("quota.low") == SignalClass.STRESS
        assert classify_message("quota_low") == SignalClass.STRESS

    def test_chief_sync_failed_is_stress(self):
        assert classify_message("chief.sync_failed") == SignalClass.STRESS

    def test_fallback_p3plus_is_stress(self):
        """P3+ fallback usage means ideal channels are broken — stress signal."""
        assert classify_message("fallback.p3plus") == SignalClass.STRESS

    def test_unknown_type_defaults_to_tactical(self):
        """Unknown types default to Rhl-like execution chatter (safe default)."""
        assert classify_message("some.unknown.type") == SignalClass.TACTICAL
        assert classify_message("") == SignalClass.TACTICAL

    def test_payload_stress_override(self):
        """Payload can override classification to stress (IQS activation)."""
        result = classify_message("task.dispatch", {"stress_override": True})
        assert result == SignalClass.STRESS

    def test_payload_requires_quorum(self):
        """Payload can mark a message as requiring quorum (Las promotion)."""
        result = classify_message("worker.result", {"requires_quorum": True})
        assert result == SignalClass.STRATEGIC

    def test_payload_override_only_when_present(self):
        """Without override flags, payload does not change classification."""
        result = classify_message("task.dispatch", {"some_data": 42})
        assert result == SignalClass.TACTICAL

    def test_signal_class_enum_values(self):
        """Verify enum values are lowercase strings for serialization."""
        assert SignalClass.STRATEGIC.value == "strategic"
        assert SignalClass.TACTICAL.value == "tactical"
        assert SignalClass.STATE.value == "state"
        assert SignalClass.STRESS.value == "stress"


# ════════════════════════════════════════════════════════════════════
# Stress Score (IQS-like environmental pressure)
# ════════════════════════════════════════════════════════════════════

class TestStressScore:
    """Verify stress computation and policy derivation."""

    def test_zero_inputs_zero_stress(self):
        """Perfect conditions: no stress factors = 0.0 score."""
        score = compute_stress_score()
        assert score == 0.0
        assert get_stress_level(score) == STRESS_NORMAL

    def test_normal_conditions_low_score(self):
        """Minor degradation stays in normal range."""
        score = compute_stress_score(fallback_p3plus_count=1, stale_peers=0)
        assert score < 0.25
        assert get_stress_level(score) == STRESS_NORMAL

    def test_multiple_p3plus_elevates_score(self):
        """Heavy P3+ fallback usage pushes into guarded territory."""
        score = compute_stress_score(fallback_p3plus_count=3, stale_peers=1)
        assert 0.25 <= score < 0.50
        assert get_stress_level(score) == STRESS_GUARDED

    def test_chief_sync_failures_contribute(self):
        """Chief sync failures add significant stress (coordination degraded).
        2 failures * 0.15 weight = 0.30, but capped at 0.25 per factor."""
        score = compute_stress_score(chief_sync_failures=2)
        assert score == pytest.approx(0.25, abs=0.01)

    def test_critical_threshold(self):
        """Multiple degradation factors push to critical."""
        score = compute_stress_score(
            fallback_p3plus_count=3,  # 0.30
            stale_peers=2,            # 0.20
            chief_sync_failures=2,    # 0.25 (capped)
        )
        assert score >= 0.75
        assert get_stress_level(score) == STRESS_CRITICAL

    def test_score_capped_at_1(self):
        """Score never exceeds 1.0 regardless of inputs."""
        score = compute_stress_score(
            fallback_p3plus_count=100,
            stale_peers=100,
            chief_sync_failures=100,
            surgeon_deadlocks=100,
            context_staleness=100.0,
        )
        assert score == 1.0

    def test_individual_factor_caps(self):
        """Each factor is capped — no single factor can dominate."""
        # Fallback alone maxes at 0.30
        score = compute_stress_score(fallback_p3plus_count=100)
        assert score == pytest.approx(0.30, abs=0.01)

        # Stale peers alone maxes at 0.20
        score = compute_stress_score(stale_peers=100)
        assert score == pytest.approx(0.20, abs=0.01)

    def test_degraded_level(self):
        """Verify degraded level range."""
        score = compute_stress_score(
            fallback_p3plus_count=3,  # 0.30
            stale_peers=2,            # 0.20
        )
        assert 0.50 <= score < 0.75
        assert get_stress_level(score) == STRESS_DEGRADED

    # ── Policy tests ──

    def test_normal_policy_full_autonomy(self):
        """Normal stress: full autonomy, speculative work allowed."""
        policy = apply_stress_policy(0.0)
        assert policy["allow_speculative_work"] is True
        assert policy["allow_irreversible_actions"] is True
        assert policy["require_chief_review"] is False
        assert policy["max_packet_size"] == "large"
        assert policy["stress_level"] == STRESS_NORMAL

    def test_guarded_policy_reduced_speculation(self):
        """Guarded stress: speculative work still allowed but size reduced."""
        policy = apply_stress_policy(0.30)
        assert policy["allow_speculative_work"] is True
        assert policy["max_packet_size"] == "medium"
        assert policy["stress_level"] == STRESS_GUARDED

    def test_degraded_policy_no_irreversible(self):
        """Degraded: no irreversible actions, prefer DAO guidance."""
        policy = apply_stress_policy(0.60)
        assert policy["allow_speculative_work"] is False
        assert policy["allow_irreversible_actions"] is False
        assert policy["prefer_dao_guidance"] is True
        assert policy["stress_level"] == STRESS_DEGRADED

    def test_critical_policy_chief_required(self):
        """Critical: chief review required, survival mode."""
        policy = apply_stress_policy(0.80)
        assert policy["require_chief_review"] is True
        assert policy["allow_speculative_work"] is False
        assert policy["allow_irreversible_actions"] is False
        assert policy["max_packet_size"] == "small"
        assert policy["stress_level"] == STRESS_CRITICAL


# ════════════════════════════════════════════════════════════════════
# Quorum Registry (Las-like corroboration threshold)
# ════════════════════════════════════════════════════════════════════

class TestQuorumRegistry:
    """Verify strategic proposal corroboration and quenching."""

    def test_single_node_no_quorum(self):
        """One node cannot create quorum — requires population density."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        assert reg.has_quorum("prop-1") is False

    def test_two_nodes_without_chief_no_quorum(self):
        """Two nodes but no chief confirmation = no quorum."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac2")
        assert reg.has_quorum("prop-1") is False

    def test_two_nodes_with_chief_has_quorum(self):
        """Two nodes + chief confirmation = quorum reached."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac2")
        reg.record_chief_confirmation("prop-1")
        assert reg.has_quorum("prop-1") is True

    def test_two_nodes_with_post_verification_has_quorum(self):
        """Two nodes + post-verification = quorum (alternative to chief)."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac2")
        reg.record_post_verification("prop-1")
        assert reg.has_quorum("prop-1") is True

    def test_same_node_twice_counts_once(self):
        """Same node supporting twice only counts as one (set semantics)."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_chief_confirmation("prop-1")
        assert reg.has_quorum("prop-1") is False  # Only 1 unique node

    def test_quench_blocks_quorum(self):
        """Quenched proposals cannot reach quorum (anti-biofilm mechanism)."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac2")
        reg.record_chief_confirmation("prop-1")
        # Would normally have quorum, but quenching blocks it
        reg.quench("prop-1", "Surgeon counter-position raised valid objection")
        assert reg.has_quorum("prop-1") is False

    def test_quench_nonexistent_returns_false(self):
        reg = QuorumRegistry()
        assert reg.quench("nonexistent", "reason") is False

    def test_pending_proposals_excludes_quenched(self):
        """Pending list does not include quenched proposals."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "topic A", "mac1")
        reg.record_support("prop-2", "topic B", "mac1")
        reg.quench("prop-2", "invalid")
        pending = reg.pending_proposals()
        assert len(pending) == 1
        assert pending[0]["proposal_id"] == "prop-1"

    def test_pending_proposals_excludes_quorum_reached(self):
        """Pending list does not include proposals that already have quorum."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "topic A", "mac1")
        reg.record_support("prop-1", "topic A", "mac2")
        reg.record_chief_confirmation("prop-1")
        reg.record_support("prop-2", "topic B", "mac1")
        pending = reg.pending_proposals()
        assert len(pending) == 1
        assert pending[0]["proposal_id"] == "prop-2"

    def test_proposal_ttl_expiry(self):
        """Proposals expire after TTL (signal molecule degradation)."""
        reg = QuorumRegistry(ttl_seconds=0.01)  # 10ms TTL
        reg.record_support("prop-1", "test topic", "mac1")
        time.sleep(0.02)  # Wait for expiry
        assert reg.get_proposal("prop-1") is None
        assert reg.has_quorum("prop-1") is False

    def test_chief_confirmation_on_nonexistent(self):
        reg = QuorumRegistry()
        assert reg.record_chief_confirmation("nonexistent") is False

    def test_post_verification_on_nonexistent(self):
        reg = QuorumRegistry()
        assert reg.record_post_verification("nonexistent") is False

    def test_get_proposal_returns_state(self):
        """Get proposal returns full state for inspection."""
        reg = QuorumRegistry()
        reg.record_support("prop-1", "test topic", "mac1")
        reg.record_support("prop-1", "test topic", "mac2")
        prop = reg.get_proposal("prop-1")
        assert prop is not None
        assert prop.topic == "test topic"
        assert prop.supporting_nodes == {"mac1", "mac2"}
        assert prop.chief_confirmed is False

    def test_custom_min_nodes(self):
        """Registry respects custom minimum node threshold."""
        reg = QuorumRegistry(min_nodes=3)
        reg.record_support("prop-1", "test", "mac1")
        reg.record_support("prop-1", "test", "mac2")
        reg.record_chief_confirmation("prop-1")
        assert reg.has_quorum("prop-1") is False  # Need 3, have 2
        reg.record_support("prop-1", "test", "mac3")
        assert reg.has_quorum("prop-1") is True  # Now have 3


# ════════════════════════════════════════════════════════════════════
# Node Reputation (PQS-like colony state)
# ════════════════════════════════════════════════════════════════════

class TestNodeReputation:
    """Verify reputation scoring influences routing decisions."""

    def test_no_history_neutral_reputation(self):
        """No task history = neutral reputation (benefit of the doubt)."""
        rep = compute_node_reputation()
        assert 0.4 <= rep <= 0.6  # Neutral range

    def test_high_success_rate_high_reputation(self):
        """Consistently successful node earns high reputation."""
        rep = compute_node_reputation(successful_tasks=100, failed_tasks=5)
        assert rep >= 0.7

    def test_high_failure_rate_low_reputation(self):
        """Frequently failing node has low reputation."""
        rep = compute_node_reputation(successful_tasks=5, failed_tasks=100)
        assert rep < 0.3

    def test_stale_sync_penalizes(self):
        """Stale chief sync reduces reputation (coordination degraded)."""
        fresh = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            avg_chief_sync_age_sec=10.0,
        )
        stale = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            avg_chief_sync_age_sec=7200.0,  # 2 hours stale
        )
        assert stale < fresh

    def test_heavy_fallback_penalizes(self):
        """Heavy P3+ usage reduces reputation (transport degraded)."""
        ideal = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            p3plus_usage_ratio=0.0,
        )
        fallback = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            p3plus_usage_ratio=0.8,
        )
        assert fallback < ideal

    def test_context_quality_adjusts_reputation(self):
        """Good context quality lifts reputation, poor context drags it."""
        good = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            context_quality=1.0,
        )
        poor = compute_node_reputation(
            successful_tasks=50, failed_tasks=5,
            context_quality=0.0,
        )
        assert good > poor

    def test_reputation_bounded_0_1(self):
        """Reputation is always in [0.0, 1.0]."""
        # Worst possible
        rep = compute_node_reputation(
            successful_tasks=0, failed_tasks=100,
            avg_chief_sync_age_sec=10000,
            p3plus_usage_ratio=1.0,
            context_quality=0.0,
        )
        assert rep >= 0.0

        # Best possible
        rep = compute_node_reputation(
            successful_tasks=1000, failed_tasks=0,
            avg_chief_sync_age_sec=0,
            p3plus_usage_ratio=0.0,
            context_quality=1.0,
        )
        assert rep <= 1.0


# ════════════════════════════════════════════════════════════════════
# Integration: Quorum Sensing + Invariance Gates
# ════════════════════════════════════════════════════════════════════

class TestQuorumGateIntegration:
    """Verify quorum sensing changes gate behavior."""

    def _make_packet(self, msg_type="task.dispatch", signal_class=None, **extra):
        packet = {
            "from_node": "mac1",
            "to_node": "mac2",
            "type": msg_type,
            "id": "test-123",
            "payload": {},
        }
        if signal_class:
            packet["signal_class"] = signal_class
        packet.update(extra)
        return packet

    def test_send_gate_blocks_uncorroborated_strategic(self):
        """Strategic message without corroboration is blocked."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(
            msg_type="chief.decision",
            signal_class="strategic",
        )
        result = gate.check(packet)
        # Should fail on strategic_corroboration
        corroboration_checks = [
            c for c in result.checks
            if c.name == "strategic_corroboration"
        ]
        assert len(corroboration_checks) == 1
        assert corroboration_checks[0].passed is False

    def test_send_gate_allows_strategic_with_corroboration(self):
        """Strategic message with corroboration metadata passes."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(
            msg_type="chief.decision",
            signal_class="strategic",
            meta={"corroboration_count": 2},
        )
        result = gate.check(packet)
        corroboration_checks = [
            c for c in result.checks
            if c.name == "strategic_corroboration"
        ]
        assert len(corroboration_checks) == 1
        assert corroboration_checks[0].passed is True

    def test_send_gate_allows_strategic_with_quorum_registry(self):
        """Strategic message passes when quorum registry confirms it."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        reg = QuorumRegistry()
        reg.record_support("test-123", "test", "mac1")
        reg.record_support("test-123", "test", "mac2")
        reg.record_chief_confirmation("test-123")
        packet = self._make_packet(
            msg_type="chief.decision",
            signal_class="strategic",
        )
        result = gate.check(packet, quorum_registry=reg)
        corroboration_checks = [
            c for c in result.checks
            if c.name == "strategic_corroboration"
        ]
        assert len(corroboration_checks) == 1
        assert corroboration_checks[0].passed is True

    def test_send_gate_blocks_irreversible_tactical_under_critical_stress(self):
        """Critical stress blocks irreversible tactical actions (IQS override)."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(
            signal_class="tactical",
            meta={"irreversible": True},
        )
        result = gate.check(packet, stress_score=0.80)
        stress_checks = [c for c in result.checks if c.name == "stress_policy"]
        assert len(stress_checks) == 1
        assert stress_checks[0].passed is False

    def test_send_gate_allows_reversible_tactical_under_stress(self):
        """Even under critical stress, reversible tactical actions pass."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(signal_class="tactical")
        result = gate.check(packet, stress_score=0.80)
        stress_checks = [c for c in result.checks if c.name == "stress_policy"]
        assert len(stress_checks) == 1
        assert stress_checks[0].passed is True

    def test_send_gate_stress_signals_always_pass(self):
        """Stress signals are the override — they always pass stress policy."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(
            msg_type="stress.entered",
            signal_class="stress",
        )
        result = gate.check(packet, stress_score=0.90)
        stress_checks = [c for c in result.checks if c.name == "stress_policy"]
        assert len(stress_checks) == 1
        assert stress_checks[0].passed is True

    def test_repair_gate_blocks_destructive_under_critical_stress(self):
        """Critical stress blocks destructive repairs (full_restart)."""
        from multifleet.invariance_gates import FleetRepairGate
        gate = FleetRepairGate()
        result = gate.check(
            "mac2", "full_restart",
            node_id="mac1",
            stress_score=0.80,
        )
        stress_checks = [
            c for c in result.checks
            if c.name == "stress_repair_policy"
        ]
        assert len(stress_checks) == 1
        assert stress_checks[0].passed is False

    def test_repair_gate_allows_nondestrictive_under_stress(self):
        """Critical stress allows non-destructive repairs."""
        from multifleet.invariance_gates import FleetRepairGate
        gate = FleetRepairGate()
        result = gate.check(
            "mac2", "reconnect_nats",
            node_id="mac1",
            stress_score=0.80,
        )
        stress_checks = [
            c for c in result.checks
            if c.name == "stress_repair_policy"
        ]
        assert len(stress_checks) == 1
        assert stress_checks[0].passed is True

    def test_stress_hierarchy_shift(self):
        """Verify the IQS-like hierarchy shift: stress changes what is allowed."""
        from multifleet.invariance_gates import FleetSendGate
        gate = FleetSendGate()
        packet = self._make_packet(
            signal_class="tactical",
            meta={"irreversible": True},
        )

        # Normal: irreversible allowed
        result_normal = gate.check(packet, stress_score=0.0)
        stress_normal = [c for c in result_normal.checks if c.name == "stress_policy"]
        assert stress_normal[0].passed is True

        # Critical: irreversible blocked
        result_critical = gate.check(packet, stress_score=0.80)
        stress_critical = [c for c in result_critical.checks if c.name == "stress_policy"]
        assert stress_critical[0].passed is False


# ════════════════════════════════════════════════════════════════════
# EventEnvelope signal_class integration
# ════════════════════════════════════════════════════════════════════

class TestEventEnvelopeSignalClass:
    """Verify signal_class is wired into the event schema."""

    def test_envelope_has_signal_class_field(self):
        """EventEnvelope carries signal_class."""
        from multifleet.event_schema import EventEnvelope
        env = EventEnvelope(type="fleet.node.heartbeat", signal_class="state")
        assert env.signal_class == "state"

    def test_signal_class_in_to_dict(self):
        """signal_class appears in serialized dict."""
        from multifleet.event_schema import EventEnvelope
        env = EventEnvelope(type="test", signal_class="tactical")
        d = env.to_dict()
        assert d["signal_class"] == "tactical"

    def test_signal_class_none_excluded_from_dict(self):
        """None signal_class is excluded from serialized dict."""
        from multifleet.event_schema import EventEnvelope
        env = EventEnvelope(type="test")
        d = env.to_dict()
        assert "signal_class" not in d

    def test_create_event_auto_classifies(self):
        """create_event auto-classifies signal_class from event type."""
        from multifleet.event_schema import create_event, EventType, EventSource
        env = create_event(
            EventType.FLEET_NODE_HEARTBEAT,
            EventSource.MULTI_FLEET,
            "mac1",
        )
        assert env.signal_class == "state"  # Heartbeat is PQS-like

    def test_create_event_respects_explicit_class(self):
        """Explicit signal_class overrides auto-classification."""
        from multifleet.event_schema import create_event, EventType, EventSource
        env = create_event(
            EventType.FLEET_NODE_HEARTBEAT,
            EventSource.MULTI_FLEET,
            "mac1",
            signal_class="stress",  # Override
        )
        assert env.signal_class == "stress"

    def test_roundtrip_serialization(self):
        """signal_class survives JSON roundtrip."""
        from multifleet.event_schema import EventEnvelope
        env = EventEnvelope(type="test", signal_class="strategic")
        json_str = env.to_json()
        restored = EventEnvelope.from_json(json_str)
        assert restored.signal_class == "strategic"
