"""Tests for the Rebuttal State Machine."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.rebuttal import (
    ACCEPTED,
    AUTO_ACCEPT_TIMEOUT_S,
    COUNTER_PROPOSED,
    EXPIRED,
    PROPOSED,
    REBUTTED,
    RESOLVED,
    UNDER_REVIEW,
    RebuttalManager,
)
from multifleet.store import FleetNerveStore


@pytest.fixture
def store(tmp_path):
    """Create a fresh store with a temp DB."""
    db = tmp_path / "test.db"
    return FleetNerveStore(db_path=db)


@pytest.fixture
def mgr(store):
    """Create a RebuttalManager for mac1."""
    return RebuttalManager(store, "mac1")


@pytest.fixture
def mgr2(store):
    """Create a second RebuttalManager for mac2 (same store)."""
    return RebuttalManager(store, "mac2")


class TestPropose:
    def test_propose_creates_proposal(self, mgr):
        p = mgr.propose("Add caching", "Redis L2 cache for webhook")
        assert p is not None
        assert p["state"] == PROPOSED
        assert p["proposer"] == "mac1"
        assert p["subject"] == "Add caching"
        assert p["body"] == "Redis L2 cache for webhook"
        assert p["scope"] == "general"
        assert len(p["history"]) == 1
        assert p["history"][0]["state"] == PROPOSED

    def test_propose_with_scope(self, mgr):
        p = mgr.propose("Break API", "New schema", scope="breaking")
        assert p["scope"] == "breaking"


class TestReview:
    def test_review_transitions_state(self, mgr, mgr2):
        p = mgr.propose("Test review", "body")
        r = mgr2.review(p["proposal_id"])
        assert r["state"] == UNDER_REVIEW
        assert len(r["history"]) == 2
        assert r["history"][1]["actor"] == "mac2"


class TestAccept:
    def test_accept_proposal(self, mgr, mgr2):
        p = mgr.propose("Accept me", "please")
        mgr2.review(p["proposal_id"])
        r = mgr2.accept(p["proposal_id"], reason="looks good")
        assert r["state"] == ACCEPTED
        assert r["resolution"] == "accepted"
        assert r["resolved_at"] is not None

    def test_accept_from_proposed(self, mgr, mgr2):
        """Can accept directly from proposed (skipping review)."""
        p = mgr.propose("Quick accept", "trivial change")
        # proposed -> accepted is valid per TRANSITIONS
        r = mgr2.accept(p["proposal_id"])
        assert r["state"] == ACCEPTED


class TestRebut:
    def test_rebut_with_reason(self, mgr, mgr2):
        p = mgr.propose("Bad idea", "body")
        mgr2.review(p["proposal_id"])
        r = mgr2.rebut(p["proposal_id"], reason="Too complex")
        assert r["state"] == REBUTTED
        assert "Too complex" in r["history"][-1]["detail"]

    def test_rebut_with_counter(self, mgr, mgr2):
        p = mgr.propose("Approach A", "body")
        mgr2.review(p["proposal_id"])
        r = mgr2.rebut(p["proposal_id"], reason="Prefer B", counter="Use approach B instead")
        assert r["state"] == REBUTTED
        assert "Prefer B" in r["history"][-1]["detail"]
        assert "approach B" in r["history"][-1]["detail"]


class TestCounterPropose:
    def test_counter_propose_links(self, mgr, mgr2):
        p = mgr.propose("Original", "original body")
        mgr2.review(p["proposal_id"])
        mgr2.rebut(p["proposal_id"], reason="nope")
        cp = mgr2.counter_propose(p["proposal_id"], "Better approach")
        assert cp is not None
        assert cp["state"] == PROPOSED
        assert cp["parent_id"] == p["proposal_id"]
        assert "Counter:" in cp["subject"]
        assert cp["body"] == "Better approach"

        # Original should be in counter_proposed state
        orig = mgr.get(p["proposal_id"])
        assert orig["state"] == COUNTER_PROPOSED


class TestResolve:
    def test_resolve_chain(self, mgr, mgr2):
        p = mgr.propose("Resolve me", "body")
        mgr2.review(p["proposal_id"])
        mgr2.rebut(p["proposal_id"], reason="disagree")
        r = mgr.resolve(p["proposal_id"], "merged both approaches")
        assert r["state"] == RESOLVED
        assert r["resolution"] == "merged both approaches"
        assert r["resolved_at"] is not None


class TestInvalidTransitions:
    def test_invalid_transition_rejected(self, mgr, mgr2):
        p = mgr.propose("Test invalid", "body")
        # Can't go from proposed -> resolved directly
        r = mgr.resolve(p["proposal_id"], "skip")
        assert "error" in r or r["state"] == PROPOSED

    def test_cannot_rebut_without_review(self, mgr, mgr2):
        p = mgr.propose("Test", "body")
        # proposed -> rebutted is not valid
        r = mgr2.rebut(p["proposal_id"], reason="nope")
        assert r["state"] == PROPOSED  # unchanged


class TestExpireStale:
    def test_expire_stale_auto_accepts(self, mgr):
        p = mgr.propose("Stale", "nobody responded")

        # Manually backdate created_at
        conn = mgr._store._conn()
        old_time = time.time() - AUTO_ACCEPT_TIMEOUT_S - 10
        conn.execute(
            "UPDATE rebuttals SET created_at = ? WHERE proposal_id = ?",
            (old_time, p["proposal_id"]),
        )
        conn.commit()

        count = mgr.expire_stale()
        assert count == 1

        updated = mgr.get(p["proposal_id"])
        assert updated["state"] == ACCEPTED
        assert updated["resolution"] == "auto-accepted"

    def test_expire_does_not_touch_fresh(self, mgr):
        mgr.propose("Fresh", "just created")
        count = mgr.expire_stale()
        assert count == 0


class TestHistory:
    def test_history_tracks_transitions(self, mgr, mgr2):
        p = mgr.propose("Track history", "body")
        mgr2.review(p["proposal_id"])
        mgr2.rebut(p["proposal_id"], reason="nah")
        mgr.resolve(p["proposal_id"], "ok fine")
        final = mgr.get(p["proposal_id"])
        assert len(final["history"]) == 4
        states = [h["state"] for h in final["history"]]
        assert states == [PROPOSED, UNDER_REVIEW, REBUTTED, RESOLVED]


class TestListActive:
    def test_list_active_filters(self, mgr, mgr2):
        p1 = mgr.propose("Active 1", "body")
        p2 = mgr.propose("Active 2", "body")
        p3 = mgr.propose("Will accept", "body")
        mgr2.accept(p3["proposal_id"])

        active = mgr.list_active()
        ids = [a["proposal_id"] for a in active]
        assert p1["proposal_id"] in ids
        assert p2["proposal_id"] in ids
        assert p3["proposal_id"] not in ids  # accepted = terminal

    def test_list_active_by_state(self, mgr, mgr2):
        p1 = mgr.propose("P1", "body")
        p2 = mgr.propose("P2", "body")
        mgr2.review(p2["proposal_id"])

        proposed = mgr.list_active(state=PROPOSED)
        assert len(proposed) == 1
        assert proposed[0]["proposal_id"] == p1["proposal_id"]

        reviewing = mgr.list_active(state=UNDER_REVIEW)
        assert len(reviewing) == 1
        assert reviewing[0]["proposal_id"] == p2["proposal_id"]


class TestStats:
    def test_stats(self, mgr, mgr2):
        mgr.propose("S1", "body")
        p2 = mgr.propose("S2", "body")
        mgr2.accept(p2["proposal_id"])
        stats = mgr.stats
        assert stats["total"] == 2
        assert stats["active"] == 1
        assert stats["by_state"][PROPOSED] == 1
        assert stats["by_state"][ACCEPTED] == 1
