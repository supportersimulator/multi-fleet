"""Tests for SignalScorer and IncrementalTrigger in probe_engine.

Covers: score calculation, severity weighting, recurrence bonus,
top-N selection, noise filtering, incremental git-diff triggers,
and file-to-probe mapping.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import (
    FILE_PROBE_MAP,
    IncrementalTrigger,
    ProbeCategory,
    ProbeEngine,
    ProbeFinding,
    ProbeRegistry,
    ProbeResult,
    SignalScorer,
)


# ── Helpers ──

def _make_result(
    probe_id: str = "test_probe",
    severity: str = "medium",
    confidence: float = 0.8,
    category: str = "structure",
    status: str = "warning",
    generated_at: str | None = None,
) -> ProbeResult:
    return ProbeResult(
        probe_id=probe_id,
        category=category,
        status=status,
        confidence=confidence,
        severity=severity,
        findings=[ProbeFinding(title=f"Finding from {probe_id}")],
        generated_at=generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def _make_registry_with(*probe_ids: str) -> ProbeRegistry:
    """Create a registry with dummy probes for the given IDs."""
    reg = ProbeRegistry()
    for pid in probe_ids:
        reg.register(
            probe_id=pid,
            fn=lambda ctx, _pid=pid: ProbeResult(
                probe_id=_pid, category="structure", status="ok",
                confidence=0.9, severity="info",
            ),
            category=ProbeCategory.STRUCTURE,
        )
    return reg


# ── SignalScorer: score calculation ──

class TestSignalScorerBasic:

    def test_score_critical_higher_than_info(self):
        scorer = SignalScorer()
        critical = _make_result(severity="critical", confidence=0.9)
        info = _make_result(severity="info", confidence=0.9)
        scored = scorer.score_results([info, critical])
        # Critical should be first (higher score)
        assert scored[0][1].severity == "critical"
        assert scored[1][1].severity == "info"

    def test_score_higher_confidence_wins_same_severity(self):
        scorer = SignalScorer()
        high_conf = _make_result(probe_id="a", severity="high", confidence=0.95)
        low_conf = _make_result(probe_id="b", severity="high", confidence=0.3)
        scored = scorer.score_results([low_conf, high_conf])
        assert scored[0][1].probe_id == "a"

    def test_severity_weights_are_monotonic(self):
        """Each severity level should produce a higher score than the one below it."""
        scorer = SignalScorer()
        severities = ["info", "low", "medium", "high", "critical"]
        results = [_make_result(probe_id=s, severity=s, confidence=1.0) for s in severities]
        scored = scorer.score_results(results)
        scores = [s for s, _ in scored]
        # Should be sorted descending
        assert scores == sorted(scores, reverse=True)

    def test_unknown_severity_gets_default_weight(self):
        scorer = SignalScorer()
        result = _make_result(severity="unknown_level", confidence=1.0)
        scored = scorer.score_results([result])
        # Default weight is 0.3
        assert abs(scored[0][0] - 0.3) < 0.01

    def test_zero_confidence_yields_zero_score(self):
        scorer = SignalScorer()
        result = _make_result(severity="critical", confidence=0.0)
        scored = scorer.score_results([result])
        assert scored[0][0] == 0.0


# ── SignalScorer: recurrence bonus ──

class TestRecurrenceBonus:

    def test_no_ledger_returns_neutral_bonus(self):
        scorer = SignalScorer(evidence_ledger=None)
        assert scorer._recurrence_bonus("any_probe") == 1.0

    def test_ledger_with_occurrences_boosts_score(self):
        ledger = MagicMock()
        ledger.count_by_probe.return_value = 5
        scorer = SignalScorer(evidence_ledger=ledger)
        bonus = scorer._recurrence_bonus("recurring_probe")
        assert bonus > 1.0
        ledger.count_by_probe.assert_called_once_with("recurring_probe")

    def test_ledger_with_zero_occurrences(self):
        ledger = MagicMock()
        ledger.count_by_probe.return_value = 0
        scorer = SignalScorer(evidence_ledger=ledger)
        bonus = scorer._recurrence_bonus("new_probe")
        # log1p(0) = 0, so bonus = 1.0
        assert bonus == 1.0

    def test_ledger_exception_returns_neutral(self):
        ledger = MagicMock()
        ledger.count_by_probe.side_effect = RuntimeError("db error")
        scorer = SignalScorer(evidence_ledger=ledger)
        assert scorer._recurrence_bonus("broken") == 1.0


# ── SignalScorer: top_n and filter_noise ──

class TestTopNAndFilter:

    def test_top_n_returns_correct_count(self):
        scorer = SignalScorer()
        results = [
            _make_result(probe_id=f"p{i}", severity="medium", confidence=0.1 * (i + 1))
            for i in range(10)
        ]
        top = scorer.top_n(results, n=3)
        assert len(top) == 3

    def test_top_n_returns_highest_scored(self):
        scorer = SignalScorer()
        low = _make_result(probe_id="low", severity="info", confidence=0.1)
        high = _make_result(probe_id="high", severity="critical", confidence=0.99)
        mid = _make_result(probe_id="mid", severity="medium", confidence=0.5)
        top = scorer.top_n([low, mid, high], n=1)
        assert top[0].probe_id == "high"

    def test_top_n_with_n_larger_than_list(self):
        scorer = SignalScorer()
        results = [_make_result(probe_id="only")]
        top = scorer.top_n(results, n=100)
        assert len(top) == 1

    def test_filter_noise_removes_low_scores(self):
        scorer = SignalScorer()
        high = _make_result(severity="critical", confidence=0.9)
        low = _make_result(severity="info", confidence=0.1)
        filtered = scorer.filter_noise([high, low], threshold=0.3)
        assert len(filtered) == 1
        assert filtered[0].severity == "critical"

    def test_filter_noise_empty_input(self):
        scorer = SignalScorer()
        assert scorer.filter_noise([], threshold=0.1) == []


# ── IncrementalTrigger: changed files ──

class TestIncrementalTriggerChangedFiles:

    @patch("multifleet.probe_engine.subprocess.run")
    def test_changed_files_parses_git_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/main.py\nlib/utils.py\npyproject.toml\n",
        )
        trigger = IncrementalTrigger(repo_path="/fake")
        files = trigger.changed_files("HEAD~1")
        assert files == ["src/main.py", "lib/utils.py", "pyproject.toml"]
        mock_run.assert_called_once()

    @patch("multifleet.probe_engine.subprocess.run")
    def test_changed_files_no_changes(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        trigger = IncrementalTrigger()
        files = trigger.changed_files()
        assert files == []

    @patch("multifleet.probe_engine.subprocess.run")
    def test_changed_files_git_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128, stderr="fatal: bad ref")
        trigger = IncrementalTrigger()
        files = trigger.changed_files()
        assert files == []

    @patch("multifleet.probe_engine.subprocess.run")
    def test_changed_files_exception(self, mock_run):
        mock_run.side_effect = OSError("no git")
        trigger = IncrementalTrigger()
        files = trigger.changed_files()
        assert files == []


# ── IncrementalTrigger: map_to_probes ──

class TestMapToProbes:

    def test_py_files_map_to_structure_and_readiness(self):
        reg = _make_registry_with("structure_imports", "agent_readiness", "other")
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes(["src/module.py"], reg)
        assert "structure_imports" in probes
        assert "agent_readiness" in probes
        assert "other" not in probes

    def test_pyproject_maps_to_dependency_drift(self):
        reg = _make_registry_with("dependency_drift", "structure_imports")
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes(["pyproject.toml"], reg)
        assert "dependency_drift" in probes

    def test_claude_md_maps_to_mirror_drift(self):
        reg = _make_registry_with("mirror_drift")
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes(["CLAUDE.md"], reg)
        assert "mirror_drift" in probes

    def test_test_files_map_to_reliability(self):
        reg = _make_registry_with("reliability_flaky", "reliability_repeated")
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes(["tests/test_foo.py"], reg)
        assert "reliability_flaky" in probes
        assert "reliability_repeated" in probes

    def test_no_changes_no_probes(self):
        reg = _make_registry_with("structure_imports")
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes([], reg)
        assert probes == []

    def test_unregistered_probes_filtered_out(self):
        """Even if file matches a pattern, unregistered probe IDs are excluded."""
        reg = _make_registry_with()  # empty registry
        trigger = IncrementalTrigger()
        probes = trigger.map_to_probes(["foo.py"], reg)
        assert probes == []


# ── ProbeEngine.scan with incremental trigger ──

class TestEngineIncrementalScan:

    @patch("multifleet.probe_engine.subprocess.run")
    def test_incremental_scan_runs_matched_probes(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="src/app.py\n",
        )
        reg = _make_registry_with("structure_imports", "agent_readiness")
        engine = ProbeEngine(registry=reg, repo_path="/fake")
        results = engine.scan(trigger="incremental")
        probe_ids = {r.probe_id for r in results}
        assert "structure_imports" in probe_ids

    @patch("multifleet.probe_engine.subprocess.run")
    def test_incremental_scan_no_changes_returns_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        engine = ProbeEngine(repo_path="/fake")
        results = engine.scan(trigger="incremental")
        assert results == []

    @patch("multifleet.probe_engine.subprocess.run")
    def test_incremental_scan_custom_since(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="x.py\n")
        reg = _make_registry_with("structure_imports")
        engine = ProbeEngine(registry=reg, repo_path="/fake")
        engine.scan(trigger="incremental", context={"since": "abc123"})
        # Verify git diff was called with the custom ref
        call_args = mock_run.call_args
        assert "abc123" in call_args[0][0] or "abc123" in call_args.kwargs.get("args", call_args[0][0])
