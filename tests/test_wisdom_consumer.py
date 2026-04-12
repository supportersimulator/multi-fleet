"""Tests for WisdomConsumer — the wisdom feedback loop."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from multifleet.wisdom_consumer import (
    WisdomAccuracyTracker,
    WisdomConsumer,
    WisdomEntry,
    _detect_contradiction,
    _extract_keywords,
    _is_wisdom_event,
)


class TestWisdomEntry:

    def test_to_dict(self):
        entry = WisdomEntry(
            source="evidence_ledger",
            event_type="consensus_reached",
            area="auth",
            subject="Auth token refresh validated",
            content="Fleet consensus: token refresh works after route refactor",
            timestamp=time.time(),
            node_id="mac1",
            entry_id="abc-123",
            confidence=0.9,
        )
        d = entry.to_dict()
        assert d["source"] == "evidence_ledger"
        assert d["area"] == "auth"
        assert d["confidence"] == 0.9

    def test_matches_keywords_hit(self):
        entry = WisdomEntry(
            source="file", subject="Auth token refresh", content="validated", area="auth",
        )
        assert entry.matches_keywords(["auth"], min_hits=1)
        assert entry.matches_keywords(["auth", "token"], min_hits=2)

    def test_matches_keywords_miss(self):
        entry = WisdomEntry(
            source="file", subject="Billing module", content="approved",
        )
        assert not entry.matches_keywords(["auth", "token"], min_hits=1)

    def test_matches_keywords_empty(self):
        entry = WisdomEntry(source="file", subject="test")
        assert not entry.matches_keywords([])


class TestWisdomConsumer:

    def test_empty_wisdom_returns_empty(self):
        """No wisdom sources configured — returns empty list."""
        with tempfile.TemporaryDirectory() as tmp:
            consumer = WisdomConsumer(wisdom_path=tmp)
            result = consumer.get_wisdom_for_task(["auth", "token"])
            assert result == []

    def test_keyword_matching_from_files(self):
        """Wisdom files are read and matched by keywords."""
        with tempfile.TemporaryDirectory() as tmp:
            # Write a wisdom file
            wisdom = {
                "source": "fleet-evidence",
                "entry_id": "w-001",
                "event_type": "consensus_reached",
                "node_id": "mac1",
                "subject": "Auth token refresh approved",
                "action": "success",
                "area": "auth",
                "promoted_at": time.time(),
                "original_timestamp": time.time() - 3600,
                "content": {
                    "details": "Fleet consensus: auth token refresh is safe after route refactor",
                    "confidence": 0.85,
                },
            }
            Path(tmp, "20260410-120000_consensus_reached_w001.json").write_text(
                json.dumps(wisdom)
            )

            consumer = WisdomConsumer(wisdom_path=tmp)
            matches = consumer.get_wisdom_for_task(["auth", "token"])
            assert len(matches) == 1
            assert matches[0]["area"] == "auth"

    def test_keyword_matching_no_match(self):
        """Keywords that don't match any wisdom return empty."""
        with tempfile.TemporaryDirectory() as tmp:
            wisdom = {
                "entry_id": "w-002",
                "event_type": "consensus_reached",
                "subject": "Billing calculation fixed",
                "area": "billing",
                "content": {"details": "Billing rounding error corrected"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "20260410-120000_consensus_w002.json").write_text(
                json.dumps(wisdom)
            )

            consumer = WisdomConsumer(wisdom_path=tmp)
            assert consumer.get_wisdom_for_task(["auth", "token"]) == []

    def test_feed_synthesis_no_contradictions(self):
        """When wisdom supports the decision, no warnings."""
        with tempfile.TemporaryDirectory() as tmp:
            wisdom = {
                "entry_id": "w-003",
                "event_type": "consensus_reached",
                "subject": "Auth merge approved cleanly",
                "area": "auth",
                "content": {"details": "Auth changes merged successfully"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "20260410-120000_consensus_w003.json").write_text(
                json.dumps(wisdom)
            )

            consumer = WisdomConsumer(wisdom_path=tmp)
            feedback = consumer.feed_synthesis({
                "task_id": "auth-fix-new",
                "decision": "merge",
                "reasoning": "Auth token cleanup looks clean",
                "confidence": 0.85,
            })
            assert feedback["warnings"] == []
            assert feedback["wisdom_checked"] >= 1

    def test_feed_synthesis_detects_contradiction(self):
        """When past wisdom shows rejection but decision is merge, warn."""
        with tempfile.TemporaryDirectory() as tmp:
            wisdom = {
                "entry_id": "w-004",
                "event_type": "consensus_reached",
                "subject": "Auth refactor rejected — broke token flow",
                "area": "auth",
                "content": {"details": "Auth refactor failed tests, needs_rebuttal"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "20260410-120000_consensus_w004.json").write_text(
                json.dumps(wisdom)
            )

            consumer = WisdomConsumer(wisdom_path=tmp)
            feedback = consumer.feed_synthesis({
                "task_id": "auth-refactor",
                "decision": "merge",
                "reasoning": "Auth refactor ready to merge",
                "confidence": 0.75,
            })
            assert len(feedback["warnings"]) > 0
            assert feedback["warnings"][0]["type"] == "contradiction"

    def test_feed_synthesis_empty_wisdom(self):
        """No wisdom at all — graceful empty result."""
        with tempfile.TemporaryDirectory() as tmp:
            consumer = WisdomConsumer(wisdom_path=tmp)
            feedback = consumer.feed_synthesis({
                "task_id": "new-feature",
                "decision": "merge",
                "reasoning": "Brand new feature",
                "confidence": 0.9,
            })
            assert feedback["warnings"] == []
            assert feedback["supporting"] == []
            assert feedback["wisdom_checked"] == 0

    def test_deduplication_across_sources(self):
        """Same entry_id from ledger and file should not duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            # Wisdom file
            wisdom = {
                "entry_id": "shared-id-001",
                "event_type": "consensus_reached",
                "subject": "Shared wisdom entry about auth",
                "area": "auth",
                "content": {"details": "Auth pattern validated"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "20260410-120000_consensus_shared.json").write_text(
                json.dumps(wisdom)
            )

            # Mock ledger returning same entry_id
            mock_ledger = MagicMock()
            mock_ledger.query.return_value = [{
                "entry_id": "shared-id-001",
                "event_type": "consensus_reached",
                "subject": "Shared wisdom entry about auth",
                "node_id": "mac1",
                "timestamp": time.time(),
                "payload": json.dumps({"details": "Auth pattern validated"}),
            }]

            consumer = WisdomConsumer(evidence_ledger=mock_ledger, wisdom_path=tmp)
            matches = consumer.get_wisdom_for_task(["auth"])
            # Should appear only once despite being in both sources
            ids = [m["entry_id"] for m in matches]
            assert ids.count("shared-id-001") == 1

    def test_cache_invalidation(self):
        """After invalidate_cache, next call reloads."""
        with tempfile.TemporaryDirectory() as tmp:
            consumer = WisdomConsumer(wisdom_path=tmp)
            # First call — empty
            assert consumer.get_wisdom_for_task(["auth"]) == []

            # Write a file
            wisdom = {
                "entry_id": "late-add",
                "event_type": "wisdom",
                "subject": "Auth wisdom added late",
                "content": {"details": "auth token pattern"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "late_wisdom.json").write_text(json.dumps(wisdom))

            # Still cached — won't see the file
            assert consumer.get_wisdom_for_task(["auth"]) == []

            # Invalidate cache
            consumer.invalidate_cache()
            matches = consumer.get_wisdom_for_task(["auth"])
            assert len(matches) == 1

    def test_ledger_failure_graceful_degradation(self):
        """If ledger query raises, still loads from files."""
        with tempfile.TemporaryDirectory() as tmp:
            wisdom = {
                "entry_id": "file-only",
                "event_type": "wisdom",
                "subject": "Auth from file only",
                "content": {"details": "auth backup wisdom"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "backup_wisdom.json").write_text(json.dumps(wisdom))

            mock_ledger = MagicMock()
            mock_ledger.query.side_effect = RuntimeError("DB locked")

            consumer = WisdomConsumer(evidence_ledger=mock_ledger, wisdom_path=tmp)
            matches = consumer.get_wisdom_for_task(["auth"])
            assert len(matches) == 1
            assert matches[0]["source"] == "wisdom_file"


class TestHelpers:

    def test_is_wisdom_event(self):
        assert _is_wisdom_event("consensus_reached", "")
        assert _is_wisdom_event("evidence.promoted", "")
        assert _is_wisdom_event("merge_decision", "applied_to_wisdom")
        assert not _is_wisdom_event("merge_decision", "normal merge")

    def test_extract_keywords(self):
        kw = _extract_keywords("auth-fix-123", "Fix auth token refresh flow")
        assert "auth" in kw
        assert "token" in kw
        assert "the" not in kw

    def test_detect_contradiction_merge_vs_reject(self):
        entry = WisdomEntry(
            source="file",
            subject="Auth refactor rejected",
            content="broke token flow, failed tests",
        )
        warning = _detect_contradiction("merge", entry)
        assert "contradict" in warning.lower() or "contradiction" in warning.lower()

    def test_detect_contradiction_no_conflict(self):
        entry = WisdomEntry(
            source="file",
            subject="Auth cleanup approved",
            content="clean implementation, all tests pass",
        )
        warning = _detect_contradiction("merge", entry)
        assert warning == ""

    def test_detect_contradiction_unknown_decision(self):
        entry = WisdomEntry(source="file", subject="whatever", content="stuff")
        assert _detect_contradiction("unknown_decision_type", entry) == ""


class TestSynthesisWisdomIntegration:

    def test_synthesis_attaches_wisdom_feedback(self):
        """ChiefSynthesis should attach wisdom_feedback to result metadata."""
        from multifleet.synthesis import ChiefSynthesis

        chief = ChiefSynthesis()
        result = chief.synthesize("test-task", {
            "mac1": {
                "confidence": 0.8,
                "summary": "Clean implementation",
                "branch": "race/test/mac1",
                "risks": [],
                "strengths": ["tested"],
                "dissent": [],
            },
        })

        assert "wisdom_feedback" in result.metadata
        fb = result.metadata["wisdom_feedback"]
        assert "warnings_count" in fb
        assert "wisdom_checked" in fb


class TestWisdomAccuracyTracker:

    def test_record_and_get_stats(self):
        """Basic record/read cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            tracker.record_flag("e1")
            tracker.record_flag("e1")
            tracker.record_false_positive("e1")
            stats = tracker.get_stats("e1")
            assert stats == {"flagged": 2, "confirmed": 0, "false_positive": 1}

    def test_false_positive_rate(self):
        """FP rate = false_positive / (confirmed + false_positive)."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            tracker.record_confirmed("e1")
            tracker.record_false_positive("e1")
            tracker.record_false_positive("e1")
            assert abs(tracker.false_positive_rate("e1") - 2 / 3) < 0.01

    def test_false_positive_rate_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            assert tracker.false_positive_rate("nonexistent") == 0.0

    def test_get_unreliable_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            # e1: 3 FP, 1 confirmed = 75% FP rate
            for _ in range(3):
                tracker.record_false_positive("e1")
            tracker.record_confirmed("e1")
            # e2: 1 FP, 3 confirmed = 25% FP rate
            tracker.record_false_positive("e2")
            for _ in range(3):
                tracker.record_confirmed("e2")

            unreliable = tracker.get_unreliable_entries(threshold=0.5)
            assert "e1" in unreliable
            assert "e2" not in unreliable

    def test_should_suppress(self):
        """Suppressed when false_positive count > 3."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            for _ in range(3):
                tracker.record_false_positive("e1")
            assert not tracker.should_suppress("e1")  # exactly 3 => not suppressed
            tracker.record_false_positive("e1")
            assert tracker.should_suppress("e1")  # 4 => suppressed

    def test_resolve_outcome_success(self):
        """Successful decision marks all flagged entries as false positives."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            tracker.resolve_outcome(["e1", "e2"], decision_succeeded=True)
            assert tracker.get_stats("e1")["false_positive"] == 1
            assert tracker.get_stats("e2")["false_positive"] == 1

    def test_resolve_outcome_failure(self):
        """Failed decision marks all flagged entries as confirmed."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            tracker.resolve_outcome(["e1"], decision_succeeded=False)
            assert tracker.get_stats("e1")["confirmed"] == 1

    def test_persistence_across_instances(self):
        """Data persists to JSON sidecar and survives new instance."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker1 = WisdomAccuracyTracker(wisdom_path=tmp)
            tracker1.record_flag("e1")
            tracker1.record_false_positive("e1")

            tracker2 = WisdomAccuracyTracker(wisdom_path=tmp)
            stats = tracker2.get_stats("e1")
            assert stats["flagged"] == 1
            assert stats["false_positive"] == 1

    def test_feed_synthesis_records_flags(self):
        """feed_synthesis records flags in accuracy tracker."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            wisdom = {
                "entry_id": "w-flag-test",
                "event_type": "consensus_reached",
                "subject": "Auth refactor rejected — broke token flow",
                "area": "auth",
                "content": {"details": "Auth refactor failed tests, needs_rebuttal"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "flag_test.json").write_text(json.dumps(wisdom))

            consumer = WisdomConsumer(
                wisdom_path=tmp, accuracy_tracker=tracker
            )
            feedback = consumer.feed_synthesis({
                "task_id": "auth-refactor",
                "decision": "merge",
                "reasoning": "Auth refactor ready to merge",
                "confidence": 0.75,
            })
            assert len(feedback["warnings"]) > 0
            assert "w-flag-test" in feedback["flagged_entry_ids"]
            assert tracker.get_stats("w-flag-test")["flagged"] >= 1

    def test_feed_synthesis_suppresses_unreliable(self):
        """feed_synthesis skips entries that should_suppress returns True for."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = WisdomAccuracyTracker(wisdom_path=tmp)
            # Make entry unreliable (>3 false positives)
            for _ in range(5):
                tracker.record_false_positive("w-suppressed")

            wisdom = {
                "entry_id": "w-suppressed",
                "event_type": "consensus_reached",
                "subject": "Auth refactor rejected — broke token flow",
                "area": "auth",
                "content": {"details": "Auth refactor failed tests, needs_rebuttal"},
                "original_timestamp": time.time(),
            }
            Path(tmp, "suppressed_test.json").write_text(json.dumps(wisdom))

            consumer = WisdomConsumer(
                wisdom_path=tmp, accuracy_tracker=tracker
            )
            feedback = consumer.feed_synthesis({
                "task_id": "auth-refactor",
                "decision": "merge",
                "reasoning": "Auth refactor ready to merge",
                "confidence": 0.75,
            })
            # Should be suppressed — no warnings from this entry
            assert len(feedback["warnings"]) == 0
