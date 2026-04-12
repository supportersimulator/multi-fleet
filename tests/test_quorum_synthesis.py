"""Tests for quorum sensing <-> chief synthesis integration.

Covers:
1. QuorumAnalyzer returns structured analysis
2. Synthesis decisions create quorum proposals (feedback loop)
3. Stress-aware gating at all thresholds
4. Integration: full flow from synthesis -> quorum -> evidence
"""

import os
import pytest
import tempfile

from multifleet.synthesis import ChiefSynthesis, SynthesisResult
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.stress_score import (
    STRESS_NORMAL,
    STRESS_GUARDED,
    STRESS_DEGRADED,
    STRESS_CRITICAL,
)
from multifleet.quorum_analyzer import (
    QuorumAnalyzer,
    apply_synthesis_to_quorum,
    enrich_result_with_quorum,
    create_quorum_analyzer,
)


# ── Fixtures ──


@pytest.fixture
def registry():
    return QuorumRegistry()


@pytest.fixture
def analyzer(registry):
    return QuorumAnalyzer(registry=registry, node_id="chief")


def _make_verdicts(**overrides):
    """Build a simple verdicts dict for testing."""
    base = {
        "mac1": {
            "confidence": 0.8,
            "branch": "mac1/feature",
            "risks": [],
            "dissent": [],
            "strengths": ["fast"],
            "successful_tasks": 10,
            "failed_tasks": 1,
        },
        "mac2": {
            "confidence": 0.6,
            "branch": "mac2/feature",
            "risks": ["untested"],
            "dissent": [],
            "strengths": ["thorough"],
            "successful_tasks": 5,
            "failed_tasks": 3,
        },
    }
    base.update(overrides)
    return base


def _make_result(task_id="test-task", decision="merge", confidence=0.8):
    return SynthesisResult(
        task_id=task_id,
        decision=decision,
        winner_branch="mac1/feature",
        confidence=confidence,
        reasoning="Test reasoning",
    )


# ── 1. QuorumAnalyzer structured analysis ──


class TestQuorumAnalyzerAnalysis:
    def test_analyze_returns_all_fields(self, analyzer):
        verdicts = _make_verdicts()
        analysis = analyzer.analyze("test-task", verdicts)

        assert "proposals_relevant" in analysis
        assert "has_quorum_consensus" in analysis
        assert "stress_score" in analysis
        assert "stress_level" in analysis
        assert "stress_policy" in analysis
        assert "node_reputations" in analysis
        assert "recommendation" in analysis

    def test_analyze_no_stress_normal_operation(self, analyzer):
        analysis = analyzer.analyze("test-task", _make_verdicts())

        assert analysis["stress_level"] == STRESS_NORMAL
        assert analysis["stress_score"] == 0.0
        assert analysis["has_quorum_consensus"] is False
        assert "Normal operation" in analysis["recommendation"]

    def test_analyze_computes_node_reputations(self, analyzer):
        verdicts = _make_verdicts()
        analysis = analyzer.analyze("test-task", verdicts)

        assert "mac1" in analysis["node_reputations"]
        assert "mac2" in analysis["node_reputations"]
        # mac1 has better success rate (10/11 vs 5/8)
        assert analysis["node_reputations"]["mac1"] > analysis["node_reputations"]["mac2"]

    def test_analyze_finds_relevant_proposals(self, registry, analyzer):
        # Add a proposal whose topic matches the task
        registry.record_support("prop-1", "test-task refactor", "mac1")
        analysis = analyzer.analyze("test-task", _make_verdicts())

        assert len(analysis["proposals_relevant"]) == 1
        assert analysis["proposals_relevant"][0]["proposal_id"] == "prop-1"

    def test_analyze_ignores_unrelated_proposals(self, registry, analyzer):
        registry.record_support("prop-2", "unrelated-thing", "mac1")
        analysis = analyzer.analyze("test-task", _make_verdicts())

        assert len(analysis["proposals_relevant"]) == 0

    def test_callable_returns_none_under_normal_stress(self, analyzer):
        """Under normal stress, analyzer defers to other analyzers."""
        result = analyzer("test-task", _make_verdicts())
        assert result is None

    def test_callable_rejects_under_critical_stress_no_quorum(self):
        """Critical stress + no quorum = reject."""
        registry = QuorumRegistry()
        # Stress kwargs that produce critical stress (>= 0.75)
        stress_kwargs = {
            "fallback_p3plus_count": 3,  # 0.30
            "stale_peers": 2,            # 0.20
            "chief_sync_failures": 2,    # 0.25 (capped)
        }
        analyzer = QuorumAnalyzer(
            registry=registry,
            node_id="chief",
            stress_kwargs=stress_kwargs,
        )
        result = analyzer("test-task", _make_verdicts())

        assert result is not None
        assert result.decision == "reject"
        assert result.confidence == 0.0
        assert "Stress-gated" in result.reasoning


