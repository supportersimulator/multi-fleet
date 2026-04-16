"""Tests for Chief Election Protocol."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.chief_election import (
    ChiefElection,
    ElectionProposal,
    ElectionState,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def election():
    return ChiefElection(node_id="mac1", fleet_size=3)


def _health(status="healthy", uptime_s=1000, score=5, total=7, **extra):
    h = {"status": status, "uptime_s": uptime_s, "score": score, "total": total}
    h.update(extra)
    return h


# ── elect() tests ─────────────────────────────────────────────────────────────


class TestElect:
    def test_picks_highest_uptime(self, election):
        health = {
            "mac1": _health(uptime_s=500),
            "mac2": _health(uptime_s=1000),
            "mac3": _health(uptime_s=800),
        }
        assert election.elect(["mac1", "mac2", "mac3"], health) == "mac2"

    def test_filters_offline_candidates(self, election):
        health = {
            "mac1": _health(status="offline"),
            "mac2": _health(uptime_s=100),
            "mac3": _health(uptime_s=200),
        }
        assert election.elect(["mac1", "mac2", "mac3"], health) == "mac3"

    def test_prefers_healthy_over_degraded(self, election):
        health = {
            "mac1": _health(status="degraded", uptime_s=9999),
            "mac2": _health(status="healthy", uptime_s=100),
        }
        assert election.elect(["mac1", "mac2"], health) == "mac2"

    def test_channels_break_uptime_tie(self, election):
        health = {
            "mac1": _health(uptime_s=1000, score=3),
            "mac2": _health(uptime_s=1000, score=6),
        }
        assert election.elect(["mac1", "mac2"], health) == "mac2"

    def test_alphabetical_tiebreak(self, election):
        health = {
            "mac1": _health(uptime_s=1000, score=5),
            "mac2": _health(uptime_s=1000, score=5),
        }
        # Both identical -- alphabetical wins (mac1 < mac2)
        result = election.elect(["mac1", "mac2"], health)
        assert result == "mac1"

    def test_no_candidates_raises(self, election):
        with pytest.raises(ValueError, match="No candidates"):
            election.elect([], {})

    def test_all_offline_raises(self, election):
        health = {
            "mac1": _health(status="offline"),
            "mac2": _health(status="offline"),
        }
        with pytest.raises(ValueError, match="No healthy candidates"):
            election.elect(["mac1", "mac2"], health)

    def test_single_candidate(self, election):
        health = {"mac1": _health()}
        assert election.elect(["mac1"], health) == "mac1"

    def test_unknown_candidate_treated_as_healthy(self, election):
        """Candidate not in health dict gets default values (not offline)."""
        health = {}
        assert election.elect(["mac1"], health) == "mac1"


# ── check_chief_alive() tests ────────────────────────────────────────────────


class TestCheckChiefAlive:
    def test_alive_within_timeout(self, election):
        assert election.check_chief_alive("mac2", time.time() - 100, timeout=300)

    def test_dead_beyond_timeout(self, election):
        assert not election.check_chief_alive("mac2", time.time() - 400, timeout=300)

    def test_recently_seen_is_alive(self, election):
        # Seen 1 second ago with 5-second timeout -> alive
        assert election.check_chief_alive("mac2", time.time() - 1, timeout=5.0)

    def test_custom_timeout(self, election):
        assert election.check_chief_alive("mac2", time.time() - 50, timeout=60)
        assert not election.check_chief_alive("mac2", time.time() - 70, timeout=60)


# ── propose/vote/tally tests ─────────────────────────────────────────────────


class TestElectionProtocol:
    def test_propose_creates_voting_proposal(self, election):
        proposal = election.propose_election("chief down")
        assert proposal.state == ElectionState.VOTING
        assert proposal.reason == "chief down"
        assert proposal.proposed_by == "mac1"
        assert len(proposal.proposal_id) == 12

    def test_vote_records_choice(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        assert proposal.votes["mac1"] == "mac2"

    def test_duplicate_vote_raises(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        with pytest.raises(ValueError, match="already voted"):
            election.vote(proposal.proposal_id, candidate="mac3", voter="mac1")

    def test_vote_unknown_proposal_raises(self, election):
        with pytest.raises(KeyError, match="Unknown proposal"):
            election.vote("nonexistent", candidate="mac2", voter="mac1")

    def test_tally_no_quorum_returns_none(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        # 1 of 3 votes, need 2 for quorum
        assert election.tally(proposal.proposal_id) is None

    def test_tally_majority_wins(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac3")
        winner = election.tally(proposal.proposal_id)
        assert winner == "mac2"
        assert proposal.state == ElectionState.DECIDED
        assert proposal.decided_at is not None

    def test_tally_split_vote_no_winner(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac1", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac2")
        election.vote(proposal.proposal_id, candidate="mac3", voter="mac3")
        # 3-way split, nobody has majority (need 2)
        assert election.tally(proposal.proposal_id) is None

    def test_tally_sets_current_chief(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac3")
        election.tally(proposal.proposal_id)
        assert election.current_chief == "mac2"

    def test_tally_decided_returns_winner_again(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac3")
        assert election.tally(proposal.proposal_id) == "mac2"
        # Second tally returns same winner
        assert election.tally(proposal.proposal_id) == "mac2"

    def test_vote_on_decided_proposal_raises(self, election):
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac3")
        election.tally(proposal.proposal_id)
        with pytest.raises(ValueError, match="decided"):
            election.vote(proposal.proposal_id, candidate="mac1", voter="mac2")

    def test_tally_unknown_proposal_raises(self, election):
        with pytest.raises(KeyError):
            election.tally("nonexistent")


# ── Callback tests ────────────────────────────────────────────────────────────


class TestCallbacks:
    def test_on_new_chief_fires(self, election):
        results = []
        election.on_new_chief(lambda cid: results.append(cid))
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac2", voter="mac3")
        election.tally(proposal.proposal_id)
        assert results == ["mac2"]

    def test_callback_error_does_not_crash(self, election):
        def bad_cb(cid):
            raise RuntimeError("boom")

        election.on_new_chief(bad_cb)
        proposal = election.propose_election("test")
        election.vote(proposal.proposal_id, candidate="mac1", voter="mac1")
        election.vote(proposal.proposal_id, candidate="mac1", voter="mac3")
        # Should not raise
        assert election.tally(proposal.proposal_id) == "mac1"


# ── Proposal lifecycle tests ─────────────────────────────────────────────────


class TestProposalLifecycle:
    def test_active_proposals_filters_decided(self, election):
        p1 = election.propose_election("test1")
        p2 = election.propose_election("test2")
        election.vote(p1.proposal_id, candidate="mac1", voter="mac1")
        election.vote(p1.proposal_id, candidate="mac1", voter="mac3")
        election.tally(p1.proposal_id)
        active = election.active_proposals()
        assert len(active) == 1
        assert active[0].proposal_id == p2.proposal_id

    def test_stale_proposals_expire(self, election):
        election._proposal_ttl = 0.0  # instant expiry
        election.propose_election("stale")
        assert len(election.active_proposals()) == 0

    def test_get_proposal(self, election):
        p = election.propose_election("test")
        assert election.get_proposal(p.proposal_id) is p
        assert election.get_proposal("nonexistent") is None

    def test_to_dict(self, election):
        p = election.propose_election("test")
        d = p.to_dict()
        assert d["reason"] == "test"
        assert d["state"] == "voting"
        assert d["proposed_by"] == "mac1"


# ── Health monitor integration ────────────────────────────────────────────────


class TestHealthMonitorIntegration:
    def test_wire_triggers_election_on_stale_chief(self, election):
        election.current_chief = "mac2"

        monitor = MagicMock()
        monitor.check_fleet = MagicMock(return_value={
            "status": "ok",
            "nodes": {
                "mac1": {"status": "healthy", "uptime_s": 1000, "score": 5},
                "mac2": {"status": "offline", "last_seen_s": 999},
                "mac3": {"status": "healthy", "uptime_s": 500, "score": 4},
            },
        })

        election.wire_to_health_monitor(monitor)
        # Call the wrapped check_fleet
        monitor.check_fleet()

        # Should have created a proposal
        proposals = election.active_proposals()
        assert len(proposals) >= 1
        assert "mac2" in proposals[0].reason

    def test_wire_no_election_when_chief_healthy(self, election):
        election.current_chief = "mac1"

        monitor = MagicMock()
        monitor.check_fleet = MagicMock(return_value={
            "status": "ok",
            "nodes": {
                "mac1": {"status": "healthy", "last_seen_s": 10, "uptime_s": 1000},
                "mac2": {"status": "healthy", "last_seen_s": 5, "uptime_s": 500},
            },
        })

        election.wire_to_health_monitor(monitor)
        monitor.check_fleet()

        assert len(election.active_proposals()) == 0

    def test_wire_no_election_without_chief(self, election):
        """No election triggered if no chief is set."""
        monitor = MagicMock()
        monitor.check_fleet = MagicMock(return_value={
            "status": "ok",
            "nodes": {"mac1": {"status": "healthy"}},
        })
        election.wire_to_health_monitor(monitor)
        monitor.check_fleet()
        assert len(election.active_proposals()) == 0


# ── record_chief_heartbeat tests ─────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_updates_last_seen(self, election):
        election.current_chief = "mac2"
        old = election._chief_last_seen
        time.sleep(0.01)
        election.record_chief_heartbeat("mac2")
        assert election._chief_last_seen > old

    def test_heartbeat_wrong_chief_ignored(self, election):
        election.current_chief = "mac2"
        old = election._chief_last_seen
        election.record_chief_heartbeat("mac3")  # not the chief
        assert election._chief_last_seen == old


# ── Fleet size edge cases ────────────────────────────────────────────────────


class TestFleetSizes:
    def test_fleet_of_5_needs_3_votes(self):
        e = ChiefElection(node_id="n1", fleet_size=5)
        p = e.propose_election("test")
        e.vote(p.proposal_id, candidate="n1", voter="n1")
        e.vote(p.proposal_id, candidate="n1", voter="n2")
        assert e.tally(p.proposal_id) is None  # 2/5, need 3
        e.vote(p.proposal_id, candidate="n1", voter="n3")
        assert e.tally(p.proposal_id) == "n1"  # 3/5

    def test_fleet_of_1_needs_1_vote(self):
        e = ChiefElection(node_id="solo", fleet_size=1)
        p = e.propose_election("init")
        e.vote(p.proposal_id, candidate="solo", voter="solo")
        assert e.tally(p.proposal_id) == "solo"
