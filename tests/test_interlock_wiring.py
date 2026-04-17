"""End-to-end interlock wiring tests for all 7 points.

Verifies that the trigger -> gate -> surgeon -> result flow works
across module boundaries. Unlike test_invariance_interlock.py which
tests each component in isolation, these tests verify the actual
wiring between modules.

Point 1: FleetSendGate -> surgeon cross-exam (strategic signals need review)
Point 2: FleetRepairGate -> surgeon review (critical channel degradation)
Point 3: ChannelHealthInvariant -> CorrigibilityLedger (degradation tracking)
Point 4: Surgeon dissent -> QuorumRegistry (disagreements become fleet-visible proposals)
Point 5: StressScore -> surgeon risk mode (critical stress triggers full protocol)
Point 6: Evidence ledger -> surgeon evidence chain (bidirectional)
Point 7: Gains gate -> fleet health probe (channel degradation surfaced)
"""

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.corrigibility_ledger import CorrigibilityLedger
from multifleet.evidence_ledger import EvidenceLedger
from multifleet.gains_gate import FleetGainsGate
from multifleet.invariance_gates import FleetRepairGate, FleetSendGate
from multifleet.invariance_rules import ChannelHealthInvariant
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


# ======================================================================
# POINT 2: FleetRepairGate -> surgeon review for critical channels
# ======================================================================


class TestPoint2_RepairGateToSurgeonE2E:
    """Verify FleetRepairGate invokes SurgeonAdapter.request_cross_exam
    when a target has critical channel degradation (>= 4 broken channels)."""

    def test_critical_channel_count_triggers_surgeon_review(self):
        """Full flow: 4+ broken channels -> gate -> surgeon cross-exam called."""
        gate = FleetRepairGate()
        channel_health = {
            "mac2": {
                "score": 10,
                "last_seen": time.time(),
                "broken": ["nats", "http", "chief", "seed"],
            },
        }

        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": True,
            "dissents": [],
            "confidence": 0.85,
            "output": "Repair approved for critical channel degradation",
            "source": "cardio_review",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(
                "mac2", "reconnect_nats",
                channel_health=channel_health,
                node_id="mac1",
                stress_score=0.3,  # below critical — surgeon triggered by channel count, not stress
            )

        # Verify surgeon was called for channel degradation
        mock_adapter.request_cross_exam.assert_called_once()
        call_kwargs = mock_adapter.request_cross_exam.call_args
        topic_arg = call_kwargs.kwargs.get("topic", call_kwargs[1].get("topic", ""))
        assert "Critical channel degradation" in topic_arg

        # Check that the surgeon review result is in the gate checks
        review_checks = [c for c in result.checks if c.name == "surgeon_critical_channel_review"]
        assert len(review_checks) == 1
        assert review_checks[0].passed is True

    def test_surgeon_flags_concern_on_critical_channels(self):
        """Surgeon cross-exam with low confidence -> advisory flag (non-blocking)."""
        gate = FleetRepairGate()
        channel_health = {
            "mac2": {
                "score": 10,
                "last_seen": time.time(),
                "broken": ["nats", "http", "chief", "seed", "ssh"],
            },
        }

        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": False,
            "dissents": ["Repair may cause cascade failure"],
            "confidence": 0.3,
            "output": "Concern: too many channels broken simultaneously",
            "source": "cardio_review",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(
                "mac2", "reconnect_nats",
                channel_health=channel_health,
                node_id="mac1",
            )

        review_checks = [c for c in result.checks if c.name == "surgeon_critical_channel_review"]
        assert len(review_checks) == 1
        assert review_checks[0].passed is False
        # Non-critical (advisory) — gate should still pass if other checks pass
        assert review_checks[0].critical is False

    def test_few_broken_channels_no_surgeon_review(self):
        """Below threshold (< 4 broken channels) -> no surgeon review triggered."""
        gate = FleetRepairGate()
        channel_health = {
            "mac2": {
                "score": 10,
                "last_seen": time.time(),
                "broken": ["nats", "http"],  # only 2 broken
            },
        }

        mock_adapter = MagicMock()

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(
                "mac2", "reconnect_nats",
                channel_health=channel_health,
                node_id="mac1",
            )

        # Surgeon should NOT be called for channel review
        mock_adapter.request_cross_exam.assert_not_called()
        review_checks = [c for c in result.checks if c.name == "surgeon_critical_channel_review"]
        assert len(review_checks) == 0

    def test_destructive_repair_skips_channel_review(self):
        """Destructive repairs (full_restart) skip the channel count review
        because they already go through the stress-based surgeon review path."""
        gate = FleetRepairGate()
        channel_health = {
            "mac2": {
                "score": 10,
                "last_seen": time.time(),
                "broken": ["nats", "http", "chief", "seed", "ssh"],
            },
        }

        mock_adapter = MagicMock()
        mock_adapter.request_review.return_value = {
            "status": "ok",
            "output": "approve this repair",
            "path": "test",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health=channel_health,
                node_id="mac1",
                stress_score=0.80,
            )

        # Channel review check should not appear (destructive path handles it)
        review_checks = [c for c in result.checks if c.name == "surgeon_critical_channel_review"]
        assert len(review_checks) == 0