# ── 2. Synthesis -> Quorum feedback loop ──


class TestSynthesisToQuorumFeedback:
    def test_strategic_decision_creates_proposal(self, registry):
        result = _make_result(confidence=0.8)
        feedback = apply_synthesis_to_quorum(result, registry, node_id="chief")

        assert feedback["signal_class"] == "strategic"
        assert feedback["proposal_created"] is True
        assert feedback["proposal_id"] is not None

    def test_high_confidence_auto_confirms(self, registry):
        result = _make_result(confidence=0.8)
        feedback = apply_synthesis_to_quorum(result, registry, node_id="chief")

        assert feedback["auto_supported"] is True
        # Proposal should have chief confirmation
        prop = registry.get_proposal(feedback["proposal_id"])
        assert prop is not None
        assert prop.chief_confirmed is True

    def test_low_confidence_no_auto_confirm(self, registry):
        result = _make_result(confidence=0.5)
        feedback = apply_synthesis_to_quorum(result, registry, node_id="chief")

        assert feedback["proposal_created"] is True
        assert feedback["auto_supported"] is False
        prop = registry.get_proposal(feedback["proposal_id"])
        assert prop.chief_confirmed is False

    def test_deterministic_proposal_id(self, registry):
        """Same result produces same proposal ID."""
        r1 = _make_result(task_id="task-A", decision="merge")
        r2 = _make_result(task_id="task-A", decision="merge")
        f1 = apply_synthesis_to_quorum(r1, registry, node_id="chief")
        f2 = apply_synthesis_to_quorum(r2, registry, node_id="chief")
        assert f1["proposal_id"] == f2["proposal_id"]


# ── 3. Stress-aware gating ──


