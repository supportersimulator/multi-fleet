"""End-to-end tests for the cross-machine rebuttal protocol.

Validates the full 4-phase flow:
  Phase 1: Chief assigns task (task_brief)
  Phase 2: Local surgery produces verdicts (local_verdict)
  Phase 3: Cross-machine critique (critique)
  Phase 4: Chief synthesis (chief_decision)

All tests are unit tests using mocks -- no real network, no real daemon.
"""

import json
import sys
import time
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.cross_rebuttal import (
    RebuttalManager,
    RebuttalPhase,
    RebuttalSession,
)
from multifleet.packet_registry import PacketRegistry, create_envelope
from multifleet.synthesis import ChiefSynthesis, SynthesisResult


# ── Helpers ──────────────────────────────────────────────────────────────────

MACHINES = ["mac1", "mac2", "mac3"]

# Pairwise critique assignment: mac1->mac2, mac2->mac3, mac3->mac1
CRITIQUE_PAIRS = [("mac1", "mac2"), ("mac2", "mac3"), ("mac3", "mac1")]


def _task_brief_payload(task_id="task-e2e-001", machines=None):
    return {
        "task_id": task_id,
        "objective": "Implement cross-machine rebuttal protocol",
        "scope": "multi-fleet/multifleet/",
        "deadline_minutes": 30,
        "constraints": ["no breaking changes", "must pass gains gate"],
        "assigned_to": machines or MACHINES,
    }


def _verdict_payload(task_id, machine_id, confidence=0.8, branch=None,
                     risks=None, dissent=None, strengths=None):
    return {
        "task_id": task_id,
        "machine_id": machine_id,
        "branch": branch or f"{machine_id}/feat-rebuttal",
        "summary": f"{machine_id} completed local surgery",
        "confidence": confidence,
        "files_touched": ["multifleet/cross_rebuttal.py"],
        "risks": risks or [],
        "dissent": dissent or [],
        "strengths": strengths or [],
        "questions_for_chief": [],
    }


def _critique_payload(task_id, from_machine, to_machine, target_branch,
                      confidence=0.75, blocking=None, action="merge"):
    return {
        "task_id": task_id,
        "from_machine": from_machine,
        "to_machine": to_machine,
        "target_branch": target_branch,
        "target_commit": "abc123",
        "summary": f"{from_machine} reviewed {to_machine}'s branch",
        "confidence": confidence,
        "blocking_concerns": blocking or [],
        "suggested_merge_action": action,
    }


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def registry():
    return PacketRegistry()


@pytest.fixture
def mgr():
    return RebuttalManager()


@pytest.fixture
def engine(tmp_path):
    return ChiefSynthesis(ledger_path=str(tmp_path / "decisions.jsonl"))


@pytest.fixture
def task_brief():
    return _task_brief_payload()


@pytest.fixture
def session(mgr, task_brief):
    return mgr.start_session(task_brief)


# =============================================================================
# Phase 1: Chief assigns task
# =============================================================================


class TestPhase1ChiefAssigns:
    def test_phase1_task_brief_creation(self, registry):
        """Chief creates a task_brief packet, validates against schema."""
        payload = _task_brief_payload()
        errors = registry.validate("task_brief", payload)
        assert errors == [], f"task_brief validation errors: {errors}"

    def test_phase1_task_brief_envelope(self, registry):
        """task_brief wrapped in envelope has correct structure."""
        payload = _task_brief_payload()
        env = create_envelope(
            from_node="mac1", to="all", type="task_brief", payload=payload,
        )
        assert env["type"] == "task_brief"
        assert env["from_node"] == "mac1"
        assert env["to_node"] == "all"
        assert env["payload"]["objective"] == payload["objective"]
        assert "id" in env
        assert "timestamp" in env

    def test_phase1_rebuttal_session_starts(self, mgr, task_brief):
        """RebuttalManager.start_session() creates session in ASSIGNED phase."""
        session = mgr.start_session(task_brief)
        assert session.phase == RebuttalPhase.ASSIGNED
        assert session.task_id == "task-e2e-001"
        assert session.expected_machines == MACHINES
        assert session.deadline_minutes == 30
        assert mgr.get_session("task-e2e-001") is session

    def test_phase1_advance_to_local_surgery(self, mgr, session):
        """Session advances from ASSIGNED to LOCAL_SURGERY."""
        ok = mgr.advance_session(session.task_id)
        assert ok is True
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

    def test_phase1_cannot_skip_to_verdicts(self, session):
        """Cannot jump directly from ASSIGNED to VERDICTS_COLLECTED."""
        ok = session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        assert ok is False
        assert session.phase == RebuttalPhase.ASSIGNED


