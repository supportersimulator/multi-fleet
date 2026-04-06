"""Tests for multifleet.liaison — Fleet Liaison Agent message processing."""
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.liaison import (
    ESCALATION_MAP,
    EscalationLevel,
    FleetLiaison,
    LiaisonState,
    create_liaison,
)


@pytest.fixture
def liaison():
    return FleetLiaison(node_id="mac1")


# ── Escalation classification ──


class TestEscalationClassification:
    """Message type -> correct escalation level."""

    @pytest.mark.parametrize("msg_type,expected", [
        ("heartbeat", EscalationLevel.SILENT),
        ("ack", EscalationLevel.SILENT),
        ("sync", EscalationLevel.SILENT),
        ("branch_status", EscalationLevel.SILENT),
        ("context", EscalationLevel.INFO),
        ("repair", EscalationLevel.INFO),
        ("local_verdict", EscalationLevel.INFO),
        ("merge_candidate", EscalationLevel.INFO),
        ("task_brief", EscalationLevel.URGENT),
        ("critique", EscalationLevel.URGENT),
        ("chief_decision", EscalationLevel.URGENT),
        ("alert", EscalationLevel.CRITICAL),
    ])
    def test_default_escalation_levels(self, liaison, msg_type, expected):
        level = liaison.process_message({"type": msg_type, "from": "mac2"})
        assert level == expected

    def test_unknown_type_defaults_to_info(self, liaison):
        level = liaison.process_message({"type": "some_new_type", "from": "mac2"})
        assert level == EscalationLevel.INFO

    def test_missing_type_defaults_to_context_info(self, liaison):
        level = liaison.process_message({"from": "mac2"})
        assert level == EscalationLevel.INFO


class TestBlockingCritiqueOverride:
    """Blocking critique escalation override."""

    def test_blocking_critique_escalates_to_critical(self, liaison):
        msg = {
            "type": "critique",
            "from": "mac2",
            "payload": {"blocking_concerns": ["API break"]},
        }
        level = liaison.process_message(msg)
        assert level == EscalationLevel.CRITICAL

    def test_non_blocking_critique_stays_urgent(self, liaison):
        msg = {
            "type": "critique",
            "from": "mac2",
            "payload": {"blocking_concerns": []},
        }
        level = liaison.process_message(msg)
        assert level == EscalationLevel.URGENT

    def test_critique_no_payload_stays_urgent(self, liaison):
        msg = {"type": "critique", "from": "mac2"}
        level = liaison.process_message(msg)
        assert level == EscalationLevel.URGENT


# ── State accumulation ──


class TestStateAccumulation:
    """Pending reviews, rebuttals, unread tracking."""

    def test_task_brief_adds_pending_review(self, liaison):
        msg = {"type": "task_brief", "from": "chief", "payload": {"subject": "task1"}}
        liaison.process_message(msg)
        assert len(liaison.state.pending_reviews) == 1
        assert liaison.state.pending_reviews[0] is msg

    def test_chief_decision_adds_pending_review(self, liaison):
        msg = {"type": "chief_decision", "from": "chief", "payload": {"subject": "merge X"}}
        liaison.process_message(msg)
        assert len(liaison.state.pending_reviews) == 1

    def test_critique_adds_pending_review(self, liaison):
        msg = {"type": "critique", "from": "mac2", "payload": {"subject": "review"}}
        liaison.process_message(msg)
        assert len(liaison.state.pending_reviews) == 1

    def test_local_verdict_adds_rebuttal(self, liaison):
        msg = {
            "type": "local_verdict",
            "from": "mac2",
            "payload": {"task_id": "task-123", "summary": "done"},
        }
        liaison.process_message(msg)
        assert len(liaison.state.active_rebuttals) == 1
        assert liaison.state.active_rebuttals[0]["task_id"] == "task-123"

    def test_duplicate_verdict_task_id_not_duplicated(self, liaison):
        for _ in range(3):
            liaison.process_message({
                "type": "local_verdict",
                "from": "mac2",
                "payload": {"task_id": "task-123"},
            })
        assert len(liaison.state.active_rebuttals) == 1

    def test_different_verdict_task_ids_tracked(self, liaison):
        for i in range(3):
            liaison.process_message({
                "type": "local_verdict",
                "from": "mac2",
                "payload": {"task_id": f"task-{i}"},
            })
        assert len(liaison.state.active_rebuttals) == 3

    def test_unread_messages_accumulate(self, liaison):
        for i in range(5):
            liaison.process_message({"type": "context", "from": f"mac{i}"})
        assert len(liaison.state.unread_messages) == 5

    def test_heartbeat_updates_timestamp(self, liaison):
        assert liaison.state.last_heartbeat == 0.0
        liaison.process_message({"type": "heartbeat", "from": "mac2"})
        assert liaison.state.last_heartbeat > 0

    def test_unread_capped_at_max(self, liaison):
        for i in range(250):
            liaison.process_message({"type": "context", "from": "mac2"})
        assert len(liaison.state.unread_messages) <= 200


