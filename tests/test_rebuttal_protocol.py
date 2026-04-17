"""Tests for RebuttalProtocol — cross-machine preserved dissent."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.rebuttal_protocol import (
    DEFAULT_REBUTTAL_WINDOW_S,
    Proposal,
    ProposalState,
    Rebuttal,
    RebuttalProtocol,
)


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def evidence_ledger():
    mock = MagicMock()
    mock.record = MagicMock()
    return mock


@pytest.fixture
def corrigibility_ledger():
    mock = MagicMock()
    mock.record_decision_point = MagicMock(return_value=1)
    return mock


@pytest.fixture
def nats_messages():
    """Captures NATS publish calls as (subject, payload) tuples."""
    messages = []

    def publish(subject, payload):
        messages.append((subject, payload))

    return messages, publish


@pytest.fixture
def proto(evidence_ledger, corrigibility_ledger, nats_messages):
    _, publish = nats_messages
    return RebuttalProtocol(
        node_id="mac1",
        evidence_ledger=evidence_ledger,
        corrigibility_ledger=corrigibility_ledger,
        nats_publish=publish,
        rebuttal_window_s=60,
    )


@pytest.fixture
def proto_mac2(evidence_ledger, nats_messages):
    _, publish = nats_messages
    return RebuttalProtocol(
        node_id="mac2",
        evidence_ledger=evidence_ledger,
        nats_publish=publish,
        rebuttal_window_s=60,
    )


# ── Proposal lifecycle ───────────────────────────────────────────────────


class TestPropose:
    def test_creates_proposal(self, proto):
        p = proto.propose("task-001", "merge", ["ev-1", "ev-2"], reasoning="tests pass")
        assert p.task_id == "task-001"
        assert p.decision == "merge"
        assert p.state == ProposalState.OPEN
        assert p.proposer == "mac1"
        assert p.evidence_refs == ["ev-1", "ev-2"]

    def test_records_to_evidence_ledger(self, proto, evidence_ledger):
        proto.propose("task-002", "merge", ["ev-1"])
        evidence_ledger.record.assert_called_once()
        call_kwargs = evidence_ledger.record.call_args
        assert call_kwargs[1]["event_type"] == "rebuttal_proposal"

    def test_publishes_to_nats(self, proto, nats_messages):
        messages, _ = nats_messages
        proto.propose("task-003", "merge", ["ev-1"])
        assert len(messages) == 1
        subject, payload = messages[0]
        assert subject == "fleet.all.rebuttal"
        assert payload["type"] == "rebuttal_proposal"
        assert payload["from"] == "mac1"

    def test_proposal_retrievable(self, proto):
        proto.propose("task-004", "reject", [])
        p = proto.get_proposal("task-004")
        assert p is not None
        assert p.decision == "reject"

    def test_proposal_dict(self, proto):
        proto.propose("task-005", "merge", ["ev-1"])
        d = proto.get_proposal_dict("task-005")
        assert d["task_id"] == "task-005"
        assert d["state"] == "open"


# ── Rebuttal recording ──────────────────────────────────────────────────


class TestRebut:
    def test_records_rebuttal(self, proto):
        proto.propose("task-010", "merge", ["ev-1"])
        # Simulate mac2 rebutting
        proto._node_id = "mac2"
        r = proto.rebut("task-010", ["ev-3"], "regression in test X", confidence=0.9)
        assert r is not None
        assert r.node_id == "mac2"
        assert r.reasoning == "regression in test X"
        assert r.confidence == 0.9

    def test_rebuttal_changes_state_to_rebuttal_period(self, proto):
        proto.propose("task-011", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("task-011", ["ev-3"], "found issue")
        p = proto.get_proposal("task-011")
        assert p.state == ProposalState.REBUTTAL_PERIOD

    def test_rebuttal_preserved_in_proposal(self, proto):
        proto.propose("task-012", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("task-012", ["ev-3"], "issue A")
        proto._node_id = "mac3"
        proto.rebut("task-012", ["ev-4", "ev-5"], "issue B", confidence=0.8)

        rebuttals = proto.get_rebuttals("task-012")
        assert len(rebuttals) == 2
        nodes = {r["node_id"] for r in rebuttals}
        assert nodes == {"mac2", "mac3"}

    def test_cannot_rebut_nonexistent(self, proto):
        r = proto.rebut("fake-task", ["ev-1"], "nope")
        assert r is None

    def test_cannot_rebut_resolved(self, proto):
        proto.propose("task-013", "merge", ["ev-1"])
        proto.resolve("task-013")
        proto._node_id = "mac2"
        r = proto.rebut("task-013", ["ev-3"], "too late")
        assert r is None

    def test_cannot_rebut_after_window(self, proto):
        proto.propose("task-014", "merge", ["ev-1"], rebuttal_window_s=1)
        # Backdate proposal
        proto._proposals["task-014"].created_at = time.time() - 10
        proto._node_id = "mac2"
        r = proto.rebut("task-014", ["ev-3"], "too late")
        assert r is None

    def test_rebuttal_records_evidence(self, proto, evidence_ledger):
        proto.propose("task-015", "merge", ["ev-1"])
        evidence_ledger.record.reset_mock()
        proto._node_id = "mac2"
        proto.rebut("task-015", ["ev-3"], "issue")
        evidence_ledger.record.assert_called_once()
        call_kwargs = evidence_ledger.record.call_args
        assert call_kwargs[1]["event_type"] == "rebuttal_dissent"

    def test_rebuttal_publishes_nats(self, proto, nats_messages):
        messages, _ = nats_messages
        proto.propose("task-016", "merge", ["ev-1"])
        messages.clear()
        proto._node_id = "mac2"
        proto.rebut("task-016", ["ev-3"], "issue")
        assert len(messages) == 1
        assert messages[0][1]["type"] == "rebuttal_dissent"


# ── Resolution with preserved dissent ────────────────────────────────────


class TestResolve:
    def test_resolve_no_rebuttals(self, proto):
        proto.propose("task-020", "merge", ["ev-1"])
        result = proto.resolve("task-020")
        assert result is not None
        assert result["final_decision"] == "merge"
        assert result["decision_changed"] is False
        assert result["dissent_preserved"] == []
        assert result["rebuttal_count"] == 0

    def test_resolve_with_rebuttals_no_change(self, proto):
        proto.propose("task-021", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("task-021", ["ev-3"], "minor concern")
        proto._node_id = "mac1"
        result = proto.resolve("task-021", final_decision="merge")
        assert result["decision_changed"] is False
        assert len(result["dissent_preserved"]) == 1
        assert result["dissent_preserved"][0]["node"] == "mac2"

    def test_resolve_with_changed_decision(self, proto, corrigibility_ledger):
        proto.propose("task-022", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("task-022", ["ev-3", "ev-4"], "critical regression", confidence=0.95)
        proto._node_id = "mac1"
        result = proto.resolve("task-022", final_decision="reject",
                               final_reasoning="mac2 found regression")
        assert result["original_decision"] == "merge"
        assert result["final_decision"] == "reject"
        assert result["decision_changed"] is True
        assert len(result["dissent_preserved"]) == 1

        # Verify corrigibility ledger was written
        corrigibility_ledger.record_decision_point.assert_called_once()
        call_kwargs = corrigibility_ledger.record_decision_point.call_args
        assert call_kwargs[1]["proposed_action"] == "merge"
        assert call_kwargs[1]["final_action"] == "reject"
        assert call_kwargs[1]["changed"] is True

    def test_resolve_sets_state_to_resolved(self, proto):
        proto.propose("task-023", "merge", ["ev-1"])
        proto.resolve("task-023")
        p = proto.get_proposal("task-023")
        assert p.state == ProposalState.RESOLVED

    def test_resolve_nonexistent_returns_none(self, proto):
        assert proto.resolve("fake") is None

    def test_resolve_publishes_nats(self, proto, nats_messages):
        messages, _ = nats_messages
        proto.propose("task-024", "merge", ["ev-1"])
        messages.clear()
        proto.resolve("task-024")
        assert len(messages) == 1
        assert messages[0][1]["type"] == "rebuttal_resolution"

    def test_dissent_preserved_includes_evidence(self, proto):
        """Core Truth #1: dissent includes counter-evidence refs."""
        proto.propose("task-025", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("task-025", ["ev-10", "ev-11"], "perf regression")
        proto._node_id = "mac3"
        proto.rebut("task-025", ["ev-12"], "test failure")
        proto._node_id = "mac1"
        result = proto.resolve("task-025", final_decision="cherry_pick")

        dissent = result["dissent_preserved"]
        assert len(dissent) == 2
        mac2_dissent = next(d for d in dissent if d["node"] == "mac2")
        assert mac2_dissent["counter_evidence"] == ["ev-10", "ev-11"]
        mac3_dissent = next(d for d in dissent if d["node"] == "mac3")
        assert mac3_dissent["counter_evidence"] == ["ev-12"]


# ── Incoming message handling ────────────────────────────────────────────


class TestHandleIncoming:
    def test_ignores_own_messages(self, proto):
        result = proto.handle_incoming({
            "type": "rebuttal_proposal",
            "from": "mac1",
            "task_id": "t1",
        })
        assert result["action"] == "ignored"

    def test_ingests_remote_proposal(self, proto):
        result = proto.handle_incoming({
            "type": "rebuttal_proposal",
            "from": "mac2",
            "task_id": "task-030",
            "proposal": {
                "task_id": "task-030",
                "proposer": "mac2",
                "decision": "merge",
                "evidence_refs": ["ev-1"],
                "reasoning": "all green",
                "confidence": 0.9,
                "state": "open",
                "created_at": time.time(),
                "rebuttal_window_s": 60,
            },
        })
        assert result["action"] == "proposal_ingested"
        assert proto.get_proposal("task-030") is not None

    def test_ingests_remote_rebuttal(self, proto):
        # First ingest a proposal
        proto.handle_incoming({
            "type": "rebuttal_proposal",
            "from": "mac2",
            "task_id": "task-031",
            "proposal": {
                "task_id": "task-031",
                "proposer": "mac2",
                "decision": "merge",
                "evidence_refs": [],
                "confidence": 0.8,
                "state": "open",
                "created_at": time.time(),
                "rebuttal_window_s": 60,
            },
        })
        # Then ingest a rebuttal
        result = proto.handle_incoming({
            "type": "rebuttal_dissent",
            "from": "mac3",
            "task_id": "task-031",
            "rebuttal": {
                "task_id": "task-031",
                "node_id": "mac3",
                "counter_evidence": ["ev-5"],
                "reasoning": "found bug",
                "confidence": 0.85,
                "timestamp": time.time(),
            },
        })
        assert result["action"] == "rebuttal_ingested"
        assert result["total_rebuttals"] == 1

    def test_ingests_remote_resolution(self, proto):
        proto.handle_incoming({
            "type": "rebuttal_proposal",
            "from": "mac2",
            "task_id": "task-032",
            "proposal": {
                "task_id": "task-032",
                "proposer": "mac2",
                "decision": "merge",
                "evidence_refs": [],
                "confidence": 0.8,
                "state": "open",
                "created_at": time.time(),
                "rebuttal_window_s": 60,
            },
        })
        result = proto.handle_incoming({
            "type": "rebuttal_resolution",
            "from": "mac2",
            "task_id": "task-032",
            "resolution": {
                "task_id": "task-032",
                "original_decision": "merge",
                "final_decision": "reject",
                "decision_changed": True,
                "proposer": "mac2",
                "dissent_preserved": [{"node": "mac3", "reasoning": "bug"}],
                "resolved_at": time.time(),
            },
        })
        assert result["action"] == "resolution_ingested"
        assert result["decision_changed"] is True
        p = proto.get_proposal("task-032")
        assert p.state == ProposalState.RESOLVED

    def test_resolution_without_prior_proposal(self, proto):
        """Resolution can arrive before proposal (network ordering)."""
        result = proto.handle_incoming({
            "type": "rebuttal_resolution",
            "from": "mac2",
            "task_id": "task-033",
            "resolution": {
                "task_id": "task-033",
                "original_decision": "merge",
                "final_decision": "merge",
                "decision_changed": False,
                "proposer": "mac2",
                "dissent_preserved": [],
                "resolved_at": time.time(),
            },
        })
        assert result["action"] == "resolution_ingested"

    def test_unknown_type_returns_error(self, proto):
        result = proto.handle_incoming({"type": "unknown", "from": "mac2"})
        assert result["action"] == "error"


# ── Auto-expiry ──────────────────────────────────────────────────────────


class TestExpireStale:
    def test_auto_accepts_no_rebuttals(self, proto):
        proto.propose("task-040", "merge", ["ev-1"], rebuttal_window_s=1)
        proto._proposals["task-040"].created_at = time.time() - 10
        count = proto.expire_stale()
        assert count == 1
        p = proto.get_proposal("task-040")
        assert p.state == ProposalState.RESOLVED
        assert p.resolution["final_decision"] == "merge"

    def test_does_not_auto_accept_with_rebuttals(self, proto):
        proto.propose("task-041", "merge", ["ev-1"], rebuttal_window_s=1)
        proto._node_id = "mac2"
        # Manually add rebuttal bypassing window check
        r = Rebuttal(
            rebuttal_id="r-1", task_id="task-041", node_id="mac2",
            counter_evidence=["ev-3"], reasoning="issue",
            confidence=0.8, timestamp=time.time(),
        )
        proto._proposals["task-041"].rebuttals["mac2"] = r
        proto._proposals["task-041"].created_at = time.time() - 10
        proto._node_id = "mac1"
        count = proto.expire_stale()
        assert count == 0
        p = proto.get_proposal("task-041")
        assert p.state == ProposalState.RESOLVING

    def test_skips_already_resolved(self, proto):
        proto.propose("task-042", "merge", ["ev-1"])
        proto.resolve("task-042")
        count = proto.expire_stale()
        assert count == 0

    def test_skips_open_window(self, proto):
        proto.propose("task-043", "merge", ["ev-1"], rebuttal_window_s=3600)
        count = proto.expire_stale()
        assert count == 0


# ── Stats ────────────────────────────────────────────────────────────────


class TestStats:
    def test_empty_stats(self, proto):
        s = proto.stats
        assert s["total_proposals"] == 0
        assert s["active"] == 0

    def test_stats_after_proposals(self, proto):
        proto.propose("t1", "merge", [])
        proto.propose("t2", "reject", [])
        proto.resolve("t2")
        s = proto.stats
        assert s["total_proposals"] == 2
        assert s["active"] == 1
        assert s["by_state"]["open"] == 1
        assert s["by_state"]["resolved"] == 1

    def test_stats_tracks_dissent(self, proto):
        proto.propose("t3", "merge", [])
        proto._node_id = "mac2"
        proto.rebut("t3", ["ev-1"], "concern")
        proto._node_id = "mac1"
        s = proto.stats
        assert s["with_dissent"] == 1

    def test_stats_tracks_changed_decisions(self, proto):
        proto.propose("t4", "merge", ["ev-1"])
        proto._node_id = "mac2"
        proto.rebut("t4", ["ev-3"], "critical bug")
        proto._node_id = "mac1"
        proto.resolve("t4", final_decision="reject")
        s = proto.stats
        assert s["decisions_changed_by_rebuttal"] == 1


# ── Serialization roundtrip ──────────────────────────────────────────────


class TestSerialization:
    def test_proposal_roundtrip(self):
        p = Proposal(
            task_id="t1", proposer="mac1", decision="merge",
            evidence_refs=["ev-1"], reasoning="ok", confidence=0.9,
            state=ProposalState.REBUTTAL_PERIOD,
            created_at=1000.0, rebuttal_window_s=60,
        )
        r = Rebuttal(
            rebuttal_id="r1", task_id="t1", node_id="mac2",
            counter_evidence=["ev-3"], reasoning="no",
            confidence=0.8, timestamp=1001.0,
        )
        p.rebuttals["mac2"] = r

        d = p.to_dict()
        restored = Proposal.from_dict(d)
        assert restored.task_id == "t1"
        assert restored.state == ProposalState.REBUTTAL_PERIOD
        assert "mac2" in restored.rebuttals
        assert restored.rebuttals["mac2"].reasoning == "no"

    def test_rebuttal_roundtrip(self):
        r = Rebuttal(
            rebuttal_id="r1", task_id="t1", node_id="mac3",
            counter_evidence=["ev-5", "ev-6"], reasoning="perf issue",
            confidence=0.75, timestamp=2000.0,
            metadata={"probe": "latency_check"},
        )
        d = r.to_dict()
        restored = Rebuttal.from_dict(d)
        assert restored.counter_evidence == ["ev-5", "ev-6"]
        assert restored.metadata == {"probe": "latency_check"}


# ── Multi-node simulation ───────────────────────────────────────────────


class TestMultiNodeScenario:
    """Simulates a full 3-machine rebuttal flow."""

    def test_full_3_machine_flow(self):
        """mac1 proposes, mac2 rebuts, mac3 rebuts, chief resolves with dissent."""
        messages = []

        def broadcast(subject, payload):
            messages.append((subject, payload))

        evidence = MagicMock()
        corrigibility = MagicMock()
        corrigibility.record_decision_point = MagicMock(return_value=1)

        # Each machine has its own protocol instance
        mac1 = RebuttalProtocol("mac1", evidence, corrigibility, broadcast, 120)
        mac2 = RebuttalProtocol("mac2", evidence, nats_publish=broadcast, rebuttal_window_s=120)
        mac3 = RebuttalProtocol("mac3", evidence, nats_publish=broadcast, rebuttal_window_s=120)

        # mac1 proposes merge
        mac1.propose("race-001", "merge", ["ev-1", "ev-2"],
                      reasoning="mac1 branch passes all tests", confidence=0.85)

        # Simulate NATS delivery: mac2 and mac3 receive the proposal
        proposal_msg = messages[0][1]
        mac2.handle_incoming(proposal_msg)
        mac3.handle_incoming(proposal_msg)

        # mac2 rebuts with evidence
        mac2.rebut("race-001", ["ev-10", "ev-11"],
                    "latency regression: p99 went from 50ms to 200ms",
                    confidence=0.9)

        # mac3 rebuts with different evidence
        mac3.rebut("race-001", ["ev-20"],
                    "missing test coverage for error path",
                    confidence=0.6)

        # Simulate NATS delivery of rebuttals to mac1
        for subj, msg in messages:
            if msg.get("type") == "rebuttal_dissent" and msg.get("from") != "mac1":
                mac1.handle_incoming(msg)

        # Verify mac1 sees both rebuttals
        rebuttals = mac1.get_rebuttals("race-001")
        assert len(rebuttals) == 2

        # Chief (mac1) resolves — changes decision based on rebuttals
        result = mac1.resolve(
            "race-001",
            final_decision="cherry_pick",
            final_reasoning="Accept core feature but exclude perf regression commits",
        )

        # Verify preserved dissent
        assert result["decision_changed"] is True
        assert result["original_decision"] == "merge"
        assert result["final_decision"] == "cherry_pick"
        assert len(result["dissent_preserved"]) == 2

        # Verify corrigibility was recorded
        corrigibility.record_decision_point.assert_called_once()

        # Verify evidence ledger got all entries:
        # 1 proposal + 2 rebuttals (from mac2, mac3) + 1 resolution
        # (mac1's protocol records proposal + resolution = 2)
        # mac2 records its rebuttal = 1, mac3 records its rebuttal = 1
        # Total evidence.record calls = 4
        assert evidence.record.call_count == 4
