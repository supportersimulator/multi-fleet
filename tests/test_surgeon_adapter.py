"""Tests for surgeon_adapter — verifies the bridge between multi-fleet and 3-surgeons."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# Ensure multi-fleet package is importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


class TestSurgeonAdapterDegraded:
    """When surgery_bridge is not importable, adapter degrades gracefully."""

    def test_unavailable_when_bridge_missing(self):
        """Adapter reports unavailable when memory.surgery_bridge can't import."""
        with patch.dict(sys.modules, {"memory.surgery_bridge": None, "memory": types.ModuleType("memory")}):
            # Force re-import to trigger ImportError path
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            adapter._bridge_module = None
            adapter._bridge_available = False
            assert adapter.is_available() is False

    def test_check_consensus_degraded(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False
        result = adapter.check_consensus("test claim")
        assert result["resolved"] is False
        assert result["source"] == "degraded"
        assert "error" in result

    def test_request_review_degraded(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False
        result = adapter.request_review("test subject")
        assert result["status"] == "degraded"
        assert result["review_type"] == "none"

    def test_get_state_degraded(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False
        state = adapter.get_state()
        assert state["phase"] == "unavailable"
        assert state["abstention"] is True


class TestSurgeonAdapterWithBridge:
    """When bridge is available, adapter queries real state."""

    def _make_adapter_with_mock(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        return adapter, mock_bridge

    def test_is_available_true(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.bridge_status.return_value = {"status": "ok", "path": "direct"}
        assert adapter.is_available() is True

    def test_is_available_false_on_error(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.bridge_status.return_value = {"status": "error"}
        assert adapter.is_available() is False

    def test_check_consensus_resolved(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.9, "dissent": []},
            "path": "direct",
        }
        result = adapter.check_consensus("change is safe")
        assert result["resolved"] is True
        assert result["confidence"] == 0.9
        assert result["dissent"] == []
        mock_bridge.consensus.assert_called_once_with("change is safe")

    def test_check_consensus_caching(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.85, "dissent": []},
            "path": "direct",
        }
        r1 = adapter.check_consensus("same claim")
        r2 = adapter.check_consensus("same claim")
        assert r2["cached"] is True
        assert mock_bridge.consensus.call_count == 1  # Only called once

    def test_check_consensus_error_handling(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.consensus.side_effect = RuntimeError("connection refused")
        result = adapter.check_consensus("broken")
        assert result["resolved"] is False
        assert result["source"] == "error"
        assert "connection refused" in result["error"]

    def test_request_review_cardio(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "LGTM",
            "path": "direct",
        }
        result = adapter.request_review("schema migration", evidence=["test passes"])
        assert result["review_type"] == "cardio_review"
        assert result["status"] == "ok"
        call_arg = mock_bridge.cardio_review.call_args[0][0]
        assert "schema migration" in call_arg
        assert "test passes" in call_arg

    def test_request_review_fallback_to_consensus(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.cardio_review.side_effect = RuntimeError("unavailable")
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": "consensus reached",
            "path": "subprocess",
        }
        result = adapter.request_review("fallback test")
        assert result["review_type"] == "consensus_fallback"

    def test_get_state_after_consensus(self):
        adapter, mock_bridge = self._make_adapter_with_mock()
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.8, "dissent": ["minor"]},
            "path": "direct",
        }
        adapter.check_consensus("task done")
        state = adapter.get_state()
        assert state["phase"] == "close"  # resolved -> close
        assert state["disagreement_score"] == pytest.approx(0.2, abs=0.01)
        assert state["correction_loops"] == 1  # one dissent


class TestAcceptanceGateWithAdapter:
    """Verify acceptance gate uses adapter instead of trusting caller."""

    def test_keyword_evaluate_queries_adapter(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        from multifleet.acceptance_gate import AcceptanceGate

        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        mock_bridge.bridge_status.return_value = {"status": "ok"}
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.9, "dissent": []},
            "path": "direct",
        }

        gate = AcceptanceGate(surgeon_adapter=adapter)
        # Caller says surgeon_resolved=True, but adapter verifies
        decision = gate.evaluate(
            surgeon_resolved=True,  # This alone used to be trusted
            surgeon_intensity="standard",
        )
        # Adapter was actually queried
        mock_bridge.consensus.assert_called_once()
        assert decision.accepted is True

    def test_keyword_evaluate_blocks_when_adapter_disagrees(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        from multifleet.acceptance_gate import AcceptanceGate

        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        mock_bridge.bridge_status.return_value = {"status": "ok"}
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": False, "confidence": 0.3, "dissent": ["not safe"]},
            "path": "direct",
        }

        gate = AcceptanceGate(surgeon_adapter=adapter)
        # Caller claims resolved, but adapter says no
        decision = gate.evaluate(
            surgeon_resolved=True,
            surgeon_intensity="full",
        )
        assert decision.accepted is False
        assert any("not resolved" in b for b in decision.blocking_issues)


class TestNodeOverlayWithAdapter:
    """Verify overlay manager can use adapter for live surgeon state."""

    def test_update_from_surgeon_with_adapter(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        from multifleet.node_overlay import NodeOverlayManager

        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        mock_bridge.bridge_status.return_value = {"status": "ok"}
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": {"resolved": True, "confidence": 0.95, "dissent": []},
            "path": "direct",
        }
        # Prime the adapter state
        adapter.check_consensus("test")

        mgr = NodeOverlayManager()
        overlay = mgr.update_from_surgeon("mac2", surgeon_adapter=adapter)
        assert overlay.surgeon.phase == "close"
        assert overlay.surgeon.disagreement_score == pytest.approx(0.05, abs=0.01)


class TestCheckAvailability:
    """Test per-surgeon availability checks."""

    def _make_adapter(self, bridge_available=False, bridge_module=None):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = bridge_module
        adapter._bridge_available = bridge_available
        return adapter

    def test_atlas_always_available(self):
        adapter = self._make_adapter()
        result = adapter.check_availability()
        assert result["atlas"]["available"] is True
        assert result["atlas"]["model"] == "claude-opus"

    @patch.dict("os.environ", {"CONTEXT_DNA_OPENAI": "sk-test123"})
    def test_cardiologist_available_with_key(self):
        adapter = self._make_adapter()
        result = adapter.check_availability()
        assert result["cardiologist"]["available"] is True
        assert result["cardiologist"]["reason"] == "api_key_present"

    def test_cardiologist_unavailable_without_key(self):
        import os
        old_cdna = os.environ.pop("CONTEXT_DNA_OPENAI", None)
        old_oai = os.environ.pop("OPENAI_API_KEY", None)
        try:
            adapter = self._make_adapter()
            result = adapter.check_availability()
            assert result["cardiologist"]["available"] is False
        finally:
            if old_cdna is not None:
                os.environ["CONTEXT_DNA_OPENAI"] = old_cdna
            if old_oai is not None:
                os.environ["OPENAI_API_KEY"] = old_oai

    def test_neurologist_unavailable_when_port_down(self):
        adapter = self._make_adapter()
        result = adapter.check_availability()
        # Port 5044 won't be running in test env
        assert result["neurologist"]["available"] is False
        assert "5044" in result["neurologist"]["reason"]

    def test_any_available_always_true(self):
        """Atlas is always available, so any_available should always be True."""
        adapter = self._make_adapter()
        result = adapter.check_availability()
        assert result["any_available"] is True

    def test_bridge_info_present(self):
        adapter = self._make_adapter()
        result = adapter.check_availability()
        assert "bridge" in result

    def test_bridge_status_included_when_available(self):
        mock_bridge = MagicMock()
        mock_bridge.bridge_status.return_value = {"mode": "auto", "effective_route": "subprocess"}
        adapter = self._make_adapter(bridge_available=True, bridge_module=mock_bridge)
        result = adapter.check_availability()
        assert result["bridge"]["available"] is True
        assert result["bridge"]["mode"] == "auto"


class TestRequestCrossExam:
    """Test 3-surgeon cross-examination routing."""

    def _make_adapter_with_bridge(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        return adapter, mock_bridge

    def test_cross_exam_via_cardio_review(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "The decision appears sound. Consensus agreed.",
            "path": "subprocess",
        }

        result = adapter.request_cross_exam("merge decision", {"files": 3, "risk": "low"})
        assert "cardiologist" in result["surgeons_consulted"]
        assert "atlas" in result["surgeons_consulted"]
        assert result["source"] == "subprocess"

    def test_cross_exam_falls_to_neurologist(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.side_effect = Exception("cardio down")
        mock_bridge.neurologist_challenge.return_value = {
            "status": "ok",
            "output": "Have you considered edge cases?",
            "path": "direct",
        }

        result = adapter.request_cross_exam("arch change", {})
        assert "neurologist" in result["surgeons_consulted"]
        # challenge != consensus
        assert result["consensus"] is False

    def test_cross_exam_falls_to_consensus(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.side_effect = Exception("cardio down")
        mock_bridge.neurologist_challenge.side_effect = Exception("neuro down")
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": "consensus reached",
            "path": "subprocess",
        }

        result = adapter.request_cross_exam("simple change", {})
        assert result["source"] == "subprocess"

    def test_cross_exam_all_fail_returns_error(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.side_effect = Exception("fail1")
        mock_bridge.neurologist_challenge.side_effect = Exception("fail2")
        mock_bridge.consensus.side_effect = Exception("fail3")

        result = adapter.request_cross_exam("doomed", {})
        assert result["consensus"] is False
        assert result["source"] == "error"

    def test_cross_exam_degraded_without_bridge(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        result = adapter.request_cross_exam("anything", {})
        assert result["source"] == "degraded"
        assert result["consensus"] is False
        assert result["surgeons_consulted"] == ["atlas"]

    def test_evidence_included_in_prompt(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "Agreed.",
            "path": "direct",
        }

        adapter.request_cross_exam("test topic", {"risk": "high", "files_changed": 12})
        call_arg = mock_bridge.cardio_review.call_args[0][0]
        assert "risk: high" in call_arg
        assert "files_changed: 12" in call_arg


class TestValidateAcceptance:
    """Test task validation — the fix for the silent bypass bug."""

    def _make_adapter_with_bridge(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        return adapter, mock_bridge

    def test_validation_routes_through_cross_exam(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "Agreed. Consensus reached with high confidence.",
            "path": "direct",
        }

        result = adapter.validate_acceptance("task-001", {"claim": "feature is safe"})
        assert result["task_id"] == "task-001"
        assert "surgeons_consulted" in result
        mock_bridge.cardio_review.assert_called_once()

    def test_validation_degraded_returns_false(self):
        """Bridge down means validated=False, never faked True."""
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        result = adapter.validate_acceptance("task-002", {"claim": "something"})
        assert result["validated"] is False
        assert result["surgeon_resolved"] is False
        assert result["source"] == "degraded"
        assert "unavailable" in result["reason"]

    def test_validation_never_trusts_caller(self):
        """Even if caller injects surgeon_resolved=True in result, adapter ignores it."""
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        result = adapter.validate_acceptance("task-fake", {"surgeon_resolved": True})
        assert result["surgeon_resolved"] is False

    def test_high_confidence_sets_resolved(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "Agreed. Consensus: this is correct.",
            "path": "direct",
        }

        result = adapter.validate_acceptance("task-003", {"claim": "safe change"})
        # consensus in output -> resolved=True, confidence=0.8 >= 0.6
        assert result["surgeon_resolved"] is True

    def test_low_confidence_not_resolved(self):
        adapter, mock_bridge = self._make_adapter_with_bridge()
        mock_bridge.cardio_review.side_effect = Exception("fail")
        mock_bridge.neurologist_challenge.side_effect = Exception("fail")
        mock_bridge.consensus.return_value = {
            "status": "ok",
            "output": "unclear results",
            "path": "subprocess",
        }

        result = adapter.validate_acceptance("task-004", {"claim": "risky"})
        # "unclear" -> no consensus/dissent keywords -> confidence=0.5, resolved=False
        assert result["surgeon_resolved"] is False


class TestGetSurgeonState:
    """Test full dashboard state retrieval."""

    def test_state_includes_all_keys(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        state = adapter.get_surgeon_state()
        assert "atlas" in state
        assert "cardiologist" in state
        assert "neurologist" in state
        assert "bridge" in state
        assert "counters" in state
        assert "overlay" in state

    def test_atlas_always_online(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        state = adapter.get_surgeon_state()
        assert state["atlas"]["status"] == "online"
        assert state["atlas"]["model"] == "claude-opus"

    def test_counters_in_state(self):
        from multifleet.surgeon_adapter import SurgeonAdapter
        adapter = SurgeonAdapter()
        adapter._bridge_module = None
        adapter._bridge_available = False

        state = adapter.get_surgeon_state()
        assert isinstance(state["counters"], dict)
        assert "cross_exams" in state["counters"]
        assert "validations" in state["counters"]


class TestAcceptanceGateValidateAcceptance:
    """Verify acceptance gate now routes through validate_acceptance instead of check_consensus."""

    def test_standard_intensity_uses_validate_acceptance(self):
        """The gate should call validate_acceptance for standard/full intensity."""
        from multifleet.surgeon_adapter import SurgeonAdapter
        from multifleet.acceptance_gate import AcceptanceGate

        adapter = SurgeonAdapter()
        mock_bridge = MagicMock()
        adapter._bridge_module = mock_bridge
        adapter._bridge_available = True
        mock_bridge.bridge_status.return_value = {"status": "ok"}
        # The validate_acceptance path calls request_cross_exam -> cardio_review
        mock_bridge.cardio_review.return_value = {
            "status": "ok",
            "output": "Agreed. Consensus: accepted.",
            "path": "direct",
        }

        gate = AcceptanceGate(surgeon_adapter=adapter)
        decision = gate.evaluate(
            surgeon_resolved=False,  # Caller says not resolved
            surgeon_intensity="standard",
        )
        # But adapter says it IS resolved (consensus in output)
        mock_bridge.cardio_review.assert_called_once()
        assert decision.accepted is True
