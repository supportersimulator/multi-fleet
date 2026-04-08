"""Tests for Human Arbiter View — decision interface for fleet disagreements."""

import json
import time
import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.arbiter_view import (
    register_pending_decision,
    get_pending_decisions,
    get_decision,
    record_decision,
    create_pending_from_race,
    build_arbiter_html,
    _pending_decisions,
    _decided,
)


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset arbiter state between tests."""
    _pending_decisions.clear()
    _decided.clear()
    yield
    _pending_decisions.clear()
    _decided.clear()


def _make_race(confidence=0.6, winner="mac1", task="Fix auth flow"):
    """Build a mock race dict for testing."""
    return {
        "race_id": "abc123",
        "task": task,
        "status": "synthesizing",
        "started_at": time.time() - 120,
        "winner": winner,
        "participants": {
            "mac1": {
                "node": "mac1",
                "branch": "race/abc123/mac1",
                "status": "completed",
                "result": {
                    "summary": "Refactored auth with JWT tokens",
                    "confidence": 0.75,
                    "strengths": ["clean API", "backward compatible"],
                    "risks": ["adds dependency"],
                },
            },
            "mac2": {
                "node": "mac2",
                "branch": "race/abc123/mac2",
                "status": "completed",
                "result": {
                    "summary": "Session-based auth with Redis",
                    "confidence": 0.65,
                    "strengths": ["simple", "no new deps"],
                    "risks": ["Redis required"],
                },
            },
        },
        "synthesis": {
            "winner_branch": "race/abc123/mac1",
            "confidence": confidence,
            "reasoning": "JWT approach has better long-term scalability.",
            "decision": "merge",
            "dissent_preserved": [
                {
                    "surgeon": "Cardiologist",
                    "claim": "JWT tokens add complexity without clear benefit for internal service.",
                    "severity": "medium",
                },
            ],
            "chief_decision_packet": {"type": "chief_decision", "task_id": "abc123"},
        },
    }


class TestPendingDecisions:

    def test_pending_when_race_complete(self):
        """Race with low confidence creates a pending arbiter decision."""
        race = _make_race(confidence=0.6)
        did = create_pending_from_race(race)
        assert did is not None
        pending = get_pending_decisions()
        assert len(pending) == 1
        assert pending[0]["decision_id"] == did
        assert pending[0]["type"] == "race"
        assert pending[0]["status"] == "awaiting_human"

    def test_high_confidence_no_pending(self):
        """Race with high confidence does NOT create a pending decision."""
        race = _make_race(confidence=0.9)
        did = create_pending_from_race(race)
        assert did is None
        assert len(get_pending_decisions()) == 0

    def test_no_pending_returns_empty(self):
        """Empty state returns empty list."""
        assert get_pending_decisions() == []

    def test_register_manual_decision(self):
        """Manually registered decisions appear in pending."""
        did = register_pending_decision({
            "type": "rebuttal",
            "rebuttal_id": "reb-001",
            "task": "Schema migration approach",
            "recommendation": {"confidence": 0.5, "reasoning": "Unclear winner"},
            "alternatives": [],
            "dissent": [],
        })
        assert did is not None
        assert len(get_pending_decisions()) == 1


class TestDecide:

    def test_decide_records_and_resolves(self):
        """Recording a decision moves it from pending to decided."""
        race = _make_race(confidence=0.5)
        did = create_pending_from_race(race)
        assert len(get_pending_decisions()) == 1

        result = record_decision(did, "accept_recommendation", {"notes": "LGTM"})
        assert result["status"] == "decided"
        assert result["action"] == "accept_recommendation"
        assert "decision_latency_s" in result
        assert len(get_pending_decisions()) == 0
        # Should be in decided
        assert get_decision(did) is not None
        assert get_decision(did)["status"] == "decided"

    def test_decide_unknown_id(self):
        """Deciding on unknown ID returns error."""
        result = record_decision("nonexistent", "accept_recommendation")
        assert "error" in result

    def test_decide_accept_alternative(self):
        """Accept alternative action records correctly."""
        race = _make_race(confidence=0.4)
        did = create_pending_from_race(race)
        result = record_decision(did, "accept_alternative", {"alternative_index": 0, "node": "mac2"})
        assert result["action"] == "accept_alternative"
        assert result["action_details"]["node"] == "mac2"

    def test_decide_override(self):
        """Override with custom text records correctly."""
        race = _make_race(confidence=0.3)
        did = create_pending_from_race(race)
        result = record_decision(did, "override", {"custom_text": "Use neither; start fresh."})
        assert result["action"] == "override"
        assert "start fresh" in result["action_details"]["custom_text"]


class TestArbiterHTML:

    def test_arbiter_html_renders(self):
        """HTML page builds without errors and contains key elements."""
        html = build_arbiter_html(daemon_port=8855)
        assert "<!DOCTYPE html>" in html
        assert "Fleet Arbiter" in html
        assert "AWAITING HUMAN DECISION" in html
        assert "Accept Recommendation" in html
        assert "Override: Custom" in html
        assert "Request More Info" in html
        assert "/arbiter/pending" in html
        assert "/arbiter/decide" in html

    def test_html_contains_superset_theme(self):
        """HTML uses the Superset dark theme colors."""
        html = build_arbiter_html()
        assert "#1a1a2e" in html
        assert "#16213e" in html
        assert "#22c55e" in html  # green


class TestCreateFromRace:

    def test_race_decision_has_alternatives(self):
        """Decision from race includes non-winner participants as alternatives."""
        race = _make_race(confidence=0.5)
        did = create_pending_from_race(race)
        dec = get_decision(did)
        assert len(dec["alternatives"]) == 1
        assert dec["alternatives"][0]["node"] == "mac2"

    def test_race_decision_has_dissent(self):
        """Decision from race preserves dissent from synthesis."""
        race = _make_race(confidence=0.5)
        did = create_pending_from_race(race)
        dec = get_decision(did)
        assert len(dec["dissent"]) == 1
        assert dec["dissent"][0]["surgeon"] == "Cardiologist"

    def test_race_decision_has_recommendation(self):
        """Decision from race has proper recommendation fields."""
        race = _make_race(confidence=0.5)
        did = create_pending_from_race(race)
        dec = get_decision(did)
        rec = dec["recommendation"]
        assert rec["confidence"] == 0.5
        assert rec["branch"] == "race/abc123/mac1"
        assert rec["node"] == "mac1"
        assert "scalability" in rec["reasoning"]