# =============================================================================
# Phase 2: Local surgery produces verdicts
# =============================================================================


class TestPhase2LocalSurgery:
    def test_phase2_local_verdict_schema_valid(self, registry):
        """Each machine produces a local_verdict that validates."""
        for m in MACHINES:
            payload = _verdict_payload("task-e2e-001", m)
            errors = registry.validate("local_verdict", payload)
            assert errors == [], f"{m} verdict validation errors: {errors}"

    def test_phase2_verdict_missing_confidence_rejected(self, registry):
        """local_verdict missing 'confidence' is rejected by registry."""
        payload = _verdict_payload("task-e2e-001", "mac1")
        del payload["confidence"]
        errors = registry.validate("local_verdict", payload)
        assert len(errors) > 0
        assert any("confidence" in e for e in errors)

    def test_phase2_verdict_collection(self, session):
        """Session collects verdicts from 3 machines."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        assert len(session.verdicts) == 3
        for m in MACHINES:
            assert m in session.verdicts

    def test_phase2_auto_advance_on_all_verdicts(self, mgr, session):
        """When all 3 verdicts received, all_verdicts_in() returns True
        and session can advance to VERDICTS_COLLECTED."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        assert session.all_verdicts_in() is True
        ok = session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        assert ok is True
        assert session.phase == RebuttalPhase.VERDICTS_COLLECTED

    def test_phase2_partial_verdicts_not_ready(self, session):
        """all_verdicts_in() is False when only 2 of 3 verdicts received."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        session.add_verdict("mac1", _verdict_payload("task-e2e-001", "mac1"))
        session.add_verdict("mac2", _verdict_payload("task-e2e-001", "mac2"))
        assert session.all_verdicts_in() is False

    def test_phase2_verdict_with_dissent(self, session):
        """Verdict with neurologist dissent is preserved."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        dissent = [
            {"surgeon": "neurologist", "claim": "Race condition in state machine", "severity": "high"},
        ]
        verdict = _verdict_payload("task-e2e-001", "mac2", dissent=dissent)
        session.add_verdict("mac2", verdict)
        assert session.verdicts["mac2"]["dissent"] == dissent
        assert session.verdicts["mac2"]["dissent"][0]["surgeon"] == "neurologist"

    def test_phase2_verdict_with_risks(self, session):
        """Verdict with risks is stored correctly."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        verdict = _verdict_payload("task-e2e-001", "mac1",
                                   risks=["memory leak", "thread safety"])
        session.add_verdict("mac1", verdict)
        assert len(session.verdicts["mac1"]["risks"]) == 2


# =============================================================================
# Phase 3: Cross-machine critique
# =============================================================================


class TestPhase3CrossCritique:
    def test_phase3_critique_schema_valid(self, registry):
        """Critique packet validates against schema."""
        payload = _critique_payload(
            "task-e2e-001", "mac1", "mac2", "mac2/feat-rebuttal",
        )
        errors = registry.validate("critique", payload)
        assert errors == [], f"critique validation errors: {errors}"

    def test_phase3_pairwise_critique(self, mgr, session):
        """Each machine critiques one other: mac1->mac2, mac2->mac3, mac3->mac1."""
        # Advance to UNDER_REVIEW
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        session.advance(RebuttalPhase.UNDER_REVIEW)

        for from_m, to_m in CRITIQUE_PAIRS:
            critique = _critique_payload(
                "task-e2e-001", from_m, to_m,
                f"{to_m}/feat-rebuttal",
            )
            session.add_critique(from_m, critique)

        assert len(session.critiques) == 3
        # Verify pairwise assignments are recorded
        assert session.critiques["mac1"]["to_machine"] == "mac2"
        assert session.critiques["mac2"]["to_machine"] == "mac3"
        assert session.critiques["mac3"]["to_machine"] == "mac1"

    def test_phase3_critique_with_blocking_concerns(self, registry):
        """Critique with blocking concerns flags for revision."""
        payload = _critique_payload(
            "task-e2e-001", "mac1", "mac2", "mac2/feat-rebuttal",
            blocking=["Deadlock risk in concurrent access"],
            action="revise",
        )
        errors = registry.validate("critique", payload)
        assert errors == []
        assert len(payload["blocking_concerns"]) == 1
        assert payload["suggested_merge_action"] == "revise"

    def test_phase3_critique_enum_validated(self, registry):
        """Critique with invalid suggested_merge_action is rejected."""
        payload = _critique_payload(
            "task-e2e-001", "mac1", "mac2", "mac2/feat-rebuttal",
            action="invalid_action",
        )
        errors = registry.validate("critique", payload)
        assert len(errors) > 0
        assert any("suggested_merge_action" in e for e in errors)

    def test_phase3_all_critiques_auto_advance(self, session):
        """When all critiques received, advance to CRITIQUES_COLLECTED."""
        # Walk to UNDER_REVIEW
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        session.advance(RebuttalPhase.UNDER_REVIEW)

        for from_m, to_m in CRITIQUE_PAIRS:
            session.add_critique(from_m, _critique_payload(
                "task-e2e-001", from_m, to_m, f"{to_m}/feat-rebuttal",
            ))
        assert session.all_critiques_in() is True
        ok = session.advance(RebuttalPhase.CRITIQUES_COLLECTED)
        assert ok is True
        assert session.phase == RebuttalPhase.CRITIQUES_COLLECTED

    def test_phase3_partial_critiques_not_ready(self, session):
        """all_critiques_in() is False with partial critiques."""
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        session.advance(RebuttalPhase.UNDER_REVIEW)

        session.add_critique("mac1", _critique_payload(
            "task-e2e-001", "mac1", "mac2", "mac2/feat-rebuttal",
        ))
        assert session.all_critiques_in() is False


# =============================================================================
# Phase 4: Chief synthesis
# =============================================================================


class TestPhase4ChiefSynthesis:
    def test_phase4_synthesis_produces_decision(self, engine):
        """ChiefSynthesis.synthesize() produces a SynthesisResult."""
        verdicts = {
            "mac1": _verdict_payload("task-e2e-001", "mac1", confidence=0.9),
            "mac2": _verdict_payload("task-e2e-001", "mac2", confidence=0.7),
            "mac3": _verdict_payload("task-e2e-001", "mac3", confidence=0.6),
        }
        result = engine.synthesize("task-e2e-001", verdicts)
        assert isinstance(result, SynthesisResult)
        assert result.task_id == "task-e2e-001"
        assert result.decision in ("merge", "cherry_pick", "reconciliation",
                                   "reject", "needs_rebuttal")
        assert result.winner_branch is not None
        assert 0.0 <= result.confidence <= 1.0

    def test_phase4_chief_decision_schema_valid(self, engine, registry):
        """chief_decision packet validates against schema."""
        verdicts = {
            "mac1": _verdict_payload("task-e2e-001", "mac1", confidence=0.85),
        }
        result = engine.synthesize("task-e2e-001", verdicts)
        decision_payload = result.to_chief_decision()
        errors = registry.validate("chief_decision", decision_payload)
        assert errors == [], f"chief_decision validation errors: {errors}"

    def test_phase4_dissent_preserved_in_decision(self, engine):
        """All dissent from all machines appears in final decision."""
        verdicts = {
            "mac1": _verdict_payload("task-e2e-001", "mac1", confidence=0.9,
                                     dissent=[{"surgeon": "neurologist",
                                               "claim": "threading risk",
                                               "severity": "high"}]),
            "mac2": _verdict_payload("task-e2e-001", "mac2", confidence=0.7,
                                     dissent=[{"surgeon": "cardiologist",
                                               "claim": "perf regression",
                                               "severity": "medium"}]),
            "mac3": _verdict_payload("task-e2e-001", "mac3", confidence=0.6),
        }
        result = engine.synthesize("task-e2e-001", verdicts)
        assert len(result.dissent_preserved) == 2
        claims = {d["claim"] for d in result.dissent_preserved}
        assert "threading risk" in claims
        assert "perf regression" in claims

    def test_phase4_cherry_pick_from_non_winners(self, engine):
        """Non-winning branches contribute cherry-pick suggestions."""
        verdicts = {
            "mac1": _verdict_payload("task-e2e-001", "mac1", confidence=0.9,
                                     branch="mac1/feat-rebuttal"),
            "mac2": _verdict_payload("task-e2e-001", "mac2", confidence=0.5,
                                     branch="mac2/feat-rebuttal",
                                     strengths=["better error handling"]),
            "mac3": _verdict_payload("task-e2e-001", "mac3", confidence=0.4,
                                     branch="mac3/feat-rebuttal",
                                     strengths=["improved logging"]),
        }
        result = engine.synthesize("task-e2e-001", verdicts)
        assert result.winner_branch == "mac1/feat-rebuttal"
        assert len(result.cherry_pick_from) == 2
        machines = {cp["machine"] for cp in result.cherry_pick_from}
        assert "mac2" in machines
        assert "mac3" in machines
        reasons = {cp["reason"] for cp in result.cherry_pick_from}
        assert "better error handling" in reasons
        assert "improved logging" in reasons

    def test_phase4_session_resolves(self, mgr, session, engine):
        """Session advances to RESOLVED after chief_decision."""
        # Walk to SYNTHESIS
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-e2e-001", m))
        session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        session.advance(RebuttalPhase.UNDER_REVIEW)
        for from_m, to_m in CRITIQUE_PAIRS:
            session.add_critique(from_m, _critique_payload(
                "task-e2e-001", from_m, to_m, f"{to_m}/feat-rebuttal",
            ))
        session.advance(RebuttalPhase.CRITIQUES_COLLECTED)
        session.advance(RebuttalPhase.SYNTHESIS)

        # Synthesize and set decision
        result = engine.synthesize(session.task_id, session.verdicts)
        session.set_decision(result.to_chief_decision())
        assert session.chief_decision is not None
        assert session.chief_decision["decision"] in (
            "merge", "cherry_pick", "reconciliation", "reject", "needs_rebuttal",
        )

        # Resolve
        ok = session.advance(RebuttalPhase.RESOLVED)
        assert ok is True
        assert session.phase == RebuttalPhase.RESOLVED


# =============================================================================
# Full lifecycle
# =============================================================================


class TestFullLifecycle:
    def test_full_lifecycle_3_machines(self, mgr, engine, registry):
        """Complete flow: task_brief -> 3 verdicts -> 3 critiques -> chief_decision."""
        task_id = "task-full-001"
        brief = _task_brief_payload(task_id)

        # Validate task_brief schema
        assert registry.validate("task_brief", brief) == []

        # Phase 1: Chief assigns
        session = mgr.start_session(brief)
        assert session.phase == RebuttalPhase.ASSIGNED
        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

        # Phase 2: Each machine submits a verdict with varying confidence/dissent
        verdicts_data = {
            "mac1": _verdict_payload(task_id, "mac1", confidence=0.9,
                                     branch="mac1/feat-impl",
                                     strengths=["clean architecture"]),
            "mac2": _verdict_payload(task_id, "mac2", confidence=0.75,
                                     branch="mac2/feat-impl",
                                     dissent=[{"surgeon": "neurologist",
                                               "claim": "coupling concern",
                                               "severity": "medium"}],
                                     strengths=["better tests"]),
            "mac3": _verdict_payload(task_id, "mac3", confidence=0.6,
                                     branch="mac3/feat-impl",
                                     risks=["tight deadline"],
                                     strengths=["improved logging"]),
        }
        for m, v in verdicts_data.items():
            assert registry.validate("local_verdict", v) == []
            session.add_verdict(m, v)
        assert session.all_verdicts_in() is True

        # Advance through verdict collection
        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.VERDICTS_COLLECTED

        # Phase 3: Cross-machine critique (pairwise)
        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.UNDER_REVIEW

        for from_m, to_m in CRITIQUE_PAIRS:
            critique = _critique_payload(
                task_id, from_m, to_m,
                f"{to_m}/feat-impl",
            )
            assert registry.validate("critique", critique) == []
            session.add_critique(from_m, critique)
        assert session.all_critiques_in() is True

        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.CRITIQUES_COLLECTED

        # Phase 4: Chief synthesis
        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.SYNTHESIS

        result = engine.synthesize(task_id, session.verdicts)
        assert isinstance(result, SynthesisResult)

        decision_payload = result.to_chief_decision()
        assert registry.validate("chief_decision", decision_payload) == []

        session.set_decision(decision_payload)
        mgr.advance_session(task_id)
        assert session.phase == RebuttalPhase.RESOLVED

        # Verify final state
        assert result.winner_branch == "mac1/feat-impl"  # highest confidence
        assert len(result.dissent_preserved) == 1  # mac2's neurologist dissent
        assert result.dissent_preserved[0]["claim"] == "coupling concern"
        assert len(result.cherry_pick_from) == 2  # mac2 + mac3 strengths
        assert result.decision == "cherry_pick"  # because cherry_picks exist

    def test_full_lifecycle_with_deadline_expiry(self, mgr):
        """Session expires when deadline passes."""
        brief = _task_brief_payload("task-expire-001")
        brief["deadline_minutes"] = 1
        session = mgr.start_session(brief)
        session.advance(RebuttalPhase.LOCAL_SURGERY)

        # Not expired yet
        assert session.is_expired() is False
        assert mgr.cleanup_expired() == 0

        # Simulate passage of time past deadline
        session.created_at = time.time() - 120  # 2 min ago, deadline 1 min
        assert session.is_expired() is True
        removed = mgr.cleanup_expired()
        assert removed == 1
        assert mgr.get_session("task-expire-001") is None

    def test_full_lifecycle_2_machines_partial(self, mgr, engine):
        """Works with 2 machines (one offline) -- doesn't block forever."""
        brief = _task_brief_payload("task-partial-001", machines=["mac1", "mac2"])
        session = mgr.start_session(brief)
        assert session.expected_machines == ["mac1", "mac2"]

        mgr.advance_session("task-partial-001")
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

        # Only 2 machines needed
        session.add_verdict("mac1", _verdict_payload("task-partial-001", "mac1", confidence=0.85))
        session.add_verdict("mac2", _verdict_payload("task-partial-001", "mac2", confidence=0.7))
        assert session.all_verdicts_in() is True

        session.advance(RebuttalPhase.VERDICTS_COLLECTED)
        session.advance(RebuttalPhase.UNDER_REVIEW)

        session.add_critique("mac1", _critique_payload(
            "task-partial-001", "mac1", "mac2", "mac2/feat-rebuttal",
        ))
        session.add_critique("mac2", _critique_payload(
            "task-partial-001", "mac2", "mac1", "mac1/feat-rebuttal",
        ))
        assert session.all_critiques_in() is True

        session.advance(RebuttalPhase.CRITIQUES_COLLECTED)
        session.advance(RebuttalPhase.SYNTHESIS)

        result = engine.synthesize("task-partial-001", session.verdicts)
        assert result.winner_branch is not None
        session.set_decision(result.to_chief_decision())
        session.advance(RebuttalPhase.RESOLVED)
        assert session.phase == RebuttalPhase.RESOLVED

    def test_full_lifecycle_ledger_recorded(self, mgr, engine, tmp_path):
        """Decision ledger records the synthesis result."""
        ledger = tmp_path / "ledger.jsonl"
        engine_with_ledger = ChiefSynthesis(ledger_path=str(ledger))

        brief = _task_brief_payload("task-ledger-001")
        session = mgr.start_session(brief)
        mgr.advance_session("task-ledger-001")
        for m in MACHINES:
            session.add_verdict(m, _verdict_payload("task-ledger-001", m))

        result = engine_with_ledger.synthesize("task-ledger-001", session.verdicts)
        assert ledger.exists()
        entry = json.loads(ledger.read_text().strip())
        assert entry["task_id"] == "task-ledger-001"
        assert "confidence" in entry
        assert "duration_ms" in entry