# ======================================================================
# POINT 3: ChannelHealthInvariant -> CorrigibilityLedger
# ======================================================================


class TestPoint3_ChannelHealthToCorrigibilityE2E:
    """Verify ChannelHealthInvariant.check() records degradation to CorrigibilityLedger."""

    def test_degradation_recorded_in_corrigibility_ledger(self):
        """Channel health violation -> decision point in corrigibility ledger."""
        inv = ChannelHealthInvariant()
        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = CorrigibilityLedger(db_path=tmp_db)

        channel_health = {
            "mac2": {"score": "2/7", "broken": ["nats", "http", "chief", "seed", "ssh"]},
        }

        with patch("multifleet.corrigibility_ledger.CorrigibilityLedger", return_value=ledger):
            violations = inv.check(channel_health)

        assert len(violations) == 1
        assert violations[0]["peer"] == "mac2"

        # Verify the corrigibility ledger has the record
        stats = ledger.get_impact_stats()
        assert stats["total_decisions"] == 1

        # Verify the recorded context matches the violation
        with ledger._conn() as conn:
            row = conn.execute("SELECT * FROM decision_points ORDER BY id DESC LIMIT 1").fetchone()
            assert "channel_health_degraded:mac2" in row["context"]
            assert "channel_health_invariant" in row["source"]

    def test_multiple_peers_degraded_records_each(self):
        """Multiple degraded peers -> one record per peer."""
        inv = ChannelHealthInvariant()
        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = CorrigibilityLedger(db_path=tmp_db)

        channel_health = {
            "mac2": {"score": "1/7", "broken": ["nats", "http", "chief", "seed", "ssh", "wol"]},
            "mac3": {"score": "2/7", "broken": ["nats", "http", "chief", "seed", "ssh"]},
        }

        with patch("multifleet.corrigibility_ledger.CorrigibilityLedger", return_value=ledger):
            violations = inv.check(channel_health)

        assert len(violations) == 2

        stats = ledger.get_impact_stats()
        assert stats["total_decisions"] == 2

    def test_healthy_channels_no_corrigibility_record(self):
        """Healthy channels -> no violations -> no corrigibility records."""
        inv = ChannelHealthInvariant()
        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = CorrigibilityLedger(db_path=tmp_db)

        channel_health = {
            "mac2": {"score": "6/7", "broken": ["ssh"]},
        }

        with patch("multifleet.corrigibility_ledger.CorrigibilityLedger", return_value=ledger):
            violations = inv.check(channel_health)

        assert len(violations) == 0
        stats = ledger.get_impact_stats()
        assert stats["total_decisions"] == 0

    def test_corrigibility_failure_doesnt_break_check(self):
        """If CorrigibilityLedger fails, check() still returns violations."""
        inv = ChannelHealthInvariant()
        channel_health = {
            "mac2": {"score": "1/7", "broken": ["nats", "http", "chief", "seed", "ssh", "wol"]},
        }

        with patch("multifleet.corrigibility_ledger.CorrigibilityLedger", side_effect=Exception("db error")):
            violations = inv.check(channel_health)

        # Violations still returned despite ledger failure
        assert len(violations) == 1
        assert violations[0]["peer"] == "mac2"


# ======================================================================
# POINT 6: Evidence ledger -> surgeon evidence chain (bidirectional)
# ======================================================================


