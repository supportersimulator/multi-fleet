"""Tests for Ghost Pulse and Ghost Digest — ContextDNA subconscious."""

import json
import tempfile
import time
from pathlib import Path

import pytest

from multifleet.ghost_pulse import GhostPulse, PulseResult, run_pulse
from multifleet.ghost_digest import GhostDigest, DigestResult, EvidenceCandidate


class TestGhostPulse:

    def test_pulse_runs(self):
        ghost = GhostPulse(repo_root=".")
        result = ghost.pulse()
        assert isinstance(result, PulseResult)
        assert result.timestamp
        assert result.duration_ms >= 0

    def test_pulse_summary_all_clear(self):
        r = PulseResult()
        assert r.summary() == "all clear"

    def test_pulse_summary_with_issues(self):
        r = PulseResult(stale_beliefs_expired=3, contradictions_found=1)
        s = r.summary()
        assert "3 stale expired" in s
        assert "1 contradictions" in s

    def test_expire_stale_beliefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            old_ts = time.time() - 200000  # ~55 hours ago
            ledger.write_text(
                json.dumps({"timestamp": old_ts, "decision": "merge", "task_id": "old"}) + "\n"
                + json.dumps({"timestamp": time.time(), "decision": "merge", "task_id": "new"}) + "\n"
            )
            ghost = GhostPulse(repo_root=".", evidence_ledger_path=str(ledger))
            result = ghost.pulse()
            assert result.stale_beliefs_expired >= 1

    def test_detect_contradictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text(
                json.dumps({"task_id": "abc", "decision": "merge"}) + "\n"
                + json.dumps({"task_id": "abc", "decision": "reject"}) + "\n"
            )
            ghost = GhostPulse(repo_root=".", evidence_ledger_path=str(ledger))
            result = ghost.pulse()
            assert result.contradictions_found >= 1

    def test_detect_repeated_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text(
                json.dumps({"task_id": "fail1", "decision": "reject"}) + "\n"
                + json.dumps({"task_id": "fail1", "decision": "needs_rebuttal"}) + "\n"
            )
            ghost = GhostPulse(repo_root=".", evidence_ledger_path=str(ledger))
            result = ghost.pulse()
            assert result.repeated_failures_detected >= 1

    def test_run_pulse_convenience(self):
        result = run_pulse(repo_root=".")
        assert "stale_beliefs_expired" in result
        assert "timestamp" in result

    def test_to_dict(self):
        r = PulseResult(stale_beliefs_expired=1, warnings=["test"])
        d = r.to_dict()
        assert d["stale_beliefs_expired"] == 1
        assert "test" in d["warnings"]


class TestGhostDigest:

    def test_digest_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")
            result = digest.digest(
                task_id="test-task",
                task_intent="Fix auth",
            )
            assert isinstance(result, DigestResult)
            assert result.task_id == "test-task"
            assert len(result.evidence_candidates) >= 1  # At least the completion record

    def test_digest_with_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")
            result = digest.digest(
                task_id="synth-task",
                task_intent="Build feature",
                synthesis_result={
                    "decision": "merge",
                    "winner_branch": "mac1",
                    "reasoning": "Highest confidence with clean implementation",
                    "confidence": 0.85,
                    "dissent_preserved": [
                        {"machine": "mac2", "claim": "Over-engineered"}
                    ],
                    "cherry_pick_from": [
                        {"machine": "mac2", "commit": "abc123", "reason": "Better logging"}
                    ],
                },
            )
            # Should have: synthesis decision + cherry-pick + completion
            assert len(result.evidence_candidates) >= 3
            assert len(result.unresolved_objections) >= 1
            assert "mac2" in result.unresolved_objections[0]

    def test_digest_with_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")
            result = digest.digest(
                task_id="probe-task",
                probe_results=[{
                    "probe_id": "mirror_drift",
                    "severity": "high",
                    "confidence": 0.9,
                    "findings": [{"title": "Architecture drift detected", "evidence": ["git log"]}],
                }],
            )
            # High severity probe findings become evidence candidates
            probe_candidates = [c for c in result.evidence_candidates if c.category == "finding"]
            assert len(probe_candidates) >= 1

    def test_digest_with_blocked_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")
            result = digest.digest(
                task_id="blocked-task",
                acceptance_decision={
                    "accepted": False,
                    "reason": "2 blocking issues",
                    "blocking_issues": ["Probe: mirror drift", "Surgeon: unresolved"],
                    "required_followups": ["Update architecture docs"],
                },
            )
            assert len(result.future_warnings) >= 1
            assert "BLOCKED" in result.future_warnings[0]
            assert len(result.packet_hints) >= 1

    def test_digest_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            digest = GhostDigest(evidence_dir=tmp, node_id="test")
            digest.digest(task_id="persist-test", task_intent="Test persistence")

            files = list(Path(tmp).glob("*.json"))
            assert len(files) == 1
            data = json.loads(files[0].read_text())
            assert data["task_id"] == "persist-test"

    def test_evidence_candidate_to_dict(self):
        c = EvidenceCandidate(
            candidate_id="test",
            source_task_id="task1",
            source_node="mac1",
            title="Test finding",
            category="learning",
            content="Something useful",
            confidence=0.8,
        )
        d = c.to_dict()
        assert d["candidate_id"] == "test"
        assert d["confidence"] == 0.8

    def test_digest_summary(self):
        r = DigestResult(
            task_id="summary-test",
            evidence_candidates=[
                EvidenceCandidate("a", "t", "n", "Title", "learning", "Content", 0.5)
            ],
            unresolved_objections=["obj1"],
        )
        s = r.summary()
        assert "1 candidates" in s
        assert "1 objections" in s
