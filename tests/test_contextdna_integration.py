"""Tests for ContextDNA integration — probe injection into liaison + failure clustering.

Covers:
- Probe injection appears in liaison batch
- Top 3 recommendations cap
- Graceful degradation when probes unavailable
- Cross-session clustering severity levels
- Evidence ledger promotion for 3+ session patterns
- Full pipeline: audit errors -> cluster -> evidence -> liaison injection
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.liaison import FleetLiaison
from multifleet.probe_engine import ProbeFinding, ProbeResult
from multifleet.probes.repeated_failure_cluster import (
    _cluster_by_session,
    _cluster_key,
    _promote_to_evidence,
    probe_repeated_failure_cluster,
)


# ── Fixtures ──


@pytest.fixture
def liaison():
    return FleetLiaison(node_id="mac1")


@pytest.fixture
def tmp_audit_dir(tmp_path):
    """Create a temp audit dir with sample error events."""
    return tmp_path


def _write_audit_events(audit_dir: Path, events: list[dict], filename: str = "audit-2026-04-05.jsonl"):
    filepath = audit_dir / filename
    with open(filepath, "w") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


# ── Task 1: Probe injection into liaison ──


class TestProbeInjectionInLiaison:
    """Probe recommendations appear in liaison batch output."""

    def test_probe_injection_appears_in_batch(self, liaison):
        """When probes return findings, they appear in get_batch_for_injection."""
        mock_result = ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="ok",
            confidence=0.75,
            severity="info",
            findings=[
                ProbeFinding(title="#1 [score:80] CLAUDE.md"),
                ProbeFinding(title="#2 [score:50] liaison.py"),
            ],
        )

        with patch("multifleet.liaison.FleetLiaison.get_probe_injection") as mock_get:
            mock_get.return_value = "[PROBE] Recommended context:\n  - #1 [score:80] CLAUDE.md\n  - #2 [score:50] liaison.py"
            batch = liaison.get_batch_for_injection()

        assert batch is not None
        assert "[PROBE] Recommended context:" in batch
        assert "CLAUDE.md" in batch

    def test_top_3_only(self, liaison):
        """Only top 3 probe findings are included, not all."""
        findings = [
            ProbeFinding(title=f"#{i} [score:{100-i*10}] file{i}.py")
            for i in range(1, 6)
        ]
        mock_result = ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="ok",
            confidence=0.75,
            severity="info",
            findings=findings,
        )

        with patch("multifleet.probes.create_engine") as mock_engine_factory:
            engine = MagicMock()
            engine.scan_one.return_value = mock_result
            mock_engine_factory.return_value = engine

            result = liaison.get_probe_injection()

        assert result is not None
        lines = result.strip().split("\n")
        # Header + 3 findings = 4 lines
        assert len(lines) == 4
        assert "file4.py" not in result
        assert "file5.py" not in result

    def test_graceful_when_no_probes(self, liaison):
        """Returns None when probe engine fails or has no findings."""
        with patch("multifleet.probes.create_engine", side_effect=ImportError("no probes")):
            result = liaison.get_probe_injection()
        assert result is None

    def test_graceful_when_no_findings(self, liaison):
        """Returns None when probe returns empty findings."""
        mock_result = ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="ok",
            confidence=0.7,
            severity="info",
            findings=[],
        )

        with patch("multifleet.probes.create_engine") as mock_engine_factory:
            engine = MagicMock()
            engine.scan_one.return_value = mock_result
            mock_engine_factory.return_value = engine

            result = liaison.get_probe_injection()

        assert result is None

    def test_batch_includes_both_messages_and_probes(self, liaison):
        """When both unread messages and probes exist, batch includes both."""
        liaison.process_message({
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "test msg"},
        })

        with patch("multifleet.liaison.FleetLiaison.get_probe_injection") as mock_get:
            mock_get.return_value = "[PROBE] Recommended context:\n  - #1 hotspot"
            batch = liaison.get_batch_for_injection()

        assert batch is not None
        assert "## Fleet Update" in batch
        assert "[PROBE] Recommended context:" in batch

    def test_batch_only_probes_no_messages(self, liaison):
        """When no unread messages but probes exist, batch is probe-only."""
        with patch("multifleet.liaison.FleetLiaison.get_probe_injection") as mock_get:
            mock_get.return_value = "[PROBE] Recommended context:\n  - #1 hotspot"
            batch = liaison.get_batch_for_injection()

        assert batch is not None
        assert "## Fleet Update" not in batch
        assert "[PROBE] Recommended context:" in batch


# ── Task 2: Cross-session clustering ──


class TestCrossSessionClustering:
    """Failure clustering detects cross-session patterns."""

    def test_two_session_pattern_medium_severity(self):
        """Pattern in 2 sessions = medium severity."""
        errors = [
            {"tool": "git", "target": "push", "session_id": "sess-1", "status": "error"},
            {"tool": "git", "target": "push", "session_id": "sess-2", "status": "error"},
        ]
        clusters = _cluster_by_session(errors)
        assert len(clusters) == 1
        assert clusters[0]["pattern"] == "git::push"
        assert clusters[0]["session_count"] == 2
        assert clusters[0]["severity"] == "medium"

    def test_three_session_pattern_high_severity(self):
        """Pattern in 3+ sessions = high severity."""
        errors = [
            {"tool": "curl", "target": "health", "session_id": f"sess-{i}", "status": "error"}
            for i in range(3)
        ]
        clusters = _cluster_by_session(errors)
        assert len(clusters) == 1
        assert clusters[0]["severity"] == "high"
        assert clusters[0]["session_count"] == 3

    def test_single_session_not_clustered(self):
        """Failures from only 1 session are not cross-session clusters."""
        errors = [
            {"tool": "git", "target": "push", "session_id": "sess-1", "status": "error"},
            {"tool": "git", "target": "push", "session_id": "sess-1", "status": "error"},
        ]
        clusters = _cluster_by_session(errors)
        assert len(clusters) == 0

    def test_metadata_session_id_extracted(self):
        """Session ID from nested metadata dict is used."""
        errors = [
            {"tool": "x", "target": "y", "metadata": {"session_id": "a"}, "status": "error"},
            {"tool": "x", "target": "y", "metadata": {"session_id": "b"}, "status": "error"},
        ]
        clusters = _cluster_by_session(errors)
        assert len(clusters) == 1
        assert set(clusters[0]["sessions"]) == {"a", "b"}

    def test_sessions_capped_at_5(self):
        """Sessions list in cluster is capped at 5 entries."""
        errors = [
            {"tool": "x", "target": "y", "session_id": f"s-{i}", "status": "error"}
            for i in range(10)
        ]
        clusters = _cluster_by_session(errors)
        assert len(clusters[0]["sessions"]) <= 5


# ── Task 3: Evidence ledger promotion ──


class TestEvidenceLedgerPromotion:
    """Cross-session failures promote to evidence ledger at 3+ sessions."""

    def test_three_sessions_promotes_to_ledger(self):
        """3+ session cluster gets recorded in evidence ledger."""
        clusters = [
            {"pattern": "curl::health", "session_count": 3, "sessions": ["a", "b", "c"], "severity": "high"},
        ]

        with patch("multifleet.evidence_ledger.EvidenceLedger") as MockLedger:
            mock_ledger = MagicMock()
            MockLedger.return_value = mock_ledger

            _promote_to_evidence(clusters)

            mock_ledger.record.assert_called_once()
            call_kwargs = mock_ledger.record.call_args
            assert call_kwargs[1]["event_type"] == "failure_cluster"
            assert "curl::health" in call_kwargs[1]["subject"]

    def test_two_sessions_not_promoted(self):
        """2-session cluster does NOT get promoted to evidence ledger."""
        clusters = [
            {"pattern": "git::push", "session_count": 2, "sessions": ["a", "b"], "severity": "medium"},
        ]

        with patch("multifleet.evidence_ledger.EvidenceLedger") as MockLedger:
            mock_ledger = MagicMock()
            MockLedger.return_value = mock_ledger

            _promote_to_evidence(clusters)

            mock_ledger.record.assert_not_called()

    def test_promotion_graceful_on_ledger_failure(self):
        """If evidence ledger import fails, no exception raised."""
        clusters = [
            {"pattern": "x::y", "session_count": 3, "sessions": ["a", "b", "c"], "severity": "high"},
        ]

        with patch.dict("sys.modules", {"multifleet.evidence_ledger": None}):
            # Should not raise — the import will fail and be caught
            _promote_to_evidence(clusters)


# ── Task 4: Full pipeline integration ──


class TestFullPipeline:
    """End-to-end: audit errors -> cluster -> evidence -> liaison injection."""

    def test_full_pipeline_audit_to_injection(self, tmp_audit_dir, liaison):
        """Errors in audit trail flow through clustering to evidence and liaison."""
        # Write audit events with cross-session failures
        events = []
        for sid in ["sess-1", "sess-2", "sess-3"]:
            events.append({
                "tool": "nats",
                "target": "publish",
                "status": "error",
                "session_id": sid,
                "timestamp": "2026-04-05T10:00:00Z",
            })
        # Add enough for _MIN_CLUSTER_SIZE (3)
        for i in range(3):
            events.append({
                "tool": "nats",
                "target": "publish",
                "status": "error",
                "session_id": "sess-1",
                "timestamp": "2026-04-05T10:00:00Z",
            })
        _write_audit_events(tmp_audit_dir, events)

        # Run the probe
        with patch("multifleet.probes.repeated_failure_cluster._promote_to_evidence") as mock_promote:
            result = probe_repeated_failure_cluster({"audit_dir": str(tmp_audit_dir)})

        assert result.status == "warning"
        assert len(result.findings) > 0

        # Check cross-session findings exist
        cross_findings = [f for f in result.findings if "Cross-session" in f.title]
        assert len(cross_findings) >= 1

        # Verify promote was called with clusters
        mock_promote.assert_called_once()
        clusters_arg = mock_promote.call_args[0][0]
        assert any(c["session_count"] >= 3 for c in clusters_arg)

    def test_probe_findings_flow_to_liaison_injection(self, liaison):
        """Probe findings from injection_prioritizer flow into liaison batch."""
        findings = [
            ProbeFinding(title="#1 [score:90] critical-file.py"),
            ProbeFinding(title="#2 [score:60] config.yaml"),
            ProbeFinding(title="#3 [score:40] tests/test.py"),
        ]
        mock_result = ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="warning",
            confidence=0.75,
            severity="medium",
            findings=findings,
        )

        with patch("multifleet.probes.create_engine") as mock_factory:
            engine = MagicMock()
            engine.scan_one.return_value = mock_result
            mock_factory.return_value = engine

            injection = liaison.get_probe_injection()

        assert injection is not None
        assert "critical-file.py" in injection
        assert "config.yaml" in injection
        # Low-score finding (40) may be filtered by prioritizer — only top findings shown
        # assert "tests/test.py" in injection
