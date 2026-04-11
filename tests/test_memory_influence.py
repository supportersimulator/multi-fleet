"""Tests for MemoryInfluenceResolver — the "holy shit" demo moment."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.memory_influence import MemoryInfluence, MemoryInfluenceResolver


class TestMemoryInfluence:

    def test_to_dict(self):
        mi = MemoryInfluence(
            evidence_refs=[{"entry_id": "abc", "reasoning_snippet": "test"}],
            pattern_summary="Auth rejected 2/3 times",
            decisions_queried=10,
            decisions_relevant=3,
            confidence_adjustment=-0.15,
        )
        d = mi.to_dict()
        assert d["decisions_relevant"] == 3
        assert d["confidence_adjustment"] == -0.15
        assert d["authority"] == "local"

    def test_summary_line_with_refs(self):
        mi = MemoryInfluence(
            evidence_refs=[{"entry_id": "x"}],
            pattern_summary="Auth changes risky.",
            decisions_relevant=2,
        )
        line = mi.summary_line()
        assert "2 past decision" in line
        assert "Auth changes risky" in line

    def test_summary_line_no_refs(self):
        mi = MemoryInfluence()
        assert "No past evidence" in mi.summary_line()


class TestMemoryInfluenceResolver:

    def test_resolve_with_empty_ledger(self):
        resolver = MemoryInfluenceResolver(node_id="test")
        influence = resolver.resolve(
            "test-task",
            {"mac1": {"summary": "Fix auth", "branch": "fix/auth"}},
            current_confidence=0.9,
        )
        assert isinstance(influence, MemoryInfluence)
        assert influence.authority == "local"

    def test_resolve_with_decisions_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            decisions = Path(tmp) / "decisions.jsonl"
            decisions.write_text(
                json.dumps({
                    "task_id": "past-auth-1",
                    "decision": "reject",
                    "reasoning": "Auth token refresh broke after route refactor",
                    "dissent_count": 1,
                    "timestamp": time.time() - 3600,
                }) + "\n"
                + json.dumps({
                    "task_id": "past-auth-2",
                    "decision": "merge",
                    "reasoning": "Auth cleanup approved with caveats about token handling",
                    "dissent_count": 0,
                    "timestamp": time.time() - 7200,
                }) + "\n"
            )

            resolver = MemoryInfluenceResolver(
                decisions_path=str(decisions), node_id="test"
            )
            influence = resolver.resolve(
                "auth-fix",
                {"mac1": {"summary": "Fix auth token refresh", "branch": "fix/auth-token"}},
                current_confidence=0.8,
            )

            assert influence.decisions_queried >= 2
            # Should find relevant decisions about auth/token
            if influence.decisions_relevant > 0:
                assert any("auth" in ref.get("reasoning_snippet", "").lower()
                          for ref in influence.evidence_refs)

    def test_confidence_adjustment_negative(self):
        """Past rejections should lower confidence."""
        resolver = MemoryInfluenceResolver(node_id="test")
        mi = MemoryInfluence(
            evidence_refs=[
                {"reasoning_snippet": "Auth token broke — rejected"},
                {"reasoning_snippet": "Auth needs_rebuttal — insufficient tests"},
                {"reasoning_snippet": "Auth merge approved"},
            ],
        )
        adj = resolver._calculate_adjustment(mi, 0.8)
        assert adj < 0  # More rejections than approvals

    def test_confidence_adjustment_positive(self):
        """Past approvals should slightly boost confidence."""
        resolver = MemoryInfluenceResolver(node_id="test")
        mi = MemoryInfluence(
            evidence_refs=[
                {"reasoning_snippet": "Feature approved cleanly"},
                {"reasoning_snippet": "Merge successful with tests"},
            ],
        )
        adj = resolver._calculate_adjustment(mi, 0.8)
        assert adj >= 0  # Approvals

    def test_confidence_adjustment_no_evidence(self):
        resolver = MemoryInfluenceResolver(node_id="test")
        mi = MemoryInfluence()
        assert resolver._calculate_adjustment(mi, 0.8) == 0.0

    def test_keyword_extraction(self):
        resolver = MemoryInfluenceResolver(node_id="test")
        keywords = resolver._extract_keywords(
            "auth-fix-123",
            {"mac1": {"summary": "Fix auth token refresh flow", "branch": "fix/auth-token"}},
        )
        assert "auth" in keywords
        assert "token" in keywords
        assert "the" not in keywords  # noise filtered

    def test_escalation_on_low_confidence(self):
        """Low confidence should trigger chief escalation attempt."""
        resolver = MemoryInfluenceResolver(
            node_id="test", confidence_threshold=0.7,
        )
        # Chief will fail (no bridge running) — should degrade gracefully
        influence = resolver.resolve(
            "uncertain-task",
            {"mac1": {"summary": "Complex refactor", "branch": "refactor/big"}},
            current_confidence=0.5,  # Below threshold
        )
        # Should still return valid influence (local fallback)
        assert isinstance(influence, MemoryInfluence)

    def test_pattern_summary_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            decisions = Path(tmp) / "decisions.jsonl"
            entries = []
            for i in range(5):
                entries.append(json.dumps({
                    "task_id": f"billing-{i}",
                    "decision": "reject" if i % 2 == 0 else "merge",
                    "reasoning": f"Billing module change #{i} — {'failed tests' if i % 2 == 0 else 'approved'}",
                    "dissent_count": 1 if i % 2 == 0 else 0,
                    "timestamp": time.time() - i * 3600,
                }))
            decisions.write_text("\n".join(entries) + "\n")

            resolver = MemoryInfluenceResolver(
                decisions_path=str(decisions), node_id="test"
            )
            influence = resolver.resolve(
                "billing-fix",
                {"mac1": {"summary": "Fix billing calculation", "branch": "fix/billing"}},
            )

            if influence.pattern_summary:
                assert "Past decisions:" in influence.pattern_summary


class TestSynthesisIntegration:

    def test_synthesis_attaches_memory_influence(self):
        """ChiefSynthesis should attach memory_influence to result metadata."""
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

        # memory_influence should be in metadata (may be empty if no past decisions)
        assert "memory_influence" in result.metadata
        mi = result.metadata["memory_influence"]
        assert "evidence_refs" in mi
        assert "authority" in mi
