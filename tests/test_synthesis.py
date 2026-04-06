"""Tests for the Chief Synthesis Engine."""

import json
import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.synthesis import ChiefSynthesis, SynthesisResult


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "decisions.jsonl"


@pytest.fixture
def engine(ledger):
    return ChiefSynthesis(ledger_path=str(ledger))


def _verdict(confidence=0.8, risks=None, dissent=None, strengths=None,
             branch="feat-x", followups=None, **extra):
    v = {
        "confidence": confidence,
        "risks": risks or [],
        "dissent": dissent or [],
        "strengths": strengths or [],
        "branch": branch,
        "followups": followups or [],
    }
    v.update(extra)
    return v


# ── SynthesisResult tests ────────────────────────────────────────────────────


class TestSynthesisResult:
    def test_to_chief_decision_fields(self):
        r = SynthesisResult(
            task_id="t1",
            decision="merge",
            winner_branch="feat-x",
            confidence=0.85,
            reasoning="High confidence",
            cherry_pick_from=[{"machine": "mac2", "commit": "abc", "reason": "good idea"}],
            followups=["run tests"],
            dissent_preserved=[{"surgeon": "neuro", "machine": "mac2", "claim": "risky"}],
        )
        d = r.to_chief_decision()
        assert d["type"] == "chief_decision"
        assert d["task_id"] == "t1"
        assert d["decision"] == "merge"
        assert d["winner_branch"] == "feat-x"
        assert d["confidence"] == 0.85
        assert d["reasoning"] == "High confidence"
        assert len(d["cherry_pick_from"]) == 1
        assert len(d["followups"]) == 1
        assert d["dissent_count"] == 1
        assert "timestamp" in d

    def test_to_chief_decision_empty_lists(self):
        r = SynthesisResult(
            task_id="t2", decision="reject", winner_branch=None,
            confidence=0.0, reasoning="No data",
        )
        d = r.to_chief_decision()
        assert d["cherry_pick_from"] == []
        assert d["followups"] == []
        assert d["dissent_preserved"] == []
        assert d["dissent_count"] == 0

    def test_confidence_rounding(self):
        r = SynthesisResult(
            task_id="t3", decision="merge", winner_branch="b",
            confidence=0.123456789, reasoning="test",
        )
        d = r.to_chief_decision()
        assert d["confidence"] == 0.1235


# ── Basic confidence analyzer ────────────────────────────────────────────────


class TestBasicConfidenceAnalyzer:
    def test_single_verdict(self, engine):
        result = engine.synthesize("task-1", {
            "mac1": _verdict(confidence=0.9, branch="feat-a"),
        })
        assert result.decision == "merge"
        assert result.winner_branch == "feat-a"
        assert result.confidence == 0.9
        assert result.dissent_preserved == []

    def test_multiple_verdicts_highest_wins(self, engine):
        result = engine.synthesize("task-2", {
            "mac1": _verdict(confidence=0.7, branch="branch-a"),
            "mac2": _verdict(confidence=0.9, branch="branch-b"),
            "mac3": _verdict(confidence=0.5, branch="branch-c"),
        })
        assert result.winner_branch == "branch-b"
        assert result.confidence <= 0.9

    def test_risk_penalty(self, engine):
        result = engine.synthesize("task-3", {
            "mac1": _verdict(confidence=0.9, risks=["r1", "r2", "r3"], branch="risky"),
            "mac2": _verdict(confidence=0.8, risks=[], branch="safe"),
        })
        # mac1: 0.9 - 3*0.05 = 0.75
        # mac2: 0.8 - 0 = 0.8
        assert result.winner_branch == "safe"

    def test_dissent_penalty(self, engine):
        result = engine.synthesize("task-4", {
            "mac1": _verdict(confidence=0.9, dissent=[{"claim": "bad"}] * 3, branch="dissented"),
            "mac2": _verdict(confidence=0.7, dissent=[], branch="clean"),
        })
        # mac1: 0.9 - 3*0.1 = 0.6
        # mac2: 0.7
        assert result.winner_branch == "clean"

    def test_tie_breaking_deterministic(self, engine):
        """When scores are equal, max() picks consistently."""
        result = engine.synthesize("task-5", {
            "mac1": _verdict(confidence=0.8, branch="a"),
            "mac2": _verdict(confidence=0.8, branch="b"),
        })
        # Both score 0.8; should pick one consistently
        assert result.winner_branch in ("a", "b")
        assert result.decision == "merge"

    def test_combined_penalties(self, engine):
        result = engine.synthesize("task-6", {
            "mac1": _verdict(confidence=0.95, risks=["r1"], dissent=[{"c": "d1"}], branch="a"),
            "mac2": _verdict(confidence=0.8, branch="b"),
        })
        # mac1: 0.95 - 0.05 - 0.1 = 0.8
        # mac2: 0.8
        # Tied — either wins
        assert result.confidence == 0.8


