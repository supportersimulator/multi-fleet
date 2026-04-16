"""Tests for corrigibility_ledger — proves surgeon input changes outcomes."""

import json
import tempfile
import os
import pytest

from multifleet.corrigibility_ledger import CorrigibilityLedger


@pytest.fixture
def ledger(tmp_path):
    db = str(tmp_path / "corrigibility.db")
    return CorrigibilityLedger(db_path=db)


def test_record_decision_point(ledger):
    row_id = ledger.record_decision_point(
        context="merge feature-x",
        proposed_action="accept",
        surgeon_input="cardiologist dissents: missing tests",
        final_action="blocked",
        changed=True,
        source="cross_exam",
    )
    assert row_id >= 1


def test_record_unchanged_decision(ledger):
    row_id = ledger.record_decision_point(
        context="deploy hotfix",
        proposed_action="proceed",
        surgeon_input="all surgeons agree",
        final_action="proceed",
        changed=False,
        source="validate_acceptance",
    )
    assert row_id >= 1


def test_record_disagreement(ledger):
    row_id = ledger.record_disagreement(
        topic="schema migration strategy",
        surgeon_a_position="atlas: migrate in-place",
        surgeon_b_position="cardiologist: blue-green deploy",
        resolution="blue-green deploy chosen",
        evidence={"risk": "high", "rollback_plan": "yes"},
    )
    assert row_id >= 1


def test_get_impact_stats_empty(ledger):
    stats = ledger.get_impact_stats()
    assert stats["total_decisions"] == 0
    assert stats["changed_by_surgeon"] == 0
    assert stats["change_rate"] == 0.0
    assert stats["total_disagreements"] == 0


def test_get_impact_stats_with_data(ledger):
    # 3 decisions: 2 changed, 1 unchanged
    ledger.record_decision_point("a", "accept", "dissent", "blocked", True, "test")
    ledger.record_decision_point("b", "accept", "dissent", "blocked", True, "test")
    ledger.record_decision_point("c", "proceed", "agree", "proceed", False, "test")
    ledger.record_disagreement("x", "pos-a", "pos-b", "pos-b chosen")

    stats = ledger.get_impact_stats()
    assert stats["total_decisions"] == 3
    assert stats["changed_by_surgeon"] == 2
    assert stats["unchanged"] == 1
    assert stats["change_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert stats["total_disagreements"] == 1


def test_get_recent_disagreements(ledger):
    for i in range(15):
        ledger.record_disagreement(
            f"topic-{i}", f"pos-a-{i}", f"pos-b-{i}", f"resolution-{i}"
        )

    recent = ledger.get_recent_disagreements(limit=5)
    assert len(recent) == 5
    # Newest first
    assert recent[0]["topic"] == "topic-14"
    assert recent[4]["topic"] == "topic-10"


def test_disagreement_evidence_roundtrip(ledger):
    evidence = {"files_changed": 12, "blast_radius": "high"}
    ledger.record_disagreement("topic", "a", "b", "resolution", evidence)

    recent = ledger.get_recent_disagreements(limit=1)
    assert recent[0]["evidence"] == evidence


def test_multiple_sources_tracked(ledger):
    ledger.record_decision_point("a", "p", "s", "f", True, "cross_exam")
    ledger.record_decision_point("b", "p", "s", "f", False, "validate_acceptance")
    ledger.record_decision_point("c", "p", "s", "f", True, "invariance_gate:send")

    stats = ledger.get_impact_stats()
    assert stats["total_decisions"] == 3
    assert stats["changed_by_surgeon"] == 2