# ── Batch injection ──


class TestBatchInjection:
    """get_batch_for_injection() formatting and queue clearing."""

    def test_returns_none_when_empty(self, liaison):
        assert liaison.get_batch_for_injection() is None

    def test_returns_markdown_with_messages(self, liaison):
        liaison.process_message({"type": "context", "from": "mac2", "payload": {"subject": "hello"}})
        batch = liaison.get_batch_for_injection()
        assert batch is not None
        assert "## Fleet Update" in batch
        assert "mac2" in batch
        assert "hello" in batch

    def test_clears_unread_after_batch(self, liaison):
        liaison.process_message({"type": "context", "from": "mac2"})
        liaison.get_batch_for_injection()
        assert len(liaison.state.unread_messages) == 0
        assert liaison.get_batch_for_injection() is None

    def test_groups_by_escalation_level(self, liaison):
        liaison.process_message({"type": "alert", "from": "mac2", "payload": {"subject": "fire"}})
        liaison.process_message({"type": "context", "from": "mac3", "payload": {"subject": "fyi"}})
        batch = liaison.get_batch_for_injection()
        # CRITICAL section should appear before INFO section
        crit_pos = batch.index("[CRITICAL]")
        info_pos = batch.index("[INFO]")
        assert crit_pos < info_pos

    def test_includes_state_summary_when_reviews_pending(self, liaison):
        liaison.process_message({"type": "task_brief", "from": "chief", "payload": {"subject": "work"}})
        batch = liaison.get_batch_for_injection()
        assert "review(s) pending" in batch


# ── Mark read ──


class TestMarkRead:
    """mark_read() clears the unread queue."""

    def test_mark_read_all(self, liaison):
        for i in range(3):
            liaison.process_message({"type": "context", "from": "mac2"})
        liaison.mark_read()
        assert len(liaison.state.unread_messages) == 0

    def test_mark_read_partial(self, liaison):
        for i in range(5):
            liaison.process_message({"type": "context", "from": f"mac{i}"})
        liaison.mark_read(count=3)
        assert len(liaison.state.unread_messages) == 2


# ── Callbacks ──


class TestCallbacks:
    """Escalation callback firing."""

    def test_callback_fires_on_matching_level(self, liaison):
        cb = MagicMock()
        liaison.register_callback(EscalationLevel.URGENT, cb)
        msg = {"type": "task_brief", "from": "chief"}
        liaison.process_message(msg)
        cb.assert_called_once_with(msg, EscalationLevel.URGENT)

    def test_callback_not_fired_on_different_level(self, liaison):
        cb = MagicMock()
        liaison.register_callback(EscalationLevel.CRITICAL, cb)
        liaison.process_message({"type": "context", "from": "mac2"})
        cb.assert_not_called()

    def test_callback_exception_does_not_propagate(self, liaison):
        cb = MagicMock(side_effect=RuntimeError("boom"))
        liaison.register_callback(EscalationLevel.INFO, cb)
        # Should not raise
        level = liaison.process_message({"type": "context", "from": "mac2"})
        assert level == EscalationLevel.INFO
        cb.assert_called_once()


