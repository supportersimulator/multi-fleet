"""End-to-end interlock wiring tests for Points 1, 4, and 5.

Verifies that the trigger -> gate -> surgeon -> result flow works
across module boundaries. Unlike test_invariance_interlock.py which
tests each component in isolation, these tests verify the actual
wiring between modules.

Point 1: FleetSendGate -> surgeon cross-exam (strategic signals need review)
Point 4: Surgeon dissent -> QuorumRegistry (disagreements become fleet-visible proposals)
Point 5: StressScore -> surgeon risk mode (critical stress triggers full protocol)
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.invariance_gates import FleetSendGate
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.stress_score import (
    compute_stress_score,
    evaluate_stress_and_trigger_surgeon,
    get_stress_level,
    STRESS_CRITICAL,
)
from multifleet.surgeon_adapter import SurgeonAdapter

# Patch targets
_SURGEON_ADAPTER_GATES = "multifleet.surgeon_adapter.SurgeonAdapter"
_BRIDGE_MODULE = "multifleet.surgeon_adapter"


def _mock_bridge(consensus_output=None, cardio_output=None, challenge_output=None):
    """Create a mock surgery_bridge module."""
    bridge = MagicMock()
    bridge.bridge_status.return_value = {"status": "ok", "mode": "test"}

    if cardio_output is None:
        cardio_output = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.8, "dissent": []},
            "path": "test",
        }
    bridge.cardio_review.return_value = cardio_output

    if consensus_output is None:
        consensus_output = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.7, "dissent": []},
            "path": "test",
        }
    bridge.consensus.return_value = consensus_output

    if challenge_output is None:
        challenge_output = {"status": "ok", "output": "challenge result", "path": "test"}
    bridge.neurologist_challenge.return_value = challenge_output

    return bridge


# ======================================================================
# POINT 1: FleetSendGate -> surgeon cross-exam end-to-end
# ======================================================================


class TestPoint1_SendGateToSurgeonE2E:
    """Verify FleetSendGate actually invokes SurgeonAdapter.request_cross_exam
    for strategic signals and that the result determines gate pass/fail."""

    def test_strategic_signal_triggers_cross_exam_and_passes(self):
        """Full flow: strategic packet -> gate -> surgeon adapter -> cross-exam -> gate passes."""
        gate = FleetSendGate()
        pkt = {
            "from_node": "mac1",
            "to_node": "mac2",
            "type": "architecture_proposal",
            "id": "e2e-test-1",
            "signal_class": "strategic",
            "payload": {"subject": "memory bridge redesign"},
            "meta": {"corroboration_count": 0},
        }

        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": True,
            "dissents": [],
            "confidence": 0.85,
            "output": "All surgeons approve the architecture proposal",
            "source": "cardio_review",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(pkt)

        # Verify surgeon was actually called with correct topic
        mock_adapter.request_cross_exam.assert_called_once()
        call_kwargs = mock_adapter.request_cross_exam.call_args
        assert "architecture_proposal" in call_kwargs.kwargs.get("topic", call_kwargs[1].get("topic", ""))

        # Gate should pass
        assert result.passed is True
        cross_exam = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross_exam) == 1
        assert cross_exam[0].passed is True

    def test_strategic_signal_surgeon_blocks_gate(self):
        """Full flow: strategic packet -> surgeon rejects -> gate fails."""
        gate = FleetSendGate()
        pkt = {
            "from_node": "mac1",
            "to_node": "mac2",
            "type": "risky_migration",
            "id": "e2e-test-2",
            "signal_class": "strategic",
            "payload": {"subject": "database migration"},
            "meta": {"corroboration_count": 0},
        }

        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": False,
            "dissents": ["Migration risks data loss without rollback plan"],
            "confidence": 0.25,
            "output": "Surgeons disagree - no rollback plan",
            "source": "cardio_review",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(pkt)

        assert result.passed is False
        # Both surgeon check and corroboration should fail
        failed = {c.name for c in result.checks if not c.passed and c.critical}
        assert "surgeon_cross_exam" in failed
        assert "strategic_corroboration" in failed


# ======================================================================
# POINT 4: Surgeon dissent -> QuorumRegistry end-to-end
# ======================================================================


class TestPoint4_DissentToQuorumE2E:
    """Verify SurgeonAdapter.request_cross_exam propagates dissent to QuorumRegistry."""

    def test_cross_exam_dissent_creates_quorum_proposal(self):
        """Full flow: cross-exam with dissent -> _propagate_dissent_to_quorum -> registry has proposal."""
        adapter = SurgeonAdapter()

        # Give it a mock bridge that returns dissent
        mock_bridge = _mock_bridge(
            cardio_output={
                "status": "ok",
                "output": {
                    "resolved": False,
                    "confidence": 0.3,
                    "dissent": ["Race condition in shared state"],
                },
                "path": "test",
            }
        )
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True

        # Patch QuorumRegistry to capture the call
        with patch("multifleet.surgeon_adapter.os") as mock_os:
            mock_os.environ.get.return_value = "mac1"
            with patch("multifleet.quorum.quorum_registry.QuorumRegistry") as MockRegistry:
                mock_registry = MagicMock()
                MockRegistry.return_value = mock_registry

                result = adapter.request_cross_exam(
                    topic="shared state design",
                    evidence={"concern": "race condition"},
                )

        # Verify cross-exam detected dissent
        assert result["consensus"] is False
        assert len(result["dissents"]) > 0

        # Verify dissent was propagated to quorum registry
        mock_registry.record_surgeon_dissent.assert_called_once()
        call_args = mock_registry.record_surgeon_dissent.call_args
        assert "shared state design" in call_args[0][0]  # topic

    def test_consensus_result_no_quorum_propagation(self):
        """When surgeons agree (consensus=True), no dissent is propagated."""
        adapter = SurgeonAdapter()

        mock_bridge = _mock_bridge(
            cardio_output={
                "status": "ok",
                "output": {"resolved": True, "confidence": 0.9, "dissent": []},
                "path": "test",
            }
        )
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True

        with patch("multifleet.quorum.quorum_registry.QuorumRegistry") as MockRegistry:
            mock_registry = MagicMock()
            MockRegistry.return_value = mock_registry

            result = adapter.request_cross_exam(
                topic="uncontroversial change",
                evidence={"type": "minor fix"},
            )

        assert result["consensus"] is True
        mock_registry.record_surgeon_dissent.assert_not_called()

    def test_dissent_proposal_visible_in_pending(self):
        """Dissent recorded via record_surgeon_dissent appears in pending_proposals."""
        registry = QuorumRegistry()
        dissent = {
            "consensus": False,
            "confidence": 0.25,
            "dissents": ["Memory bridge has unbounded growth"],
        }

        registry.record_surgeon_dissent("memory bridge scaling", dissent, "mac1")

        # Must patch surgeon for has_quorum check inside pending_proposals
        # The import is `from ..surgeon_adapter import SurgeonAdapter`
        with patch("multifleet.surgeon_adapter.SurgeonAdapter") as MockAdapter:
            mock_adapter = MagicMock()
            mock_adapter.check_consensus.return_value = {"confidence": 0.9}
            MockAdapter.return_value = mock_adapter
            pending = registry.pending_proposals()

        assert len(pending) >= 1
        topics = [p["topic"] for p in pending]
        assert any("SURGEON DISSENT" in t for t in topics)
        assert any("memory bridge scaling" in t for t in topics)


# ======================================================================
# POINT 5: StressScore -> surgeon risk mode end-to-end
# ======================================================================


class TestPoint5_StressToSurgeonE2E:
    """Verify critical stress triggers full surgeon protocol via evaluate_stress_and_trigger_surgeon."""

    def test_critical_stress_triggers_surgeon(self):
        """Full flow: critical stress -> evaluate -> surgeon cross-exam triggered."""
        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist", "neurologist"],
            "consensus": False,
            "dissents": ["Fleet infrastructure critically degraded"],
            "confidence": 0.4,
            "output": "Full protocol review: multiple systems failing",
            "source": "cross_exam",
        }

        with patch("multifleet.surgeon_adapter.SurgeonAdapter", return_value=mock_adapter):
            result = evaluate_stress_and_trigger_surgeon(
                topic="fleet health check",
                fallback_p3plus_count=3,  # 0.30
                chief_sync_failures=2,    # 0.25 (capped)
                stale_peers=2,            # 0.20
            )

        assert result["stress_level"] == STRESS_CRITICAL
        assert result["surgeon_triggered"] is True
        assert result["surgeon_result"] is not None
        assert "consensus" in result["surgeon_result"]

        # Verify surgeon was called with stress context
        mock_adapter.request_cross_exam.assert_called_once()
        call_kwargs = mock_adapter.request_cross_exam.call_args
        topic_arg = call_kwargs.kwargs.get("topic", call_kwargs[1].get("topic", ""))
        assert "CRITICAL STRESS" in topic_arg

    def test_normal_stress_no_surgeon(self):
        """Normal stress does not trigger surgeon protocol."""
        result = evaluate_stress_and_trigger_surgeon(
            topic="routine check",
            fallback_p3plus_count=0,
            stale_peers=0,
        )

        assert result["stress_level"] != STRESS_CRITICAL
        assert result["surgeon_triggered"] is False
        assert result["surgeon_result"] is None

    def test_degraded_stress_no_surgeon(self):
        """Degraded stress (below critical) does not trigger surgeon."""
        result = evaluate_stress_and_trigger_surgeon(
            topic="degraded check",
            fallback_p3plus_count=2,  # 0.20
            chief_sync_failures=1,    # 0.15
            stale_peers=1,            # 0.10
        )

        # Should be degraded (0.45) but not critical
        assert result["stress_score"] >= 0.25
        assert result["stress_score"] < 0.75
        assert result["surgeon_triggered"] is False

    def test_surgeon_unavailable_no_crash(self):
        """Critical stress with surgeon unavailable degrades gracefully."""
        with patch(
            "multifleet.surgeon_adapter.SurgeonAdapter",
            side_effect=ImportError("no bridge"),
        ):
            result = evaluate_stress_and_trigger_surgeon(
                topic="no surgeon available",
                fallback_p3plus_count=3,
                chief_sync_failures=2,
                stale_peers=2,
            )

        assert result["stress_level"] == STRESS_CRITICAL
        assert result["surgeon_triggered"] is False
        assert "error" in result["surgeon_result"]

    def test_surgeon_unavailable_raises_stress(self):
        """INTERLOCK 5 input side: surgeon_available=False increases stress score."""
        score_with = compute_stress_score(
            fallback_p3plus_count=2,
            surgeon_available=True,
        )
        score_without = compute_stress_score(
            fallback_p3plus_count=2,
            surgeon_available=False,
        )

        assert score_without > score_with
        assert score_without - score_with == pytest.approx(0.15, abs=0.001)


# ======================================================================
# INTEGRATION: stress rises -> full protocol -> dissent -> visible in quorum
# ======================================================================


class TestIntegration_StressToDissentToQuorum:
    """End-to-end: stress rises, triggers surgeon, dissent propagates to quorum."""

    def test_critical_stress_surgeon_dissent_becomes_proposal(self):
        """Critical stress -> surgeon cross-exam -> dissent -> quorum proposal.

        This is the full chain: Point 5 triggers surgeon, Point 4 propagates
        the dissent to the quorum registry, making it fleet-visible.
        """
        # Create a real adapter with a mock bridge that produces dissent
        mock_bridge = _mock_bridge(
            cardio_output={
                "status": "ok",
                "output": {
                    "resolved": False,
                    "confidence": 0.3,
                    "dissent": ["Fleet has multiple cascading failures"],
                },
                "path": "test",
            }
        )

        real_adapter = SurgeonAdapter()
        real_adapter._bridge_module = mock_bridge
        real_adapter._bridge_available = True

        captured_registry = {}

        def capture_record_surgeon_dissent(topic, dissent, node_id):
            captured_registry["topic"] = topic
            captured_registry["dissent"] = dissent
            captured_registry["node_id"] = node_id
            return MagicMock()

        with patch(
            "multifleet.surgeon_adapter.SurgeonAdapter",
            return_value=real_adapter,
        ):
            with patch(
                "multifleet.quorum.quorum_registry.QuorumRegistry"
            ) as MockQR:
                mock_qr = MagicMock()
                mock_qr.record_surgeon_dissent.side_effect = capture_record_surgeon_dissent
                MockQR.return_value = mock_qr

                with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                    result = evaluate_stress_and_trigger_surgeon(
                        topic="cascading failure investigation",
                        fallback_p3plus_count=3,
                        chief_sync_failures=2,
                        stale_peers=2,
                    )

        # Verify the full chain
        assert result["stress_level"] == STRESS_CRITICAL
        assert result["surgeon_triggered"] is True
        assert result["surgeon_result"]["consensus"] is False

        # Verify dissent was propagated to quorum
        assert "topic" in captured_registry
        assert "CRITICAL STRESS" in captured_registry["topic"]
