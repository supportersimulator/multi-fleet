"""Integration tests for probe -> evidence -> analyzer -> synthesis pipeline.

Verifies the full wiring: probes feed the evidence ledger, the contextdna
analyzer reads probe evidence, and synthesis runs probes before analyzing.
"""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import (
    ProbeCategory,
    ProbeEngine,
    ProbeFinding,
    ProbeRegistry,
    ProbeResult,
)
from multifleet.evidence_ledger import EvidenceLedger


# ── Test helpers ──

def _high_probe(context: dict) -> ProbeResult:
    """Probe returning HIGH severity with findings."""
    return ProbeResult(
        probe_id="test_high",
        category="security",
        status="warning",
        confidence=0.85,
        severity="high",
        findings=[
            ProbeFinding(
                title="Hardcoded secret in config.py",
                evidence=["line 42: API_KEY = 'sk-...'"],
                blast_radius="high",
                file_paths=["config.py"],
            )
        ],
    )


def _critical_probe(context: dict) -> ProbeResult:
    """Probe returning CRITICAL severity with findings."""
    return ProbeResult(
        probe_id="test_critical",
        category="security",
        status="critical",
        confidence=0.95,
        severity="critical",
        findings=[
            ProbeFinding(
                title="Exposed credentials in database.py",
                evidence=["plaintext password"],
                blast_radius="high",
                file_paths=["database.py"],
            )
        ],
    )


def _low_probe(context: dict) -> ProbeResult:
    """Probe returning LOW severity."""
    return ProbeResult(
        probe_id="test_low",
        category="structure",
        status="ok",
        confidence=0.9,
        severity="low",
        findings=[
            ProbeFinding(title="Minor style issue", blast_radius="low")
        ],
    )


def _info_probe(context: dict) -> ProbeResult:
    """Probe returning INFO severity (no findings)."""
    return ProbeResult(
        probe_id="test_info",
        category="structure",
        status="ok",
        confidence=1.0,
        severity="info",
    )


def _medium_probe(context: dict) -> ProbeResult:
    """Probe returning MEDIUM severity with findings."""
    return ProbeResult(
        probe_id="test_medium",
        category="context_quality",
        status="warning",
        confidence=0.75,
        severity="medium",
        findings=[
            ProbeFinding(title="Stale docs", blast_radius="low")
        ],
    )


@pytest.fixture
def ledger(tmp_path):
    """EvidenceLedger backed by temp SQLite."""
    return EvidenceLedger(db_path=str(tmp_path / "evidence.db"))


@pytest.fixture
def engine_with_probes():
    """ProbeEngine with HIGH, LOW, and CRITICAL probes registered."""
    registry = ProbeRegistry()
    registry.register("high_probe", _high_probe, ProbeCategory.SECURITY, triggers=["all", "manual"])
    registry.register("low_probe", _low_probe, ProbeCategory.STRUCTURE, triggers=["all", "manual"])
    registry.register("critical_probe", _critical_probe, ProbeCategory.SECURITY, triggers=["all", "manual"])
    registry.register("info_probe", _info_probe, ProbeCategory.STRUCTURE, triggers=["all", "manual"])
    registry.register("medium_probe", _medium_probe, ProbeCategory.CONTEXT_QUALITY, triggers=["all", "manual"])
    return ProbeEngine(registry=registry)


# ── 1. Probe HIGH finding -> evidence ledger entry ──

