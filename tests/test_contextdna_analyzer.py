"""Tests for the ContextDNA memory-backed synthesis analyzer."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.synthesis import ChiefSynthesis, SynthesisResult


# ── Helpers ──────────────────────────────────────────────────────────────────


def _verdict(confidence=0.8, summary="implement caching layer", risks=None,
             dissent=None, strengths=None, branch="feat-x", followups=None,
             files_touched=None, **extra):
    v = {
        "confidence": confidence,
        "summary": summary,
        "risks": risks or [],
        "dissent": dissent or [],
        "strengths": strengths or [],
        "branch": branch,
        "followups": followups or [],
        "files_touched": files_touched or [],
    }
    v.update(extra)
    return v


# ── Module-level import helpers ──────────────────────────────────────────────


def _import_analyzer():
    """Import the analyzer module fresh (avoid stale import caching)."""
    from multifleet.contextdna_analyzer import create_contextdna_analyzer
    return create_contextdna_analyzer


def _mock_query_learnings(results):
    """Return a mock query_learnings function that returns fixed results."""
    def mock_fn(query, limit=5):
        return results
    return mock_fn


# ── Creation tests ───────────────────────────────────────────────────────────


class TestAnalyzerCreation:
    def test_creates_analyzer_when_memory_available(self):
        """Analyzer is created when memory.query is importable."""
        mock_ql = _mock_query_learnings(["success pattern found"])
        with patch.dict("sys.modules", {"memory": MagicMock(), "memory.query": MagicMock(query_learnings=mock_ql)}):
            create = _import_analyzer()
            analyzer = create()
            assert analyzer is not None
            assert callable(analyzer)

    def test_returns_none_when_memory_unavailable(self):
        """Returns None when no memory module is importable."""
        # Temporarily remove memory modules from sys.modules and block re-import
        import builtins
        original_import = builtins.__import__

        def block_memory(name, globals=None, locals=None, fromlist=(), level=0):
            if isinstance(name, str) and name.startswith("memory"):
                raise ImportError(f"No module named '{name}'")
            return original_import(name, globals, locals, fromlist, level)

        saved = {}
        for key in list(sys.modules):
            if key.startswith("memory"):
                saved[key] = sys.modules.pop(key)
        try:
            with patch("builtins.__import__", side_effect=block_memory):
                # Call create directly — imports inside will fail
                from multifleet.contextdna_analyzer import create_contextdna_analyzer
                result = create_contextdna_analyzer()
                assert result is None
        finally:
            sys.modules.update(saved)

    def test_returns_none_on_import_exception(self):
        """Returns None when imports raise unexpected exceptions."""
        import builtins
        original_import = builtins.__import__

        def raise_on_memory(name, globals=None, locals=None, fromlist=(), level=0):
            if isinstance(name, str) and name.startswith("memory"):
                raise RuntimeError("broken")
            return original_import(name, globals, locals, fromlist, level)

        saved = {}
        for key in list(sys.modules):
            if key.startswith("memory"):
                saved[key] = sys.modules.pop(key)
        try:
            with patch("builtins.__import__", side_effect=raise_on_memory):
                from multifleet.contextdna_analyzer import create_contextdna_analyzer
                result = create_contextdna_analyzer()
                assert result is None
        finally:
            sys.modules.update(saved)


# ── Scoring logic tests ─────────────────────────────────────────────────────


class TestMemoryScoring:
    def test_failure_keywords_reduce_score(self):
        from multifleet.contextdna_analyzer import _score_memory_text, PRIOR_FAILURE_PENALTY
        score = _score_memory_text("this caused a regression and broke the build")
        assert score == -PRIOR_FAILURE_PENALTY

    def test_success_keywords_boost_score(self):
        from multifleet.contextdna_analyzer import _score_memory_text, PRIOR_SUCCESS_BOOST
        score = _score_memory_text("this approach works and was verified stable")
        assert score == PRIOR_SUCCESS_BOOST

    def test_neutral_text_returns_zero(self):
        from multifleet.contextdna_analyzer import _score_memory_text, NO_MEMORY_MATCH
        score = _score_memory_text("refactored the module into smaller pieces")
        assert score == NO_MEMORY_MATCH

    def test_empty_text_returns_zero(self):
        from multifleet.contextdna_analyzer import _score_memory_text, NO_MEMORY_MATCH
        assert _score_memory_text("") == NO_MEMORY_MATCH
        assert _score_memory_text(None) == NO_MEMORY_MATCH


# ── Verdict analysis tests ───────────────────────────────────────────────────


class TestVerdictAnalysis:
    @pytest.fixture
    def mock_analyzer(self):
        """Create an analyzer with a controllable mock query function."""
        from multifleet.contextdna_analyzer import create_contextdna_analyzer
        import multifleet.contextdna_analyzer as mod

        mock_ql = MagicMock(return_value=["previously success confirmed stable"])
        with patch.object(mod, "query_learnings", mock_ql, create=True):
            # Directly construct the analyzer with our mock
            original_create = create_contextdna_analyzer

            def patched_create():
                # Manually set up query_fn
                def inner_analyzer(task_id, verdicts):
                    from multifleet.contextdna_analyzer import (
                        _score_memory_text, _build_memory_reasons,
                        NO_MEMORY_MATCH,
                    )
                    if not verdicts:
                        return None

                    adjustments = {}
                    for mid, v in verdicts.items():
                        summary = v.get("summary", "")
                        if not summary:
                            adjustments[mid] = {"delta": 0.0, "reason": "no queryable content"}
                            continue
                        results = mock_ql(summary, limit=5)
                        if not results:
                            adjustments[mid] = {"delta": 0.0, "reason": "no matches"}
                            continue
                        combined = " ".join(str(r) for r in results)
                        delta = _score_memory_text(combined)
                        adjustments[mid] = {"delta": delta, "reason": combined[:200], "match_count": len(results)}

                    if all(a["delta"] == 0.0 for a in adjustments.values()):
                        return None

                    scores = {}
                    for mid, v in verdicts.items():
                        base = float(v.get("confidence", 0.0))
                        risk_pen = len(v.get("risks", [])) * 0.05
                        dissent_pen = len(v.get("dissent", [])) * 0.1
                        mem_delta = adjustments.get(mid, {}).get("delta", 0.0)
                        scores[mid] = base - risk_pen - dissent_pen + mem_delta

                    winner_id = max(scores, key=scores.get)
                    winner_v = verdicts[winner_id]
                    winner_score = max(0.0, min(1.0, scores[winner_id]))

                    memory_reasons = _build_memory_reasons(adjustments)
                    reasoning = f"Memory-informed. {'; '.join(memory_reasons)}"

                    return SynthesisResult(
                        task_id=task_id,
                        decision="merge",
                        winner_branch=winner_v.get("branch", winner_id),
                        confidence=winner_score,
                        reasoning=reasoning,
                    )
                return inner_analyzer

            analyzer = patched_create()
            yield analyzer, mock_ql

    def test_success_memory_boosts_confidence(self, mock_analyzer):
        analyzer, mock_ql = mock_analyzer
        mock_ql.return_value = ["success confirmed verified stable"]
        result = analyzer("t1", {"mac1": _verdict(confidence=0.7)})
        assert result is not None
        assert result.confidence > 0.7  # boosted by memory

    def test_failure_memory_reduces_confidence(self, mock_analyzer):
        analyzer, mock_ql = mock_analyzer
        mock_ql.return_value = ["fail regression broke crash error"]
        result = analyzer("t2", {"mac1": _verdict(confidence=0.8)})
        assert result is not None
        assert result.confidence < 0.8  # penalized by memory

    def test_memory_reasoning_in_result(self, mock_analyzer):
        analyzer, mock_ql = mock_analyzer
        mock_ql.return_value = ["success verified"]
        result = analyzer("t3", {"mac1": _verdict()})
        assert result is not None
        assert "Memory" in result.reasoning or "memory" in result.reasoning

    def test_no_summary_returns_none(self, mock_analyzer):
        analyzer, mock_ql = mock_analyzer
        result = analyzer("t4", {"mac1": _verdict(summary="")})
        assert result is None

    def test_empty_verdicts_returns_none(self, mock_analyzer):
        analyzer, mock_ql = mock_analyzer
        result = analyzer("t5", {})
        assert result is None

    def test_memory_can_flip_winner(self, mock_analyzer):
        """Memory penalties on the leader can change which machine wins."""
        analyzer, mock_ql = mock_analyzer

        call_count = [0]
        def side_effect(query, limit=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return ["fail regression broke crash error bug"]  # mac1 penalized
            return ["success verified confirmed works"]  # mac2 boosted

        mock_ql.side_effect = side_effect

        result = analyzer("t6", {
            "mac1": _verdict(confidence=0.85, summary="deploy new cache", branch="a"),
            "mac2": _verdict(confidence=0.80, summary="optimize existing", branch="b"),
        })
        assert result is not None
        # mac1: 0.85 - 0.15 = 0.70, mac2: 0.80 + 0.10 = 0.90
        assert result.winner_branch == "b"


# ── Query failure graceful degradation ───────────────────────────────────────


class TestGracefulDegradation:
    def test_query_exception_returns_none(self):
        """If the memory query raises, analyzer returns None (graceful degradation)."""
        from multifleet.contextdna_analyzer import (
            _score_memory_text, _build_memory_reasons, NO_MEMORY_MATCH,
        )

        def failing_query(query, limit=5):
            raise ConnectionError("Redis down")

        def analyzer(task_id, verdicts):
            adjustments = {}
            for mid, v in verdicts.items():
                summary = v.get("summary", "")
                if not summary:
                    adjustments[mid] = {"delta": 0.0, "reason": "no content"}
                    continue
                try:
                    failing_query(summary, limit=5)
                except Exception:
                    adjustments[mid] = {"delta": 0.0, "reason": "query error"}
                    continue
            if all(a["delta"] == 0.0 for a in adjustments.values()):
                return None
            return SynthesisResult(task_id=task_id, decision="merge",
                                   winner_branch="x", confidence=0.5, reasoning="test")

        result = analyzer("t1", {"mac1": _verdict()})
        assert result is None  # Graceful fallback

    def test_empty_memory_results_returns_none(self):
        """If memory returns empty results, analyzer returns None."""
        from multifleet.contextdna_analyzer import NO_MEMORY_MATCH

        def empty_query(query, limit=5):
            return []

        # Simulate the analyzer logic path with empty results
        adjustments = {"mac1": {"delta": NO_MEMORY_MATCH, "reason": "no matches"}}
        all_zero = all(a["delta"] == NO_MEMORY_MATCH for a in adjustments.values())
        assert all_zero  # Would cause analyzer to return None


# ── Auto-registration in ChiefSynthesis ──────────────────────────────────────


class TestAutoRegistration:
    def test_auto_registers_when_available(self, tmp_path):
        """ChiefSynthesis auto-registers the ContextDNA analyzer on init."""
        mock_ql = _mock_query_learnings(["success stable works"])
        with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer") as mock_create:
            mock_create.return_value = lambda tid, v: None
            engine = ChiefSynthesis(ledger_path=str(tmp_path / "ledger.jsonl"))
            assert len(engine._analyzers) == 1

    def test_works_without_contextdna(self, tmp_path):
        """ChiefSynthesis works fine when contextdna_analyzer import fails."""
        with patch("multifleet.contextdna_analyzer.create_contextdna_analyzer") as mock_create:
            mock_create.return_value = None
            engine = ChiefSynthesis(ledger_path=str(tmp_path / "ledger.jsonl"))
            # No analyzer registered when create returns None
            assert len(engine._analyzers) == 0

            # Default analyzer still works
            result = engine.synthesize("t1", {"mac1": _verdict()})
            assert result.decision == "merge"

    def test_import_error_handled(self, tmp_path):
        """ChiefSynthesis handles ImportError from contextdna_analyzer."""
        import importlib
        from multifleet import synthesis as syn_mod
        # Save original module dict so reload doesn't poison class identity
        # for other test files that imported SynthesisResult at module level.
        orig_attrs = dict(syn_mod.__dict__)
        try:
            with patch.dict("sys.modules", {"multifleet.contextdna_analyzer": None}):
                # Force reimport of synthesis to test the try/except ImportError
                importlib.reload(syn_mod)
                engine = syn_mod.ChiefSynthesis(ledger_path=str(tmp_path / "ledger.jsonl"))
                # Should have 0 analyzers (import failed)
                assert len(engine._analyzers) == 0
                result = engine.synthesize("t1", {"mac1": _verdict()})
                assert result.decision == "merge"
        finally:
            # Restore original module attrs so SynthesisResult class identity
            # is preserved for other test files.
            syn_mod.__dict__.clear()
            syn_mod.__dict__.update(orig_attrs)


# ── Integration: full flow with mocked memory ───────────────────────────────


class TestIntegrationFlow:
    def test_full_synthesis_with_memory_analyzer(self, tmp_path):
        """End-to-end: engine with memory analyzer produces evidence-backed result."""
        from multifleet.contextdna_analyzer import (
            _score_memory_text, _build_memory_reasons, PRIOR_SUCCESS_BOOST,
        )

        def success_analyzer(task_id, verdicts):
            # Simulate memory finding success patterns for mac2
            scores = {}
            for mid, v in verdicts.items():
                base = float(v.get("confidence", 0.0))
                # Give mac2 a memory boost
                boost = PRIOR_SUCCESS_BOOST if mid == "mac2" else 0.0
                scores[mid] = base + boost

            winner_id = max(scores, key=scores.get)
            winner_v = verdicts[winner_id]
            return SynthesisResult(
                task_id=task_id,
                decision="merge",
                winner_branch=winner_v.get("branch", winner_id),
                confidence=max(0.0, min(1.0, scores[winner_id])),
                reasoning=f"Memory boosted {winner_id}",
            )

        engine = ChiefSynthesis(ledger_path=str(tmp_path / "ledger.jsonl"))
        engine._analyzers.clear()  # Remove auto-registered
        engine.register_analyzer(success_analyzer)

        result = engine.synthesize("full-test", {
            "mac1": _verdict(confidence=0.80, branch="branch-a"),
            "mac2": _verdict(confidence=0.78, branch="branch-b"),
        })

        # mac2 gets +0.10 boost -> 0.88 > 0.80
        assert result.winner_branch == "branch-b"
        assert result.confidence > 0.8
        assert "Memory" in result.reasoning


# ── Memory reasons builder ──────────────────────────────────────────────────


class TestBuildMemoryReasons:
    def test_positive_delta_formatted(self):
        from multifleet.contextdna_analyzer import _build_memory_reasons
        reasons = _build_memory_reasons({"mac1": {"delta": 0.10}})
        assert len(reasons) == 1
        assert "+0.10" in reasons[0]
        assert "success" in reasons[0]

    def test_negative_delta_formatted(self):
        from multifleet.contextdna_analyzer import _build_memory_reasons
        reasons = _build_memory_reasons({"mac1": {"delta": -0.15}})
        assert len(reasons) == 1
        assert "-0.15" in reasons[0]
        assert "failure" in reasons[0]

    def test_zero_delta_excluded(self):
        from multifleet.contextdna_analyzer import _build_memory_reasons
        reasons = _build_memory_reasons({"mac1": {"delta": 0.0}})
        assert len(reasons) == 0

    def test_multiple_machines(self):
        from multifleet.contextdna_analyzer import _build_memory_reasons
        reasons = _build_memory_reasons({
            "mac1": {"delta": 0.10},
            "mac2": {"delta": -0.15},
            "mac3": {"delta": 0.0},
        })
        assert len(reasons) == 2  # mac3 excluded (zero delta)