# ── Summary generation ──


class TestSummaryGeneration:
    """_extract_summary() and LiaisonState.summary()."""

    def test_extract_subject(self, liaison):
        msg = {"type": "context", "payload": {"subject": "hello world"}}
        assert liaison._extract_summary(msg) == "hello world"

    def test_extract_body_string(self, liaison):
        msg = {"type": "context", "payload": {"body": "long message here"}}
        assert liaison._extract_summary(msg) == "long message here"

    def test_extract_truncates_to_100(self, liaison):
        msg = {"type": "context", "payload": {"subject": "x" * 200}}
        summary = liaison._extract_summary(msg)
        assert len(summary) == 100

    def test_extract_nested_body_dict(self, liaison):
        msg = {"type": "context", "payload": {"body": {"summary": "nested summary"}}}
        assert liaison._extract_summary(msg) == "nested summary"

    def test_extract_fallback_to_type(self, liaison):
        msg = {"type": "heartbeat", "from": "mac2", "payload": {}}
        summary = liaison._extract_summary(msg)
        assert "heartbeat" in summary
        assert "mac2" in summary

    def test_state_summary_quiet(self):
        state = LiaisonState(node_id="mac1")
        assert state.summary() == "Fleet quiet"

    def test_state_summary_with_reviews(self):
        state = LiaisonState(node_id="mac1", pending_reviews=[{"x": 1}])
        assert "1 review(s) pending" in state.summary()

    def test_state_summary_combined(self):
        state = LiaisonState(
            node_id="mac1",
            pending_reviews=[{"x": 1}],
            active_rebuttals=[{"task_id": "t1"}],
            unread_messages=[{"m": 1}, {"m": 2}],
        )
        s = state.summary()
        assert "1 review(s) pending" in s
        assert "1 rebuttal(s) active" in s
        assert "2 unread" in s


# ── Serialization ──


class TestSerialization:
    """to_dict() for MCP tool and persistence."""

    def test_to_dict_empty_state(self, liaison):
        d = liaison.to_dict()
        assert d["node_id"] == "mac1"
        assert d["pending_reviews"] == 0
        assert d["active_rebuttals"] == 0
        assert d["unread_count"] == 0
        assert d["summary"] == "Fleet quiet"

    def test_to_dict_after_messages(self, liaison):
        liaison.process_message({"type": "task_brief", "from": "chief", "payload": {}})
        liaison.process_message({"type": "context", "from": "mac2", "payload": {}})
        d = liaison.to_dict()
        assert d["pending_reviews"] == 1
        assert d["unread_count"] == 2


# ── EventBus publishing ──


class TestEventBusPublishing:
    """_publish_event() integration with EventBus."""

    def test_publish_with_injected_bus(self, liaison):
        mock_bus = MagicMock()
        liaison._event_bus = mock_bus
        liaison.process_message({"type": "context", "from": "mac2", "payload": {"subject": "hi"}})
        mock_bus.emit.assert_called_once()
        call_args = mock_bus.emit.call_args
        assert "liaison.message.context" in call_args[0]

    def test_publish_graceful_when_bus_unavailable(self, liaison):
        """When EventBus import fails, should silently degrade."""
        with patch.dict("sys.modules", {"multifleet.eventbus": None}):
            # Force re-import failure
            liaison._event_bus = None
            # Should not raise
            liaison.process_message({"type": "context", "from": "mac2"})


# ── Factory ──


class TestFactory:
    """create_liaison() auto-detection."""

    def test_explicit_node_id(self):
        l = create_liaison("mac3")
        assert l.node_id == "mac3"

    def test_env_node_id(self):
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac2"}):
            l = create_liaison()
            assert l.node_id == "mac2"

    def test_default_node_id(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove MULTIFLEET_NODE_ID if present
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            l = create_liaison()
            assert l.node_id == "unknown"