class TestProbeToEvidenceLedger:
    def test_high_finding_recorded_in_ledger(self, tmp_path, engine_with_probes):
        """HIGH severity probe findings get recorded to evidence ledger."""
        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            results = engine_with_probes.scan(trigger="all")

        # Should have called record for HIGH and CRITICAL findings (2 calls),
        # plus potentially a batch promotion call from ProductBridge fallback
        assert mock_ledger.record.call_count >= 2

        # Verify the individual finding calls have correct event_type
        finding_calls = [c for c in mock_ledger.record.call_args_list
                         if c[1].get("event_type") == "probe_finding"]
        assert len(finding_calls) == 2
        for call in finding_calls:
            assert call[1]["payload"]["severity"] in ("high", "critical")

    def test_high_finding_payload_structure(self, tmp_path, engine_with_probes):
        """Evidence ledger entries have correct payload structure."""
        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            engine_with_probes.scan(trigger="all")

        calls = mock_ledger.record.call_args_list
        high_call = [c for c in calls if c[1].get("event_type") == "probe_finding" and c[1]["payload"].get("severity") == "high"][0]
        payload = high_call[1]["payload"]
        assert "probe_id" in payload
        assert "findings_count" in payload
        assert "confidence" in payload
        assert "category" in payload
        assert "finding_titles" in payload
        assert payload["findings_count"] >= 1

    def test_low_finding_not_recorded(self, tmp_path):
        """LOW severity probe findings should NOT be recorded to evidence ledger."""
        registry = ProbeRegistry()
        registry.register("low_only", _low_probe, ProbeCategory.STRUCTURE, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            engine.scan(trigger="all")

        mock_ledger.record.assert_not_called()

    def test_info_finding_not_recorded(self, tmp_path):
        """INFO severity probe findings should NOT be recorded to evidence ledger."""
        registry = ProbeRegistry()
        registry.register("info_only", _info_probe, ProbeCategory.STRUCTURE, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            engine.scan(trigger="all")
            mock_ledger.record.assert_not_called()

    def test_medium_finding_not_recorded(self, tmp_path):
        """MEDIUM severity probe findings should NOT be recorded to evidence ledger."""
        registry = ProbeRegistry()
        registry.register("medium_only", _medium_probe, ProbeCategory.CONTEXT_QUALITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            engine.scan(trigger="all")
            mock_ledger.record.assert_not_called()

    def test_critical_finding_recorded(self, tmp_path):
        """CRITICAL severity probe findings get recorded to evidence ledger."""
        registry = ProbeRegistry()
        registry.register("critical_only", _critical_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            engine.scan(trigger="all")

        # Individual finding call + possible batch promotion from ProductBridge fallback
        finding_calls = [c for c in mock_ledger.record.call_args_list
                         if c[1].get("event_type") == "probe_finding"]
        assert len(finding_calls) == 1
        payload = finding_calls[0][1]["payload"]
        assert payload["severity"] == "critical"

    def test_node_id_from_env(self, tmp_path):
        """Evidence ledger records use MULTIFLEET_NODE_ID from environment."""
        registry = ProbeRegistry()
        registry.register("high", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac2"}):
                engine.scan(trigger="all")

        node_id = mock_ledger.record.call_args[1]["node_id"]
        assert node_id == "mac2"


# ── 2. Evidence ledger feeds contextdna_analyzer ──

class TestEvidenceFeedsAnalyzer:
    def test_evidence_ledger_stores_and_returns_probe_findings(self, tmp_path):
        """Evidence ledger correctly stores and queries probe_finding events."""
        db_path = str(tmp_path / "evidence.db")
        ledger = EvidenceLedger(db_path=db_path)

        ledger.record(
            event_type="probe_finding",
            node_id="mac1",
            subject="probe:security_scan severity:high",
            payload={
                "probe_id": "security_scan",
                "severity": "high",
                "findings_count": 1,
                "confidence": 0.9,
                "category": "security",
                "finding_titles": ["Hardcoded secret in config.py"],
            },
        )

        recent = ledger.query(event_type="probe_finding", limit=20)
        assert len(recent) == 1
        assert recent[0]["event_type"] == "probe_finding"
        assert recent[0]["payload"]["severity"] == "high"
        assert recent[0]["payload"]["finding_titles"] == ["Hardcoded secret in config.py"]

    def test_probe_evidence_queryable_by_type(self, tmp_path):
        """Probe evidence is filterable by event_type from mixed entries."""
        db_path = str(tmp_path / "evidence.db")
        ledger = EvidenceLedger(db_path=db_path)

        # Mix of event types
        ledger.record("merge_decision", "mac1", "merge", {"branch": "main"})
        ledger.record("probe_finding", "mac1", "probe:sec", {
            "probe_id": "sec", "severity": "critical", "findings_count": 1,
            "confidence": 0.95, "finding_titles": ["cred leak in database.py"],
        })
        ledger.record("lease_granted", "mac2", "lease", {"resource": "x"})

        probe_only = ledger.query(event_type="probe_finding", limit=20)
        assert len(probe_only) == 1
        assert probe_only[0]["payload"]["severity"] == "critical"


# ── 3. Synthesis runs probes before analyzing ──

class TestSynthesisRunsProbes:
    def test_synthesis_runs_probe_scan(self, tmp_path):
        """ChiefSynthesis.synthesize() runs probe scan before analyzing."""
        from multifleet.synthesis import ChiefSynthesis

        mock_engine_instance = MagicMock()
        mock_engine_instance.scan.return_value = []
        mock_engine_instance.findings_summary.return_value = {
            "total_probes": 0, "total_findings": 0,
        }

        with patch("multifleet.probe_engine.ProbeEngine", return_value=mock_engine_instance):
            with patch("multifleet.evidence_ledger.EvidenceLedger", side_effect=ImportError):
                with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer", return_value=None):
                    synth = ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))
                    verdicts = {
                        "mac1": {"confidence": 0.8, "branch": "feat-a", "summary": "test"},
                    }
                    result = synth.synthesize("task-1", verdicts)

                    mock_engine_instance.scan.assert_called_once_with(
                        trigger="synthesis", context={"task_id": "task-1"}
                    )

    def test_synthesis_includes_probe_summary_in_metadata(self, tmp_path):
        """Synthesis result includes probe_summary in metadata when probes run."""
        from multifleet.synthesis import ChiefSynthesis

        probe_summary = {
            "total_probes": 3,
            "total_findings": 1,
            "by_severity": {"high": 1},
            "surgeon_intensity": "standard",
        }

        mock_engine_instance = MagicMock()
        mock_engine_instance.scan.return_value = [MagicMock()]  # non-empty
        mock_engine_instance.findings_summary.return_value = probe_summary

        with patch("multifleet.probe_engine.ProbeEngine", return_value=mock_engine_instance):
            with patch("multifleet.evidence_ledger.EvidenceLedger", side_effect=ImportError):
                with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer", return_value=None):
                    synth = ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))
                    verdicts = {
                        "mac1": {"confidence": 0.8, "branch": "feat-a"},
                    }
                    result = synth.synthesize("task-2", verdicts)

                    assert "probe_summary" in result.metadata
                    assert result.metadata["probe_summary"]["total_probes"] == 3

    def test_synthesis_graceful_when_probes_fail(self, tmp_path):
        """Synthesis completes even if probe scan fails entirely."""
        from multifleet.synthesis import ChiefSynthesis

        with patch("multifleet.probe_engine.ProbeEngine", side_effect=ImportError("no probes")):
            with patch("multifleet.evidence_ledger.EvidenceLedger", side_effect=ImportError):
                with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer", return_value=None):
                    synth = ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))
                    verdicts = {
                        "mac1": {"confidence": 0.9, "branch": "feat-b"},
                    }
                    result = synth.synthesize("task-3", verdicts)

                    assert result.task_id == "task-3"
                    assert result.decision == "merge"
                    assert result.confidence > 0