# ── Dissent preservation ─────────────────────────────────────────────────────


class TestDissentPreservation:
    def test_all_dissent_collected(self, engine):
        result = engine.synthesize("task-d1", {
            "mac1": _verdict(dissent=[{"surgeon": "atlas", "claim": "concern-1"}]),
            "mac2": _verdict(dissent=[{"surgeon": "neuro", "claim": "concern-2"}]),
            "mac3": _verdict(dissent=[
                {"surgeon": "cardio", "claim": "concern-3"},
                {"surgeon": "atlas", "claim": "concern-4"},
            ]),
        })
        assert len(result.dissent_preserved) == 4
        claims = {d["claim"] for d in result.dissent_preserved}
        assert claims == {"concern-1", "concern-2", "concern-3", "concern-4"}

    def test_dissent_includes_machine_id(self, engine):
        result = engine.synthesize("task-d2", {
            "mac1": _verdict(dissent=[{"claim": "x"}]),
        })
        assert result.dissent_preserved[0]["machine"] == "mac1"

    def test_string_dissent_converted(self, engine):
        """Dissent entries that are strings get wrapped in a dict."""
        result = engine.synthesize("task-d3", {
            "mac1": _verdict(dissent=["raw string dissent"]),
        })
        assert result.dissent_preserved[0]["claim"] == "raw string dissent"
        assert result.dissent_preserved[0]["machine"] == "mac1"


# ── Cherry-pick suggestions ──────────────────────────────────────────────────


class TestCherryPick:
    def test_cherry_pick_from_non_winners(self, engine):
        result = engine.synthesize("task-cp1", {
            "mac1": _verdict(confidence=0.9, branch="winner"),
            "mac2": _verdict(confidence=0.5, branch="loser",
                             strengths=["good error handling"]),
        })
        assert result.decision == "cherry_pick"
        assert len(result.cherry_pick_from) == 1
        assert result.cherry_pick_from[0]["machine"] == "mac2"
        assert result.cherry_pick_from[0]["reason"] == "good error handling"

    def test_no_cherry_pick_when_no_strengths(self, engine):
        result = engine.synthesize("task-cp2", {
            "mac1": _verdict(confidence=0.9),
            "mac2": _verdict(confidence=0.5),
        })
        assert result.cherry_pick_from == []
        assert result.decision == "merge"

    def test_winner_strengths_not_cherry_picked(self, engine):
        result = engine.synthesize("task-cp3", {
            "mac1": _verdict(confidence=0.9, strengths=["fast"]),
            "mac2": _verdict(confidence=0.5),
        })
        # Winner's strengths are not in cherry_pick_from
        assert result.cherry_pick_from == []


# ── Custom analyzer ──────────────────────────────────────────────────────────