class TestPoint6_EvidenceLedgerSurgeonChainE2E:
    """Verify surgeon cross-exam results are recorded in evidence ledger
    AND evidence entries enrich surgeon cross-exams (bidirectional)."""

    def test_cross_exam_result_recorded_to_evidence_ledger(self):
        """SurgeonAdapter.request_cross_exam() records result in evidence ledger."""
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

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                result = adapter.request_cross_exam(
                    topic="memory bridge redesign review",
                    evidence={"concern": "unbounded growth"},
                )

        assert result["consensus"] is True

        # Evidence ledger should have the cross-exam entry
        entries = ledger.query(event_type="surgeon_cross_exam")
        assert len(entries) >= 1
        entry = entries[0]
        assert entry["event_type"] == "surgeon_cross_exam"
        assert entry["node_id"] == "mac1"
        assert "memory bridge redesign" in entry["subject"]
        assert entry["payload"]["consensus"] is True
        assert entry["payload"]["confidence"] == 0.9

    def test_evidence_enriches_cross_exam_prompt(self):
        """Evidence from ledger is included in cross-exam prompt to surgeon."""
        adapter = SurgeonAdapter()
        mock_bridge = _mock_bridge()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        # Pre-populate ledger with relevant evidence
        ledger.record("gate_activation", "mac1", "memory bridge redesign",
                       {"passed": False, "failures": ["stress_policy"]})

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                result = adapter.request_cross_exam(
                    topic="memory bridge redesign",
                    evidence={"concern": "performance"},
                )

        # The cardio_review call should include ledger evidence in the prompt
        call_args = mock_bridge.cardio_review.call_args[0][0]
        assert "ledger_entries" in call_args or "gate_activation" in call_args

    def test_dissent_cross_exam_recorded_with_dissent_count(self):
        """Cross-exam with dissent records dissent_count in evidence."""
        adapter = SurgeonAdapter()
        mock_bridge = _mock_bridge(
            cardio_output={
                "status": "ok",
                "output": {
                    "resolved": False,
                    "confidence": 0.3,
                    "dissent": ["Race condition risk", "Unbounded memory growth"],
                },
                "path": "test",
            }
        )
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                result = adapter.request_cross_exam(
                    topic="shared state architecture",
                    evidence={"concern": "race conditions"},
                )

        assert result["consensus"] is False
        entries = ledger.query(event_type="surgeon_cross_exam")
        assert len(entries) >= 1
        assert entries[0]["payload"]["dissent_count"] == 2
        assert entries[0]["payload"]["consensus"] is False

    def test_evidence_chain_integrity_after_multiple_cross_exams(self):
        """Multiple cross-exams maintain hash chain integrity in evidence ledger."""
        adapter = SurgeonAdapter()
        mock_bridge = _mock_bridge()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                adapter.request_cross_exam(topic="review 1", evidence={"a": "b"})
                adapter.request_cross_exam(topic="review 2", evidence={"c": "d"})
                adapter.request_cross_exam(topic="review 3", evidence={"e": "f"})

        chain = ledger.verify_chain()
        assert chain["valid"] is True
        assert chain["checked"] == 3


# ======================================================================
# POINT 7: Gains gate -> fleet health probe
# ======================================================================


