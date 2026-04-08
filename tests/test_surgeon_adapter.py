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
