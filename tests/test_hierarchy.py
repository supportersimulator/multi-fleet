"""Tests for fleet hierarchy — Chief / Captain / Worker role management."""

import pytest

from multifleet.hierarchy import FleetHierarchy, NodeInfo, NodeRole


# ── Fixtures ──

@pytest.fixture
def hierarchy():
    return FleetHierarchy()


@pytest.fixture
def three_node_hierarchy():
    """Classic 3-node setup: chief + 2 workers under a captain."""
    h = FleetHierarchy()
    h.register_node("chief1", NodeRole.CHIEF, capabilities=["synthesis", "memory"])
    h.register_node("cap1", NodeRole.CAPTAIN, cluster_id="us-west", capabilities=["coordination"])
    h.register_node("w1", NodeRole.WORKER, captain_id="cap1", cluster_id="us-west")
    h.register_node("w2", NodeRole.WORKER, captain_id="cap1", cluster_id="us-west")
    return h


# ── Registration Tests ──

class TestRegistration:
    def test_register_chief(self, hierarchy):
        node = hierarchy.register_node("mac1", NodeRole.CHIEF)
        assert node.role == NodeRole.CHIEF
        assert hierarchy.get_chief().node_id == "mac1"

    def test_register_worker(self, hierarchy):
        node = hierarchy.register_node("w1", NodeRole.WORKER)
        assert node.role == NodeRole.WORKER
        assert node.captain_id is None

    def test_register_captain(self, hierarchy):
        node = hierarchy.register_node("cap1", NodeRole.CAPTAIN, cluster_id="us-west")
        assert node.role == NodeRole.CAPTAIN
        assert node.cluster_id == "us-west"

    def test_captain_default_cluster(self, hierarchy):
        """Captain without explicit cluster_id uses node_id as cluster."""
        node = hierarchy.register_node("cap1", NodeRole.CAPTAIN)
        assert node.cluster_id == "cap1"

    def test_register_worker_under_captain(self, hierarchy):
        hierarchy.register_node("cap1", NodeRole.CAPTAIN, cluster_id="us-west")
        node = hierarchy.register_node("w1", NodeRole.WORKER, captain_id="cap1")
        assert node.captain_id == "cap1"
        assert node.cluster_id == "us-west"
        cap = hierarchy.get_node("cap1")
        assert "w1" in cap.workers

    def test_register_with_capabilities(self, hierarchy):
        node = hierarchy.register_node("mac1", NodeRole.CHIEF, capabilities=["synthesis", "memory"])
        assert node.capabilities == ["synthesis", "memory"]

    def test_duplicate_chief_raises(self, hierarchy):
        hierarchy.register_node("mac1", NodeRole.CHIEF)
        with pytest.raises(ValueError, match="Chief already registered"):
            hierarchy.register_node("mac2", NodeRole.CHIEF)

    def test_re_register_same_chief_ok(self, hierarchy):
        hierarchy.register_node("mac1", NodeRole.CHIEF)
        node = hierarchy.register_node("mac1", NodeRole.CHIEF)
        assert node.role == NodeRole.CHIEF


# ── Lookup Tests ──

class TestLookups:
    def test_get_chief(self, three_node_hierarchy):
        chief = three_node_hierarchy.get_chief()
        assert chief.node_id == "chief1"

    def test_get_chief_empty(self, hierarchy):
        assert hierarchy.get_chief() is None

    def test_get_captain_for(self, three_node_hierarchy):
        cap = three_node_hierarchy.get_captain_for("w1")
        assert cap.node_id == "cap1"

    def test_get_captain_for_unassigned(self, three_node_hierarchy):
        assert three_node_hierarchy.get_captain_for("chief1") is None

    def test_get_workers_for(self, three_node_hierarchy):
        workers = three_node_hierarchy.get_workers_for("cap1")
        ids = [w.node_id for w in workers]
        assert sorted(ids) == ["w1", "w2"]

    def test_get_workers_for_nonexistent(self, hierarchy):
        assert hierarchy.get_workers_for("cap99") == []

    def test_get_node(self, three_node_hierarchy):
        node = three_node_hierarchy.get_node("w1")
        assert node is not None
        assert node.role == NodeRole.WORKER

    def test_get_node_missing(self, hierarchy):
        assert hierarchy.get_node("nope") is None