class TestCustomAnalyzer:
    def test_registered_analyzer_used(self, engine):
        def custom(task_id, verdicts):
            return SynthesisResult(
                task_id=task_id,
                decision="reconciliation",
                winner_branch="custom-branch",
                confidence=0.99,
                reasoning="Custom logic",
            )

        engine.register_analyzer(custom)
        result = engine.synthesize("task-ca1", {"mac1": _verdict()})
        assert result.decision == "reconciliation"
        assert result.winner_branch == "custom-branch"
        assert result.confidence == 0.99

    def test_highest_confidence_analyzer_wins(self, engine):
        def low_conf(task_id, verdicts):
            return SynthesisResult(
                task_id=task_id, decision="reject", winner_branch=None,
                confidence=0.3, reasoning="Low",
            )

        def high_conf(task_id, verdicts):
            return SynthesisResult(
                task_id=task_id, decision="merge", winner_branch="hi",
                confidence=0.95, reasoning="High",
            )

        engine.register_analyzer(low_conf)
        engine.register_analyzer(high_conf)
        result = engine.synthesize("task-ca2", {"mac1": _verdict()})
        assert result.decision == "merge"
        assert result.confidence == 0.95

    def test_analyzer_failure_fallback(self, engine):
        def broken(task_id, verdicts):
            raise RuntimeError("boom")

        engine.register_analyzer(broken)
        # Should fall back to default analyzer
        result = engine.synthesize("task-ca3", {
            "mac1": _verdict(confidence=0.7, branch="fallback-branch"),
        })
        assert result.winner_branch == "fallback-branch"
        assert result.decision == "merge"

    def test_analyzer_returns_none_skipped(self, engine):
        def returns_none(task_id, verdicts):
            return None

        engine.register_analyzer(returns_none)
        result = engine.synthesize("task-ca4", {
            "mac1": _verdict(confidence=0.6, branch="default-wins"),
        })
        # None result is skipped, falls back to default
        assert result.winner_branch == "default-wins"


# ── Decision ledger ──────────────────────────────────────────────────────────


class TestDecisionLedger:
    def test_ledger_written(self, engine, ledger):
        engine.synthesize("task-l1", {"mac1": _verdict()})
        assert ledger.exists()
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["task_id"] == "task-l1"
        assert "timestamp" in entry
        assert "duration_ms" in entry

    def test_ledger_append_only(self, engine, ledger):
        engine.synthesize("task-l2a", {"mac1": _verdict()})
        engine.synthesize("task-l2b", {"mac1": _verdict()})
        lines = ledger.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["task_id"] == "task-l2a"
        assert json.loads(lines[1])["task_id"] == "task-l2b"

    def test_recent_decisions(self, engine):
        for i in range(5):
            engine.synthesize(f"task-r{i}", {"mac1": _verdict()})
        recent = engine.recent_decisions(limit=3)
        assert len(recent) == 3
        # Newest first
        assert recent[0]["task_id"] == "task-r4"
        assert recent[2]["task_id"] == "task-r2"

    def test_recent_decisions_empty_ledger(self, engine):
        assert engine.recent_decisions() == []

    def test_ledger_fields(self, engine, ledger):
        engine.synthesize("task-lf", {
            "mac1": _verdict(confidence=0.9),
            "mac2": _verdict(confidence=0.5, dissent=[{"claim": "x"}]),
        })
        entry = json.loads(ledger.read_text().strip())
        required = {"timestamp", "task_id", "decision", "winner_branch",
                     "confidence", "reasoning", "cherry_pick_from",
                     "dissent_count", "duration_ms"}
        assert required.issubset(set(entry.keys()))


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_verdicts(self, engine):
        result = engine.synthesize("task-e1", {})
        assert result.decision == "reject"
        assert result.confidence == 0.0
        assert result.winner_branch is None
        assert "No verdicts" in result.reasoning

    def test_single_machine_trivial(self, engine):
        result = engine.synthesize("task-e2", {
            "mac1": _verdict(confidence=0.75, branch="solo"),
        })
        assert result.decision == "merge"
        assert result.winner_branch == "solo"
        assert result.confidence == 0.75

    def test_confidence_clamped_to_0_1(self, engine):
        """Negative scores from heavy penalties get clamped to 0."""
        result = engine.synthesize("task-e3", {
            "mac1": _verdict(confidence=0.1, risks=["r"] * 10, dissent=[{"c": "d"}] * 5),
        })
        # 0.1 - 10*0.05 - 5*0.1 = 0.1 - 0.5 - 0.5 = -0.9 -> clamped to 0
        assert result.confidence == 0.0

    def test_verdict_missing_optional_fields(self, engine):
        """Verdicts with only confidence should work."""
        result = engine.synthesize("task-e4", {
            "mac1": {"confidence": 0.6},
        })
        assert result.decision == "merge"
        assert result.confidence == 0.6