class TestPoint7_GainsGateFleetHealthProbeE2E:
    """Verify FleetGainsGate.run() triggers fleet health probe
    via ChannelHealthInvariant after gate completes."""

    def test_gains_gate_probes_fleet_health(self):
        """After gains gate runs, fleet health is probed for channel degradation."""
        gate = FleetGainsGate()

        # Mock daemon health with degraded peer
        mock_health = {
            "status": "ok",
            "nodeId": "mac1",
            "uptime_s": 1000,
            "transport": "nats",
            "nats_url": "nats://127.0.0.1:4222",
            "peers": {"mac2": {}},
            "registered_types": ["context", "health"],
            "dedup_size": 50,
            "channel_health": {
                "mac2": {"score": "2/7", "broken": ["nats", "http", "chief", "seed", "ssh"]},
            },
        }

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        with patch.object(gate, "_get_health", return_value=mock_health):
            with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
                probe_result = gate._probe_fleet_health_on_gate(
                    MagicMock(passed=True)
                )

        assert probe_result is not None
        assert probe_result["violations"] == 1
        assert "mac2" in probe_result["degraded_peers"]

        # Verify probe was recorded in evidence ledger
        entries = ledger.query(event_type="fleet_health_probe")
        assert len(entries) == 1
        assert "gains-gate triggered" in entries[0]["subject"]

    def test_gains_gate_no_probe_when_healthy(self):
        """No violations -> no evidence entry recorded."""
        gate = FleetGainsGate()

        mock_health = {
            "status": "ok",
            "nodeId": "mac1",
            "channel_health": {
                "mac2": {"score": "6/7", "broken": ["ssh"]},
            },
        }

        tmp_db = tempfile.mktemp(suffix=".db")
        ledger = EvidenceLedger(db_path=tmp_db)

        with patch.object(gate, "_get_health", return_value=mock_health):
            with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=ledger):
                probe_result = gate._probe_fleet_health_on_gate(
                    MagicMock(passed=True)
                )

        assert probe_result is not None
        assert probe_result["violations"] == 0

        # No evidence entry since no violations
        entries = ledger.query(event_type="fleet_health_probe")
        assert len(entries) == 0

    def test_gains_gate_probe_no_health_data(self):
        """No health data -> probe returns None gracefully."""
        gate = FleetGainsGate()

        with patch.object(gate, "_get_health", return_value=None):
            probe_result = gate._probe_fleet_health_on_gate(
                MagicMock(passed=False)
            )

        assert probe_result is None

    def test_gains_gate_run_triggers_probe(self):
        """Full flow: FleetGainsGate.run() calls _probe_fleet_health_on_gate."""
        gate = FleetGainsGate()

        mock_health = {
            "status": "ok",
            "nodeId": "mac1",
            "uptime_s": 1000,
            "transport": "nats",
            "nats_url": "nats://127.0.0.1:4222",
            "peers": {"mac2": {}},
            "registered_types": ["context", "health"],
            "dedup_size": 50,
            "channel_health": {
                "mac2": {"score": "2/7", "broken": ["nats", "http", "chief", "seed", "ssh"]},
            },
        }

        with patch.object(gate, "_get_health", return_value=mock_health):
            with patch.object(gate, "_probe_fleet_health_on_gate") as mock_probe:
                with patch("multifleet.evidence_ledger.EvidenceLedger"):
                    # Patch out other checks that need network
                    with patch.object(gate, "_check_eventbus_running",
                                      return_value=MagicMock(passed=True, name="eventbus_running",
                                                             message="ok", latency_ms=0, critical=False,
                                                             to_dict=lambda: {"name": "eventbus_running",
                                                                              "passed": True, "message": "ok",
                                                                              "latency_ms": 0, "critical": False})):
                        result = gate.run(quick=True)

        # Verify probe was called with the gate result
        mock_probe.assert_called_once()


# ======================================================================
# INTEGRATION: Repair gate -> surgeon -> evidence -> corrigibility chain
# ======================================================================


class TestIntegration_RepairToEvidenceToCorrigibility:
    """End-to-end: repair triggers surgeon, surgeon result recorded to
    evidence, channel degradation recorded to corrigibility."""

    def test_critical_repair_full_chain(self):
        """Critical channel degradation -> surgeon review -> evidence + corrigibility."""
        gate = FleetRepairGate()
        tmp_evidence_db = tempfile.mktemp(suffix=".db")
        evidence_ledger = EvidenceLedger(db_path=tmp_evidence_db)
        tmp_corrig_db = tempfile.mktemp(suffix=".db")
        corrig_ledger = CorrigibilityLedger(db_path=tmp_corrig_db)

        channel_health = {
            "mac2": {
                "score": 20,  # Below 50 = unhealthy
                "last_seen": time.time(),
                "broken": ["nats", "http", "chief", "seed"],
            },
        }

        mock_adapter = MagicMock()
        mock_adapter.request_cross_exam.return_value = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": True,
            "dissents": [],
            "confidence": 0.85,
            "output": "Repair approved",
            "source": "test",
        }

        with patch(_SURGEON_ADAPTER_GATES, return_value=mock_adapter):
            with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=evidence_ledger):
                with patch("multifleet.corrigibility_ledger.CorrigibilityLedger", return_value=corrig_ledger):
                    result = gate.check(
                        "mac2", "reconnect_nats",
                        channel_health=channel_health,
                        node_id="mac1",
                    )

        # Gate should pass (target unhealthy + valid action + no cooldown)
        assert result.passed is True

        # Evidence ledger should have gate_activation from _record_audit
        gate_entries = evidence_ledger.query(event_type="gate_activation")
        assert len(gate_entries) >= 1
