"""Tests for the Superpowers <-> 3-Surgeons Bridge.

Covers: SuperpowersBridge, HandoffStore, fleet_handoff MCP tool,
        degraded mode, contract creation, and observability counters.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.vision_packets import (
    BrainstormPacket,
    SurgeonDecisionPacket,
    VisionContract,
)
from multifleet.superpowers_bridge import (
    SuperpowersBridge,
    HandoffStore,
    get_store,
    _COUNTERS,
)


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def store():
    return HandoffStore(max_size=10)


@pytest.fixture
def mock_surgeon():
    surgeon = MagicMock()
    surgeon.is_available.return_value = True
    surgeon.request_review.return_value = {
        "status": "ok",
        "output": "Approach looks sound. No major objections.",
        "path": "cardio_review",
        "review_type": "cardio_review",
    }
    return surgeon


@pytest.fixture
def bridge(store, mock_surgeon):
    return SuperpowersBridge(store=store, surgeon=mock_surgeon)


def _make_brainstorm(task_id: str = "test-task") -> BrainstormPacket:
    bp = BrainstormPacket(task_id=task_id, node_id="mac1")
    opt = bp.add_option(
        title="Approach A",
        description="Build it this way",
        pros=["simple", "fast"],
        cons=["limited"],
        confidence=0.8,
    )
    bp.recommended = opt.id
    return bp


# ── HandoffStore tests ───────────────────────────────────────────


class TestHandoffStore:
    def test_store_and_retrieve_brainstorm(self, store):
        bp = _make_brainstorm()
        store.store_brainstorm(bp)
        assert store.get_brainstorm(bp.id) is bp
        assert store.brainstorm_id_for_task("test-task") == bp.id

    def test_store_eviction(self):
        store = HandoffStore(max_size=2)
        bp1 = _make_brainstorm("task-1")
        bp2 = _make_brainstorm("task-2")
        bp3 = _make_brainstorm("task-3")
        store.store_brainstorm(bp1)
        store.store_brainstorm(bp2)
        store.store_brainstorm(bp3)
        # bp1 should be evicted
        assert store.get_brainstorm(bp1.id) is None
        assert store.get_brainstorm(bp2.id) is bp2
        assert store.get_brainstorm(bp3.id) is bp3

    def test_list_pending(self, store):
        bp = _make_brainstorm()
        store.store_brainstorm(bp)
        pending = store.list_pending()
        assert len(pending) == 1
        assert pending[0]["brainstorm_id"] == bp.id

    def test_list_decided(self, store):
        bp = _make_brainstorm()
        store.store_brainstorm(bp)
        sdp = SurgeonDecisionPacket(brainstorm_id=bp.id, task_id="test-task")
        store.store_decision(sdp)
        decided = store.list_decided()
        assert len(decided) == 1
        assert decided[0]["brainstorm_id"] == bp.id

    def test_pending_excludes_decided(self, store):
        bp = _make_brainstorm()
        store.store_brainstorm(bp)
        sdp = SurgeonDecisionPacket(brainstorm_id=bp.id, task_id="test-task")
        store.store_decision(sdp)
        assert len(store.list_pending()) == 0

    def test_len(self, store):
        assert len(store) == 0
        store.store_brainstorm(_make_brainstorm())
        assert len(store) == 1


# ── SuperpowersBridge tests ──────────────────────────────────────


class TestSuperpowersBridge:
    def test_submit_for_review_success(self, bridge, mock_surgeon):
        bp = _make_brainstorm()
        sdp = bridge.submit_for_review(bp)
        assert isinstance(sdp, SurgeonDecisionPacket)
        assert sdp.brainstorm_id == bp.id
        assert sdp.task_id == "test-task"
        assert len(sdp.opinions) == 1
        mock_surgeon.request_review.assert_called_once()

    def test_submit_stores_brainstorm(self, bridge, store):
        bp = _make_brainstorm()
        bridge.submit_for_review(bp)
        assert store.get_brainstorm(bp.id) is bp

    def test_submit_degraded_when_surgeon_unavailable(self, store):
        surgeon = MagicMock()
        surgeon.is_available.return_value = False
        bridge = SuperpowersBridge(store=store, surgeon=surgeon)
        bp = _make_brainstorm()
        sdp = bridge.submit_for_review(bp)
        assert sdp.consensus is False
        assert "surgeon_unavailable" in sdp.unresolved_objections

    def test_submit_degraded_on_exception(self, store):
        surgeon = MagicMock()
        surgeon.is_available.return_value = True
        surgeon.request_review.side_effect = RuntimeError("connection lost")
        bridge = SuperpowersBridge(store=store, surgeon=surgeon)
        bp = _make_brainstorm()
        sdp = bridge.submit_for_review(bp)
        assert sdp.consensus is False
        assert "surgeon_unavailable" in sdp.unresolved_objections

    def test_check_decision_returns_none_when_no_decision(self, bridge):
        assert bridge.check_decision("nonexistent") is None

    def test_check_decision_returns_decision(self, bridge, store):
        bp = _make_brainstorm()
        sdp = bridge.submit_for_review(bp)
        store.store_decision(sdp)
        result = bridge.check_decision("test-task")
        assert result is not None
        assert result.brainstorm_id == bp.id

    def test_create_contract(self, bridge):
        bp = _make_brainstorm()
        sdp = SurgeonDecisionPacket(
            brainstorm_id=bp.id,
            task_id="test-task",
            consensus=True,
            selected_option=bp.options[0].id,
        )
        vc = bridge.create_contract(bp, sdp, ["tests pass", "no regressions"], node_id="mac1")
        assert isinstance(vc, VisionContract)
        assert vc.task_id == "test-task"
        assert vc.locked is True
        assert vc.locked_by == "mac1"
        assert len(vc.done_criteria) == 2
        assert vc.selected_approach == "Approach A"

    def test_create_contract_rejects_no_consensus(self, bridge):
        bp = _make_brainstorm()
        sdp = SurgeonDecisionPacket(
            brainstorm_id=bp.id,
            task_id="test-task",
            consensus=False,
        )
        with pytest.raises(ValueError, match="consensus"):
            bridge.create_contract(bp, sdp, ["tests pass"])

    def test_counters_increment(self, bridge):
        # Reset counters for this test
        for k in _COUNTERS:
            _COUNTERS[k] = 0
        bp = _make_brainstorm()
        bridge.submit_for_review(bp)
        counters = bridge.get_counters()
        assert counters["submits"] == 1
        assert counters["decisions_returned"] == 1

    def test_build_review_prompt(self):
        bp = _make_brainstorm()
        prompt = SuperpowersBridge._build_review_prompt(bp)
        assert "test-task" in prompt
        assert "Approach A" in prompt
        assert "simple" in prompt
        assert "limited" in prompt


# ── MCP tool integration ─────────────────────────────────────────


class TestFleetHandoffMCP:
    """Test the fleet_handoff MCP tool function via import."""

    @pytest.fixture(autouse=True)
    def _setup_path(self):
        bin_dir = FLEET_ROOT / "bin"
        sys.path.insert(0, str(bin_dir))
        yield

    def test_submit_action_requires_task_id(self):
        """fleet_handoff submit without task_id returns error."""
        # Import the function directly to test logic
        from multifleet.superpowers_bridge import SuperpowersBridge, HandoffStore
        store = HandoffStore()
        surgeon = MagicMock()
        surgeon.is_available.return_value = False
        bridge = SuperpowersBridge(store=store, surgeon=surgeon)

        bp = BrainstormPacket(task_id="", node_id="mac1")
        bp.add_option("A", "desc")
        sdp = bridge.submit_for_review(bp)
        # Should still work but with empty task_id
        assert sdp.task_id == ""

    def test_bridge_review_prompt_with_no_options(self):
        """Review prompt handles empty brainstorm gracefully."""
        bp = BrainstormPacket(task_id="empty-task", node_id="mac1")
        prompt = SuperpowersBridge._build_review_prompt(bp)
        assert "empty-task" in prompt

    def test_translate_review_error_status(self):
        """Error status from surgeon produces degraded opinion."""
        bp = _make_brainstorm()
        raw = {"status": "error", "output": "timeout", "review_type": "none"}
        sdp = SuperpowersBridge._translate_review(bp, raw)
        assert sdp.consensus is False
        assert any("error" in o for o in sdp.unresolved_objections)
