"""Tests for WisdomConsumer — the wisdom feedback loop."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from multifleet.wisdom_consumer import (
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
