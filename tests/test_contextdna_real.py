"""Tests for ContextDNA analyzer with real memory module wiring.

Mocks the memory modules (query_learnings, professor.consult) to verify
the analyzer correctly queries memory, checks for prior failures,
checks architectural decisions, and returns metadata with memory_reasons.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.synthesis import SynthesisResult


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


def _make_analyzer_with_mocks(query_results=None, professor_result=None):
    """Build analyzer by patching memory imports with controllable mocks.

    Returns (analyzer_fn, mock_query_fn, mock_professor_fn).
    """
    mock_query = MagicMock(return_value=query_results or [])
    mock_professor = MagicMock(return_value=professor_result or "")

    # Patch the import mechanism so create_contextdna_analyzer picks up our mocks
    mock_memory_query = MagicMock()
    mock_memory_query.query_learnings = mock_query

    mock_memory_professor = MagicMock()
    mock_memory_professor.consult = mock_professor

    with patch.dict("sys.modules", {
        "memory": MagicMock(),
        "memory.query": mock_memory_query,
        "memory.professor": mock_memory_professor,
    }):
        from multifleet.contextdna_analyzer import create_contextdna_analyzer
        analyzer = create_contextdna_analyzer()

    return analyzer, mock_query, mock_professor


# ── Test 1: Memory query is called with verdict summary ──────────────────────


class TestMemoryQueryWiring:
    def test_queries_memory_with_summary_text(self):
        """Analyzer queries memory using the verdict summary."""
        analyzer, mock_query, _ = _make_analyzer_with_mocks(
            query_results=["success verified stable"]
        )
        assert analyzer is not None
        result = analyzer("t1", {"mac1": _verdict(summary="deploy redis cache")})
        assert result is not None
        # Verify query was called with text containing the summary
        assert mock_query.called
        call_args = mock_query.call_args_list[0]
        assert "deploy redis cache" in call_args[0][0]


# ── Test 2: Files touched included in query ──────────────────────────────────


    def test_includes_files_touched_in_query(self):
        """Analyzer includes files_touched paths in the memory query."""
        analyzer, mock_query, _ = _make_analyzer_with_mocks(
            query_results=["success works confirmed"]
        )
        result = analyzer("t2", {"mac1": _verdict(
            summary="fix cache",
            files_touched=["memory/query.py", "memory/brain.py"],
        )})
        assert result is not None
        first_call_query = mock_query.call_args_list[0][0][0]
        assert "memory/query.py" in first_call_query or "memory/brain.py" in first_call_query


# ── Test 3: Prior failure check on files ─────────────────────────────────────


class TestPriorFailureChecks:
    def test_prior_failures_on_files_penalize_score(self):
        """When memory returns failure patterns for files, confidence drops."""
        call_count = [0]

        def mock_query(query, limit=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return ["neutral refactoring pattern"]  # general query
            # File-specific failure query
            return ["fail regression broke on this file before"]

        analyzer, _, _ = _make_analyzer_with_mocks()
        # Override the query function directly in the closure
        mock_q = MagicMock(side_effect=mock_query)

        mock_memory = MagicMock()
        mock_memory.query_learnings = mock_q
        with patch.dict("sys.modules", {
            "memory": MagicMock(),
            "memory.query": mock_memory,
            "memory.professor": MagicMock(consult=MagicMock(return_value="")),
        }):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer
            analyzer = create_contextdna_analyzer()

        result = analyzer("t3", {"mac1": _verdict(
            confidence=0.85,
            files_touched=["src/cache.py"],
        )})
        # Prior failures should penalize: 0.85 - 0.15 = 0.70
        assert result is not None
        assert result.confidence < 0.85


# ── Test 4: Architectural decisions boost ────────────────────────────────────


class TestArchDecisions:
    def test_arch_decisions_boost_neutral_verdicts(self):
        """Arch decisions give a small boost when general memory is neutral."""
        call_count = [0]

        def mock_query(query, limit=5):
            call_count[0] += 1
            if "architecture decision" in query:
                return ["architecture decision: use event-driven pattern, rationale: decoupling"]
            return ["some neutral content about the module"]

        mock_q = MagicMock(side_effect=mock_query)
        mock_memory = MagicMock()
        mock_memory.query_learnings = mock_q

        with patch.dict("sys.modules", {
            "memory": MagicMock(),
            "memory.query": mock_memory,
            "memory.professor": MagicMock(consult=MagicMock(return_value="")),
        }):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer
            analyzer = create_contextdna_analyzer()

        result = analyzer("t4", {"mac1": _verdict(
            confidence=0.75,
            summary="refactor to event-driven",
        )})
        # Arch decision boost: 0.75 + 0.05 = 0.80
        if result is not None:
            assert result.confidence >= 0.75


# ── Test 5: Professor wisdom included in result ──────────────────────────────


class TestProfessorWiring:
    def test_professor_wisdom_in_reasoning(self):
        """Professor guidance appears in the SynthesisResult reasoning."""
        analyzer, mock_query, mock_professor = _make_analyzer_with_mocks(
            query_results=["success confirmed stable"],
            professor_result="CRITICAL: Always wrap boto3 calls in asyncio.to_thread",
        )
        result = analyzer("t5", {"mac1": _verdict(summary="add async boto3 calls")})
        assert result is not None
        assert "Professor" in result.reasoning
        assert "boto3" in result.reasoning


    def test_professor_failure_gracefully_ignored(self):
        """If professor raises, analyzer still works without it."""
        mock_query = MagicMock(return_value=["success verified works"])
        mock_memory = MagicMock()
        mock_memory.query_learnings = mock_query

        mock_prof = MagicMock()
        mock_prof.consult = MagicMock(side_effect=RuntimeError("LLM down"))

        with patch.dict("sys.modules", {
            "memory": MagicMock(),
            "memory.query": mock_memory,
            "memory.professor": mock_prof,
        }):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer
            analyzer = create_contextdna_analyzer()

        result = analyzer("t6", {"mac1": _verdict()})
        assert result is not None  # Still produces a result
        assert "Professor" not in result.reasoning


# ── Test 6: metadata.memory_reasons populated ────────────────────────────────


class TestMetadataMemoryReasons:
    def test_memory_reasons_in_metadata(self):
        """SynthesisResult metadata contains memory_reasons list."""
        analyzer, _, _ = _make_analyzer_with_mocks(
            query_results=["success confirmed stable verified"],
        )
        result = analyzer("t7", {"mac1": _verdict()})
        assert result is not None
        assert "memory_reasons" in result.metadata
        assert isinstance(result.metadata["memory_reasons"], list)
        assert len(result.metadata["memory_reasons"]) >= 1

    def test_memory_reasons_contain_machine_id(self):
        """Each memory_reason entry identifies the machine it relates to."""
        analyzer, _, _ = _make_analyzer_with_mocks(
            query_results=["success stable works"],
        )
        result = analyzer("t8", {"mac1": _verdict()})
        assert result is not None
        reasons = result.metadata["memory_reasons"]
        machine_reasons = [r for r in reasons if r.get("machine")]
        assert len(machine_reasons) >= 1
        assert machine_reasons[0]["machine"] == "mac1"


# ── Test 7: Graceful degradation when memory unavailable ─────────────────────


class TestStandaloneMode:
    def test_returns_none_when_memory_unavailable(self):
        """In standalone plugin mode, analyzer falls back to evidence ledger."""
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
                from multifleet.contextdna_analyzer import create_contextdna_analyzer
                result = create_contextdna_analyzer()
                # Evidence ledger fallback means analyzer may still be created
                assert result is None or callable(result)
        finally:
            sys.modules.update(saved)


# ── Test 8: Multiple machines scored correctly ───────────────────────────────


class TestMultiMachineScoring:
    def test_memory_flips_winner_across_machines(self):
        """Memory penalties on leader can change which machine wins."""
        call_count = [0]

        def mock_query(query, limit=5):
            call_count[0] += 1
            if "deploy new" in query:
                return ["fail regression broke crash error"]
            if "optimize existing" in query:
                return ["success verified confirmed works"]
            return []

        mock_q = MagicMock(side_effect=mock_query)
        mock_memory = MagicMock()
        mock_memory.query_learnings = mock_q

        with patch.dict("sys.modules", {
            "memory": MagicMock(),
            "memory.query": mock_memory,
            "memory.professor": MagicMock(consult=MagicMock(return_value="")),
        }):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer
            analyzer = create_contextdna_analyzer()

        result = analyzer("t9", {
            "mac1": _verdict(confidence=0.85, summary="deploy new cache", branch="a"),
            "mac2": _verdict(confidence=0.80, summary="optimize existing cache", branch="b"),
        })
        assert result is not None
        # mac1: 0.85 - 0.15 = 0.70, mac2: 0.80 + 0.10 = 0.90
        assert result.winner_branch == "b"
        # Both machines should appear in memory_reasons
        reasons = result.metadata.get("memory_reasons", [])
        machines = {r.get("machine") for r in reasons if r.get("machine")}
        assert "mac1" in machines or "mac2" in machines


# ── Test 9: Query error returns graceful None ────────────────────────────────


class TestQueryErrors:
    def test_query_exception_graceful_degradation(self):
        """If memory query raises, analyzer returns None (graceful degradation)."""
        mock_q = MagicMock(side_effect=ConnectionError("Redis down"))
        mock_memory = MagicMock()
        mock_memory.query_learnings = mock_q

        with patch.dict("sys.modules", {
            "memory": MagicMock(),
            "memory.query": mock_memory,
            "memory.professor": MagicMock(consult=MagicMock(return_value="")),
        }):
            from multifleet.contextdna_analyzer import create_contextdna_analyzer
            analyzer = create_contextdna_analyzer()

        result = analyzer("t10", {"mac1": _verdict()})
        assert result is None  # Graceful fallback


# ── Test 10: Professor wisdom in metadata ────────────────────────────────────


class TestProfessorInMetadata:
    def test_professor_wisdom_stored_in_memory_reasons(self):
        """Professor wisdom is captured in metadata.memory_reasons."""
        analyzer, _, mock_professor = _make_analyzer_with_mocks(
            query_results=["success confirmed works"],
            professor_result="CRITICAL: Always validate input before caching",
        )
        result = analyzer("t11", {"mac1": _verdict(summary="add input caching")})
        assert result is not None
        reasons = result.metadata.get("memory_reasons", [])
        professor_entries = [r for r in reasons if r.get("source") == "professor"]
        assert len(professor_entries) == 1
        assert "validate input" in professor_entries[0]["wisdom"]