# =============================================================================
# Packet registry integration
# =============================================================================


class TestPacketRegistryIntegration:
    def test_all_builtin_packets_auto_loaded(self, registry):
        """PacketRegistry.load_builtins() loads all 16 builtin schemas."""
        # The 4 original + 6 vision + 6 more = 16 total builtin .json files
        all_types = registry.list_types()
        assert len(all_types) >= 16, (
            f"Expected >= 16 builtin types, got {len(all_types)}: "
            f"{[t['type_name'] for t in all_types]}"
        )

    def test_vision_packets_registered(self, registry):
        """The 6 vision packet types are registered."""
        vision_types = ["task_brief", "local_verdict", "critique",
                        "chief_decision", "merge_candidate", "branch_status"]
        for vt in vision_types:
            assert registry.is_registered(vt), f"{vt} not registered"

    def test_verdict_rejected_without_required_fields(self, registry):
        """local_verdict missing 'confidence' is rejected by registry."""
        payload = {
            "task_id": "t1",
            "machine_id": "mac1",
            "branch": "mac1/feat",
            "summary": "done",
            # missing: confidence, files_touched, risks, dissent, questions_for_chief
        }
        errors = registry.validate("local_verdict", payload)
        assert len(errors) > 0

    def test_verdict_rejected_confidence_out_of_range(self, registry):
        """local_verdict with confidence > 1.0 is rejected."""
        payload = _verdict_payload("t1", "mac1", confidence=1.5)
        errors = registry.validate("local_verdict", payload)
        assert len(errors) > 0

    def test_critique_enum_validated(self, registry):
        """Critique with invalid suggested_merge_action is rejected."""
        payload = _critique_payload(
            "t1", "mac1", "mac2", "mac2/feat",
            action="nuke_from_orbit",
        )
        errors = registry.validate("critique", payload)
        assert len(errors) > 0
        assert any("suggested_merge_action" in e for e in errors)

    def test_task_brief_missing_objective_rejected(self, registry):
        """task_brief missing 'objective' is rejected."""
        payload = {"task_id": "t1", "deadline_minutes": 30}
        errors = registry.validate("task_brief", payload)
        assert len(errors) > 0
        assert any("objective" in e for e in errors)

    def test_create_envelope_roundtrip(self, registry):
        """Envelope wrapping preserves payload and metadata."""
        payload = _task_brief_payload()
        env = create_envelope(
            from_node="mac1", to="all", type="task_brief", payload=payload,
        )
        assert env["payload"] == payload
        assert env["type"] == "task_brief"
        # Re-validate the payload extracted from envelope
        errors = registry.validate("task_brief", env["payload"])
        assert errors == []