# ── 4. Full pipeline: probe -> evidence -> synthesis ──

class TestFullPipeline:
    def test_probe_to_evidence_to_query(self, tmp_path):
        """Full pipeline: probe scan records to ledger, ledger is queryable."""
        db_path = str(tmp_path / "evidence.db")
        real_ledger = EvidenceLedger(db_path=db_path)

        registry = ProbeRegistry()
        registry.register("high_sec", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=real_ledger):
            results = engine.scan(trigger="all")

        assert len(results) == 1
        assert results[0].severity == "high"

        entries = real_ledger.query(event_type="probe_finding")
        assert len(entries) == 1
        assert entries[0]["payload"]["probe_id"] == "test_high"
        assert entries[0]["payload"]["severity"] == "high"

        chain = real_ledger.verify_chain()
        assert chain["valid"] is True

    def test_multiple_probes_multiple_entries(self, tmp_path):
        """Multiple HIGH+ probes create multiple evidence entries."""
        db_path = str(tmp_path / "evidence.db")
        real_ledger = EvidenceLedger(db_path=db_path)

        registry = ProbeRegistry()
        registry.register("h1", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        registry.register("c1", _critical_probe, ProbeCategory.SECURITY, triggers=["all"])
        registry.register("l1", _low_probe, ProbeCategory.STRUCTURE, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=real_ledger):
            engine.scan(trigger="all")

        entries = real_ledger.query(event_type="probe_finding")
        assert len(entries) == 2
        severities = {e["payload"]["severity"] for e in entries}
        assert severities == {"high", "critical"}

    def test_evidence_chain_valid_after_pipeline(self, tmp_path):
        """Hash chain remains valid after probe -> evidence pipeline."""
        db_path = str(tmp_path / "evidence.db")
        real_ledger = EvidenceLedger(db_path=db_path)

        real_ledger.record("merge_decision", "mac1", "prior merge", {"branch": "main"})

        registry = ProbeRegistry()
        registry.register("h1", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=real_ledger):
            engine.scan(trigger="all")

        chain = real_ledger.verify_chain()
        assert chain["valid"] is True
        # 1 prior merge + 1 probe finding + possible batch promotion from ProductBridge
        assert chain["checked"] >= 2


# ── 5. Graceful degradation ──

class TestGracefulDegradation:
    def test_probe_scan_works_without_evidence_ledger(self):
        """Probe scan completes even if evidence ledger import fails."""
        registry = ProbeRegistry()
        registry.register("h1", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.evidence_ledger.EvidenceLedger", side_effect=ImportError):
            results = engine.scan(trigger="all")

        assert len(results) == 1
        assert results[0].severity == "high"

    def test_evidence_ledger_write_failure_doesnt_break_scan(self):
        """If evidence ledger write fails, scan still returns results."""
        registry = ProbeRegistry()
        registry.register("h1", _high_probe, ProbeCategory.SECURITY, triggers=["all"])
        engine = ProbeEngine(registry=registry)

        mock_ledger = MagicMock()
        mock_ledger.record.side_effect = RuntimeError("db locked")
        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            results = engine.scan(trigger="all")

        assert len(results) == 1
        assert results[0].severity == "high"

    def test_synthesis_evidence_record_uses_correct_api(self, tmp_path):
        """Synthesis._record_evidence calls ledger.record() not append()."""
        from multifleet.synthesis import ChiefSynthesis, SynthesisResult

        mock_ledger = MagicMock()

        with patch("multifleet.evidence_ledger.EvidenceLedger", return_value=mock_ledger):
            with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer", return_value=None):
                synth = ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))

        result = SynthesisResult(
            task_id="task-test",
            decision="merge",
            winner_branch="feat-x",
            confidence=0.9,
            reasoning="test",
        )
        synth._record_evidence(result)

        mock_ledger.record.assert_called_once()
        call_kwargs = mock_ledger.record.call_args[1]
        assert call_kwargs["event_type"] == "merge_decision"
        assert "task_id" in call_kwargs["payload"]
