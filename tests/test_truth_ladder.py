"""Tests for truth_ladder.py — Inbox → Vision → Snapshot → DAO pipeline."""

import pytest
from multifleet.truth_ladder import TruthLadder, TruthEntry, LAYERS


@pytest.fixture
def ladder(tmp_path):
    db = tmp_path / "truth.db"
    return TruthLadder(db_path=str(db))


class TestAddInbox:
    def test_add_inbox(self, ladder):
        entry = ladder.add_inbox("Test idea", "Some raw content")
        assert entry.layer == "inbox"
        assert entry.title == "Test idea"
        assert entry.status == "active"
        assert entry.promoted_from == ""

    def test_add_inbox_with_metadata(self, ladder):
        entry = ladder.add_inbox("Meta idea", "Content", metadata={"source": "chat"})
        assert entry.metadata["source"] == "chat"


class TestPromoteInboxToVision:
    def test_promote_inbox_to_vision(self, ladder):
        inbox = ladder.add_inbox("Idea", "Raw idea content")
        vision = ladder.promote_to_vision(inbox.id, evidence_refs=["chat-123"], promoted_by="mac1")
        assert vision.layer == "vision"
        assert vision.promoted_from == inbox.id
        assert vision.promoted_by == "mac1"
        assert "chat-123" in vision.evidence_refs
        # Source should be marked promoted
        source = ladder.get(inbox.id)
        assert source.status == "promoted"

    def test_cannot_promote_vision_as_inbox(self, ladder):
        inbox = ladder.add_inbox("Idea", "Content")
        vision = ladder.promote_to_vision(inbox.id)
        with pytest.raises(ValueError, match="Expected source layer: inbox"):
            ladder.promote_to_vision(vision.id)


class TestPromoteVisionToSnapshot:
    def test_promote_vision_to_snapshot(self, ladder):
        inbox = ladder.add_inbox("Idea", "Content")
        vision = ladder.promote_to_vision(inbox.id)
        snapshot = ladder.promote_to_snapshot(vision.id, evidence_refs=["probe-456"])
        assert snapshot.layer == "snapshot"
        assert snapshot.promoted_from == vision.id

    def test_cannot_promote_inbox_to_snapshot(self, ladder):
        inbox = ladder.add_inbox("Idea", "Content")
        with pytest.raises(ValueError, match="Expected source layer: vision"):
            ladder.promote_to_snapshot(inbox.id)


class TestPromoteSnapshotToDao:
    def test_promote_snapshot_to_dao(self, ladder):
        inbox = ladder.add_inbox("Idea", "Content")
        vision = ladder.promote_to_vision(inbox.id)
        snapshot = ladder.promote_to_snapshot(vision.id)
        dao = ladder.promote_to_dao(snapshot.id, evidence_refs=["stability-789"])
        assert dao.layer == "dao"
        assert dao.promoted_from == snapshot.id


class TestCannotSkipSnapshot:
    def test_cannot_skip_snapshot_vision_to_dao(self, ladder):
        """Vision → DAO must fail — MUST go through snapshot."""
        inbox = ladder.add_inbox("Idea", "Content")
        vision = ladder.promote_to_vision(inbox.id)
        with pytest.raises(ValueError, match="Expected source layer: snapshot"):
            ladder.promote_to_dao(vision.id)

    def test_cannot_skip_all_inbox_to_dao(self, ladder):
        inbox = ladder.add_inbox("Idea", "Content")
        with pytest.raises(ValueError, match="Expected source layer: snapshot"):
            ladder.promote_to_dao(inbox.id)


class TestPromotionChain:
    def test_promotion_chain(self, ladder):
        inbox = ladder.add_inbox("Chain test", "Content")
        vision = ladder.promote_to_vision(inbox.id, promoted_by="mac1")
        snapshot = ladder.promote_to_snapshot(vision.id, promoted_by="mac2")
        dao = ladder.promote_to_dao(snapshot.id, promoted_by="mac1")

        chain = ladder.get_promotion_chain(dao.id)
        layers = [e.layer for e in chain]
        assert layers == ["inbox", "vision", "snapshot", "dao"]

    def test_chain_from_middle(self, ladder):
        inbox = ladder.add_inbox("Mid chain", "Content")
        vision = ladder.promote_to_vision(inbox.id)
        snapshot = ladder.promote_to_snapshot(vision.id)

        chain = ladder.get_promotion_chain(vision.id)
        layers = [e.layer for e in chain]
        assert "inbox" in layers
        assert "vision" in layers


class TestQueryByLayer:
    def test_query_by_layer(self, ladder):
        ladder.add_inbox("A", "a")
        ladder.add_inbox("B", "b")
        inbox = ladder.add_inbox("C", "c")
        ladder.promote_to_vision(inbox.id)

        inbox_items = ladder.query(layer="inbox")
        assert len(inbox_items) == 2  # C was promoted
        vision_items = ladder.query(layer="vision")
        assert len(vision_items) == 1

    def test_query_all(self, ladder):
        ladder.add_inbox("X", "x")
        all_items = ladder.query()
        assert len(all_items) == 1

    def test_stats(self, ladder):
        ladder.add_inbox("A", "a")
        inbox = ladder.add_inbox("B", "b")
        ladder.promote_to_vision(inbox.id)
        stats = ladder.stats
        assert stats["inbox"] == 1
        assert stats["vision"] == 1
        assert stats["total"] == 2

    def test_invalid_layer_raises(self, ladder):
        with pytest.raises(ValueError, match="Invalid layer"):
            ladder.query(layer="bogus")


class TestGetEntry:
    def test_get_existing(self, ladder):
        entry = ladder.add_inbox("Find me", "Content")
        found = ladder.get(entry.id)
        assert found is not None
        assert found.title == "Find me"

    def test_get_missing(self, ladder):
        assert ladder.get("nonexistent-id") is None

    def test_promote_missing_raises(self, ladder):
        with pytest.raises(ValueError, match="Entry not found"):
            ladder.promote_to_vision("nonexistent-id")