class TestStressAwareGating:
    def test_normal_stress_no_modification(self, registry):
        result = _make_result(confidence=0.8)
        enriched = enrich_result_with_quorum(result, registry, stress_kwargs={})

        assert enriched.decision == "merge"
        assert enriched.confidence == 0.8
        assert enriched.metadata["quorum_stress"]["level"] == STRESS_NORMAL

    def test_degraded_stress_adds_warning(self, registry):
        # Produce degraded stress (>= 0.50)
        stress_kwargs = {
            "fallback_p3plus_count": 3,  # 0.30
            "stale_peers": 2,            # 0.20
        }
        result = _make_result(confidence=0.8)
        enriched = enrich_result_with_quorum(result, registry, stress_kwargs=stress_kwargs)

        assert enriched.decision == "merge"  # Not rejected
        assert abs(enriched.confidence - 0.7) < 1e-9  # Reduced by 0.1
        assert "Stress warning" in enriched.reasoning
        assert enriched.metadata["quorum_stress"]["level"] == STRESS_DEGRADED

    def test_critical_stress_without_quorum_rejects(self, registry):
        # Produce critical stress (>= 0.75)
        stress_kwargs = {
            "fallback_p3plus_count": 3,  # 0.30
            "stale_peers": 2,            # 0.20
            "chief_sync_failures": 2,    # 0.25 (capped)
        }
        result = _make_result(confidence=0.9)
        enriched = enrich_result_with_quorum(result, registry, stress_kwargs=stress_kwargs)

        assert enriched.decision == "reject"
        assert enriched.confidence == 0.0
        assert "Stress-gated" in enriched.reasoning

    def test_critical_stress_with_quorum_allows(self, registry):
        # Create a proposal with quorum for the task.
        # has_quorum() requires nodes + authority + surgeon check.
        # Surgeon adapter gracefully degrades (ImportError -> quorum stands).
        registry.record_support("prop-match", "test-task related", "mac1")
        registry.record_support("prop-match", "test-task related", "mac2")
        registry.record_chief_confirmation("prop-match")

        # Verify quorum is reached (surgeon adapter unavailable = graceful pass)
        assert registry.has_quorum("prop-match") is True

        stress_kwargs = {
            "fallback_p3plus_count": 3,
            "stale_peers": 2,
            "chief_sync_failures": 2,
        }
        result = _make_result(task_id="test-task", confidence=0.9)
        enriched = enrich_result_with_quorum(result, registry, stress_kwargs=stress_kwargs)

        # With quorum reached, critical stress should NOT reject
        assert enriched.decision == "merge"
        assert enriched.confidence == 0.9
        assert enriched.metadata["quorum_proposals"]["has_consensus"] is True

    def test_guarded_stress_no_penalty(self, registry):
        # Produce guarded stress (0.25-0.49)
        stress_kwargs = {"fallback_p3plus_count": 3}  # 0.30
        result = _make_result(confidence=0.8)
        enriched = enrich_result_with_quorum(result, registry, stress_kwargs=stress_kwargs)

        assert enriched.decision == "merge"
        assert enriched.confidence == 0.8  # No penalty for guarded
        assert enriched.metadata["quorum_stress"]["level"] == STRESS_GUARDED


# ── 4. Integration: full flow ──


class TestIntegrationFlow:
    def test_full_synthesis_with_quorum(self):
        """Full flow: ChiefSynthesis -> quorum analyzer -> feedback -> evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "decisions.jsonl")
            chief = ChiefSynthesis(ledger_path=ledger)

            # Ensure quorum registry is set up
            assert chief._quorum_registry is not None

            verdicts = _make_verdicts()
            result = chief.synthesize("integration-test", verdicts)

            # Result should have quorum metadata
            assert "quorum_stress" in result.metadata
            assert "quorum_proposals" in result.metadata
            assert "quorum_feedback" in result.metadata

            # Decision was recorded to ledger
            decisions = chief.recent_decisions(limit=1)
            assert len(decisions) == 1
            assert decisions[0]["task_id"] == "integration-test"

    def test_synthesis_creates_proposal_in_registry(self):
        """Synthesis result should create a strategic proposal."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "decisions.jsonl")
            chief = ChiefSynthesis(ledger_path=ledger)

            verdicts = _make_verdicts()
            result = chief.synthesize("proposal-test", verdicts)

            # Check that a proposal was created
            feedback = result.metadata.get("quorum_feedback", {})
            assert feedback.get("proposal_created") is True
            assert feedback.get("proposal_id") is not None

            # Verify it's in the registry
            prop = chief._quorum_registry.get_proposal(feedback["proposal_id"])
            assert prop is not None

    def test_factory_function(self):
        """create_quorum_analyzer factory works."""
        registry = QuorumRegistry()
        analyzer = create_quorum_analyzer(registry=registry, node_id="test")
        assert isinstance(analyzer, QuorumAnalyzer)
        assert analyzer.node_id == "test"
        assert analyzer.registry is registry

    def test_empty_verdicts_still_get_quorum_metadata(self):
        """Even rejected (no verdicts) results get quorum enrichment."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "decisions.jsonl")
            chief = ChiefSynthesis(ledger_path=ledger)
            result = chief.synthesize("empty-test", {})

            assert result.decision == "reject"
            # Quorum feedback still applied (on the reject result)
            assert "quorum_stress" in result.metadata
