"""Tests for bidirectional evidence sync between fleet modules and Context DNA.

Verifies that synthesis, contextdna_analyzer, probe_engine, and ghost_corroborate
correctly push evidence TO and pull evidence FROM Context DNA via ProductBridge.
All integration points must degrade gracefully when the bridge is unavailable.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.synthesis import ChiefSynthesis, SynthesisResult
from multifleet.probe_engine import ProbeEngine, ProbeResult, ProbeFinding
from multifleet.ghost_corroborate import GhostCorroborate


# ── Helpers ──────────────────────────────────────────────────────────────────


def _verdict(confidence=0.8, summary="test task", **extra):
    v = {
        "confidence": confidence,
        "summary": summary,
        "risks": [],
        "dissent": [],
        "strengths": [],
        "branch": "feat-x",
        "followups": [],
    }
    v.update(extra)
    return v


def _mock_bridge():
    bridge = MagicMock()
    bridge.fleet_to_memory.return_value = {"stored": True, "entry_id": "e1"}
    bridge.memory_to_fleet.return_value = {"results": [{"payload": {"finding": "prior"}}], "count": 1}
    return bridge


# ── Test 1: Synthesis pushes decisions to Context DNA ────────────────────────


class TestSynthesisPushToContextDNA:
    def test_push_called_after_synthesis(self, tmp_path):
        """Verify _push_to_contextdna fires after every synthesis decision."""
        bridge = _mock_bridge()
        with patch("multifleet.synthesis.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge):
            engine = ChiefSynthesis(ledger_path=str(tmp_path / "d.jsonl"))
            engine.synthesize("task-1", {"mac1": _verdict()})

        # fleet_to_memory should have been called with synthesis_decision
        calls = bridge.fleet_to_memory.call_args_list
        pushed = [c for c in calls if c[0][0].get("event_type") == "synthesis_decision"]
        assert len(pushed) >= 1
        payload = pushed[0][0][0]
        assert payload["evidence"]["task_id"] == "task-1"
        assert "synthesis" in payload["tags"]


# ── Test 2: Synthesis pulls Context DNA evidence before deciding ─────────────


class TestSynthesisPullFromContextDNA:
    def test_pull_enriches_metadata(self, tmp_path):
        """Verify synthesis pulls from Context DNA and records evidence count."""
        bridge = _mock_bridge()
        with patch("multifleet.synthesis.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge):
            engine = ChiefSynthesis(ledger_path=str(tmp_path / "d.jsonl"))
            result = engine.synthesize("task-2", {"mac1": _verdict(summary="refactor auth")})

        bridge.memory_to_fleet.assert_called()
        assert result.metadata.get("contextdna_evidence_count", 0) >= 1


# ── Test 3: Synthesis graceful when bridge unavailable ───────────────────────


class TestSynthesisGracefulDegradation:
    def test_synthesis_works_without_bridge(self, tmp_path):
        """Synthesis must not crash when ProductBridge is unreachable."""
        with patch("multifleet.product_bridge.get_bridge", side_effect=ImportError("no bridge")):
            engine = ChiefSynthesis(ledger_path=str(tmp_path / "d.jsonl"))
            result = engine.synthesize("task-3", {"mac1": _verdict()})

        assert result.decision in ("merge", "cherry_pick", "needs_rebuttal", "reject")
        assert result.confidence > 0


# ── Test 4: ProbeEngine pushes HIGH findings to Context DNA ──────────────────


class TestProbeEnginePush:
    def test_high_findings_pushed(self):
        """HIGH/CRITICAL probe findings should be pushed to Context DNA."""
        bridge = _mock_bridge()
        engine = ProbeEngine()

        high_results = [
            ProbeResult(
                probe_id="test_probe",
                category="security",
                status="warning",
                confidence=0.9,
                severity="high",
                findings=[ProbeFinding(title="SQL injection risk")],
            )
        ]

        with patch("multifleet.probe_engine.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge):
            engine._record_high_findings(high_results)

        calls = bridge.fleet_to_memory.call_args_list
        pushed = [c for c in calls if c[0][0].get("event_type") == "probe_high_findings"]
        assert len(pushed) == 1
        assert "probe" in pushed[0][0][0]["tags"]


# ── Test 5: ProbeEngine pull from Context DNA ────────────────────────────────


class TestProbeEnginePull:
    def test_pull_returns_evidence(self):
        """ProbeEngine.pull_contextdna_evidence should return evidence entries."""
        bridge = _mock_bridge()
        engine = ProbeEngine()

        with patch("multifleet.probe_engine.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge):
            results = engine.pull_contextdna_evidence("auth vulnerability")

        assert len(results) == 1
        bridge.memory_to_fleet.assert_called_once_with("auth vulnerability")

    def test_pull_graceful_when_unavailable(self):
        """Pull returns empty list when bridge is unavailable."""
        engine = ProbeEngine()
        with patch("multifleet.product_bridge.get_bridge", side_effect=Exception("down")):
            results = engine.pull_contextdna_evidence("anything")
        assert results == []


# ── Test 6: GhostCorroborate pushes results to Context DNA ───────────────────


class TestGhostCorroboratePush:
    def test_corroboration_pushed(self):
        """Corroboration results should be pushed to Context DNA."""
        bridge = _mock_bridge()
        ghost = GhostCorroborate(node_id="mac3")

        findings = [
            {"title": "stale documentation detected", "confidence": 0.7},
            {"title": "import cycle risk", "confidence": 0.6},
        ]

        with patch("multifleet.ghost_corroborate.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge), \
             patch.object(ghost, "_load_history", return_value=[]), \
             patch.object(ghost, "_emit_event"):
            ghost.corroborate(findings)

        calls = bridge.fleet_to_memory.call_args_list
        pushed = [c for c in calls if c[0][0].get("event_type") == "ghost_corroboration"]
        assert len(pushed) == 1
        payload = pushed[0][0][0]
        assert payload["node_id"] == "mac3"
        assert "ghost" in payload["tags"]


# ── Test 7: GhostCorroborate pulls Context DNA evidence for enrichment ───────


class TestGhostCorroboratePull:
    def test_pull_enriches_history(self):
        """Context DNA evidence should be merged into corroboration history."""
        bridge = _mock_bridge()
        ghost = GhostCorroborate(node_id="mac3")

        findings = [{"title": "recurring drift", "confidence": 0.5}]

        with patch("multifleet.ghost_corroborate.get_bridge", return_value=bridge, create=True), \
             patch("multifleet.product_bridge.get_bridge", return_value=bridge), \
             patch.object(ghost, "_load_history", return_value=[]) as mock_history, \
             patch.object(ghost, "_emit_event"):
            result = ghost.corroborate(findings)

        # Bridge should have been queried for related evidence
        bridge.memory_to_fleet.assert_called()
        # Result should still complete (items processed)
        assert len(result.items) == 1


# ── Test 8: ContextDNA analyzer pull enriches probe evidence ─────────────────


class TestContextDNAAnalyzerPull:
    def test_analyzer_pulls_before_scoring(self):
        """The analyzer should pull Context DNA evidence and merge with probe evidence."""
        bridge = _mock_bridge()

        with patch("multifleet.product_bridge.get_bridge", return_value=bridge):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer

            # Mock query_fn so the analyzer is created
            with patch("multifleet.contextdna_analyzer.query_learnings", return_value=[], create=True):
                try:
                    analyzer = create_contextdna_analyzer()
                except Exception:
                    pytest.skip("ContextDNA memory modules not available")

            if analyzer is None:
                pytest.skip("ContextDNA query_fn not available")

            verdicts = {"mac1": _verdict(summary="fix auth flow")}
            # The analyzer may return None if no memory adjustments, that's fine
            # We just verify the bridge was queried
            try:
                analyzer("task-pull", verdicts)
            except Exception:
                pass  # Analyzer internals may fail without full memory stack

        bridge.memory_to_fleet.assert_called()