# ── Routing Tests ──

class TestRouting:
    def test_route_same_node(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("w1", "w1")
        assert path == ["w1"]

    def test_route_same_cluster_workers(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("w1", "w2")
        assert path == ["w1", "cap1", "w2"]

    def test_route_worker_to_chief(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("w1", "chief1")
        assert path == ["w1", "cap1", "chief1"]

    def test_route_chief_to_worker(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("chief1", "w1")
        assert path == ["chief1", "cap1", "w1"]

    def test_route_captain_to_chief(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("cap1", "chief1")
        assert path == ["cap1", "chief1"]

    def test_route_chief_to_captain(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("chief1", "cap1")
        assert path == ["chief1", "cap1"]

    def test_route_captain_to_own_worker(self, three_node_hierarchy):
        path = three_node_hierarchy.route_message("cap1", "w1")
        assert path == ["cap1", "w1"]

    def test_route_cross_cluster(self):
        h = FleetHierarchy()
        h.register_node("chief1", NodeRole.CHIEF)
        h.register_node("capA", NodeRole.CAPTAIN, cluster_id="us-west")
        h.register_node("capB", NodeRole.CAPTAIN, cluster_id="us-east")
        h.register_node("wA1", NodeRole.WORKER, captain_id="capA", cluster_id="us-west")
        h.register_node("wB1", NodeRole.WORKER, captain_id="capB", cluster_id="us-east")

        path = h.route_message("wA1", "wB1")
        assert path == ["wA1", "capA", "chief1", "capB", "wB1"]

    def test_route_captain_to_captain(self):
        h = FleetHierarchy()
        h.register_node("chief1", NodeRole.CHIEF)
        h.register_node("capA", NodeRole.CAPTAIN, cluster_id="us-west")
        h.register_node("capB", NodeRole.CAPTAIN, cluster_id="us-east")
        path = h.route_message("capA", "capB")
        assert path == ["capA", "chief1", "capB"]

    def test_route_unknown_nodes(self, hierarchy):
        path = hierarchy.route_message("x", "y")
        assert path == ["x", "y"]

    def test_route_worker_to_captain_other_cluster(self):
        h = FleetHierarchy()
        h.register_node("chief1", NodeRole.CHIEF)
        h.register_node("capA", NodeRole.CAPTAIN, cluster_id="west")
        h.register_node("capB", NodeRole.CAPTAIN, cluster_id="east")
        h.register_node("w1", NodeRole.WORKER, captain_id="capA", cluster_id="west")
        path = h.route_message("w1", "capB")
        assert "capA" in path  # Goes through own captain
        assert "chief1" in path  # Then chief


# ── NATS Subject Tests ──

class TestNATSSubjects:
    def test_chief_subject(self, three_node_hierarchy):
        subj = three_node_hierarchy.nats_subject("chief1")
        assert subj == "fleet.chief.message"

    def test_captain_subject(self, three_node_hierarchy):
        subj = three_node_hierarchy.nats_subject("cap1")
        assert subj == "fleet.captain.us-west.message"

    def test_worker_subject(self, three_node_hierarchy):
        subj = three_node_hierarchy.nats_subject("w1")
        assert subj == "fleet.worker.us-west.w1.message"

    def test_unknown_node_subject(self, hierarchy):
        subj = hierarchy.nats_subject("ghost")
        assert subj == "fleet.unknown.ghost.message"

    def test_custom_msg_type(self, three_node_hierarchy):
        subj = three_node_hierarchy.nats_subject("chief1", msg_type="heartbeat")
        assert subj == "fleet.chief.heartbeat"

    def test_subscribe_subjects_chief(self, three_node_hierarchy):
        subs = three_node_hierarchy.nats_subscribe_subjects("chief1")
        assert "fleet.chief.message" in subs
        assert "fleet.captain.us-west.message" in subs

    def test_subscribe_subjects_captain(self, three_node_hierarchy):
        subs = three_node_hierarchy.nats_subscribe_subjects("cap1")
        assert "fleet.captain.us-west.message" in subs
        assert "fleet.worker.us-west.*.message" in subs

    def test_subscribe_subjects_unknown(self, hierarchy):
        assert hierarchy.nats_subscribe_subjects("ghost") == []


# ── Election Tests ──

class TestElection:
    def test_elect_most_capabilities(self, hierarchy):
        hierarchy.register_node("a", NodeRole.WORKER, capabilities=["x"])
        hierarchy.register_node("b", NodeRole.WORKER, capabilities=["x", "y", "z"])
        hierarchy.register_node("c", NodeRole.WORKER, capabilities=["x", "y"])
        winner = hierarchy.elect_chief(["a", "b", "c"])
        assert winner == "b"
        assert hierarchy.get_chief().node_id == "b"

    def test_elect_tiebreak_by_id(self, hierarchy):
        hierarchy.register_node("beta", NodeRole.WORKER, capabilities=["x"])
        hierarchy.register_node("alpha", NodeRole.WORKER, capabilities=["x"])
        winner = hierarchy.elect_chief(["beta", "alpha"])
        assert winner == "alpha"  # alphabetical tie-break

    def test_elect_demotes_old_chief(self, hierarchy):
        hierarchy.register_node("old_chief", NodeRole.CHIEF, capabilities=["x"])
        hierarchy.register_node("new_chief", NodeRole.WORKER, capabilities=["x", "y", "z"])
        winner = hierarchy.elect_chief(["old_chief", "new_chief"])
        assert winner == "new_chief"
        old = hierarchy.get_node("old_chief")
        assert old.role == NodeRole.WORKER

    def test_elect_no_candidates_raises(self, hierarchy):
        with pytest.raises(ValueError, match="No valid candidates"):
            hierarchy.elect_chief([])

    def test_elect_invalid_candidates_raises(self, hierarchy):
        with pytest.raises(ValueError, match="No valid candidates"):
            hierarchy.elect_chief(["nonexistent1", "nonexistent2"])


# ── Promotion Tests ──

class TestPromotion:
    def test_promote_worker_to_captain(self, hierarchy):
        hierarchy.register_node("w1", NodeRole.WORKER)
        ok = hierarchy.promote_to_captain("w1", cluster_id="us-west")
        assert ok is True
        node = hierarchy.get_node("w1")
        assert node.role == NodeRole.CAPTAIN
        assert node.cluster_id == "us-west"

    def test_promote_nonexistent_fails(self, hierarchy):
        assert hierarchy.promote_to_captain("ghost") is False

    def test_promote_chief_fails(self, hierarchy):
        hierarchy.register_node("chief1", NodeRole.CHIEF)
        assert hierarchy.promote_to_captain("chief1") is False

    def test_promote_removes_from_old_captain(self, hierarchy):
        hierarchy.register_node("cap1", NodeRole.CAPTAIN, cluster_id="west")
        hierarchy.register_node("w1", NodeRole.WORKER, captain_id="cap1")
        hierarchy.promote_to_captain("w1")
        cap = hierarchy.get_node("cap1")
        assert "w1" not in cap.workers


# ── Serialization Tests ──

class TestSerialization:
    def test_roundtrip(self, three_node_hierarchy):
        data = three_node_hierarchy.to_dict()
        restored = FleetHierarchy.from_dict(data)

        assert restored.get_chief().node_id == "chief1"
        assert restored.get_node("cap1").role == NodeRole.CAPTAIN
        assert restored.get_node("w1").role == NodeRole.WORKER
        assert restored.get_node("w1").captain_id == "cap1"

    def test_roundtrip_preserves_workers(self, three_node_hierarchy):
        data = three_node_hierarchy.to_dict()
        restored = FleetHierarchy.from_dict(data)
        cap = restored.get_node("cap1")
        assert sorted(cap.workers) == ["w1", "w2"]

    def test_nodeinfo_roundtrip(self):
        node = NodeInfo(
            node_id="test", role=NodeRole.CAPTAIN,
            cluster_id="east", workers=["w1"], capabilities=["code"]
        )
        data = node.to_dict()
        restored = NodeInfo.from_dict(data)
        assert restored.node_id == "test"
        assert restored.role == NodeRole.CAPTAIN
        assert restored.workers == ["w1"]

    def test_empty_hierarchy_roundtrip(self):
        h = FleetHierarchy()
        data = h.to_dict()
        restored = FleetHierarchy.from_dict(data)
        assert restored.get_chief() is None
        assert len(restored.all_nodes) == 0


# ── Config Loading Tests ──

class TestConfigLoading:
    def test_load_from_config(self):
        config = {
            "nodes": {
                "mac1": {"role": "chief", "capabilities": ["synthesis"]},
                "mac2": {"role": "captain", "region": "us-west"},
                "mac3": {"role": "worker", "captain_id": "mac2", "region": "us-west"},
            }
        }
        h = FleetHierarchy(config)
        assert h.get_chief().node_id == "mac1"
        assert h.get_node("mac2").role == NodeRole.CAPTAIN
        assert h.get_node("mac3").captain_id == "mac2"

    def test_load_unknown_role_defaults_worker(self):
        config = {"nodes": {"x": {"role": "unknown_role"}}}
        h = FleetHierarchy(config)
        assert h.get_node("x").role == NodeRole.WORKER


# ── Scale Tests ──

class TestScale:
    def test_100_node_hierarchy(self):
        """Register 100 nodes across 5 clusters, verify routing."""
        h = FleetHierarchy()
        h.register_node("chief", NodeRole.CHIEF, capabilities=["synthesis"])

        # 5 captains, 19 workers each = 95 workers + 5 captains + 1 chief = 101
        for i in range(5):
            cid = f"cap{i}"
            cluster = f"cluster{i}"
            h.register_node(cid, NodeRole.CAPTAIN, cluster_id=cluster, capabilities=["coord"])
            for j in range(19):
                wid = f"w{i}_{j}"
                h.register_node(wid, NodeRole.WORKER, captain_id=cid, cluster_id=cluster)

        assert len(h.all_nodes) == 101

        # Verify same-cluster routing (3 hops: worker -> captain -> worker)
        path = h.route_message("w0_0", "w0_1")
        assert path == ["w0_0", "cap0", "w0_1"]

        # Verify cross-cluster routing (5 hops: worker -> captain -> chief -> captain -> worker)
        path = h.route_message("w0_0", "w4_18")
        assert path == ["w0_0", "cap0", "chief", "cap4", "w4_18"]

        # Verify worker to chief (3 hops)
        path = h.route_message("w3_5", "chief")
        assert path == ["w3_5", "cap3", "chief"]

        # Verify chief to any worker (3 hops)
        path = h.route_message("chief", "w2_10")
        assert path == ["chief", "cap2", "w2_10"]

    def test_100_node_nats_subjects(self):
        """NATS subjects are correctly partitioned for 100 nodes."""
        h = FleetHierarchy()
        h.register_node("chief", NodeRole.CHIEF)
        h.register_node("cap0", NodeRole.CAPTAIN, cluster_id="c0")
        h.register_node("w0", NodeRole.WORKER, captain_id="cap0", cluster_id="c0")

        assert h.nats_subject("chief") == "fleet.chief.message"
        assert h.nats_subject("cap0") == "fleet.captain.c0.message"
        assert h.nats_subject("w0") == "fleet.worker.c0.w0.message"


# ── Edge Cases ──

class TestEdgeCases:
    def test_no_chief(self):
        h = FleetHierarchy()
        h.register_node("w1", NodeRole.WORKER)
        assert h.get_chief() is None

    def test_orphan_worker_no_captain(self):
        h = FleetHierarchy()
        h.register_node("w1", NodeRole.WORKER)
        assert h.get_captain_for("w1") is None

    def test_orphan_worker_routing(self):
        """Worker without a captain routes directly."""
        h = FleetHierarchy()
        h.register_node("chief1", NodeRole.CHIEF)
        h.register_node("w1", NodeRole.WORKER)  # No captain
        path = h.route_message("w1", "chief1")
        assert path == ["w1", "chief1"]

    def test_empty_hierarchy(self):
        h = FleetHierarchy()
        assert h.get_chief() is None
        assert len(h.all_nodes) == 0
        assert h.to_dict() == {"chief_id": None, "nodes": {}}

    def test_worker_with_nonexistent_captain(self):
        """Worker references a captain that doesn't exist yet."""
        h = FleetHierarchy()
        node = h.register_node("w1", NodeRole.WORKER, captain_id="cap_ghost")
        assert node.captain_id == "cap_ghost"
        assert h.get_captain_for("w1") is None
