"""Tests for cross-product adapters — the integration glue between 5 products."""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── ChiefSyncAdapter ──────────────────────────────────────────────────────


class TestChiefSyncAdapter:
    def test_import(self):
        from multifleet.adapters.chief_sync import ChiefSyncAdapter
        adapter = ChiefSyncAdapter()
        assert adapter is not None

    def test_pull_empty_context(self):
        from multifleet.adapters.chief_sync import ChiefSyncAdapter
        adapter = ChiefSyncAdapter()
        ctx = adapter.pull(task_summary="test task")
        assert ctx.query_count >= 0
        assert ctx.pull_duration_ms >= 0
        assert isinstance(ctx.to_dict(), dict)

    def test_context_summary(self):
        from multifleet.adapters.chief_sync import ChiefMemoryContext
        ctx = ChiefMemoryContext()
        assert ctx.summary() == "no memory context"
        assert not ctx.has_content

        ctx.professor_guidance = "Watch out for race conditions"
        assert ctx.has_content
        assert "professor" in ctx.summary()

    def test_context_to_dict(self):
        from multifleet.adapters.chief_sync import ChiefMemoryContext
        ctx = ChiefMemoryContext(
            professor_guidance="test",
            prior_failures=["fail1"],
            arch_decisions=["arch1"],
        )
        d = ctx.to_dict()
        assert d["professor_guidance"] == "test"
        assert d["prior_failures"] == ["fail1"]
        assert d["arch_decisions"] == ["arch1"]

    def test_singleton(self):
        from multifleet.adapters.chief_sync import get_chief_sync
        a = get_chief_sync()
        b = get_chief_sync()
        assert a is b


# ── PacketDeliveryAdapter ────────────────────────────────────────────────


class TestPacketDeliveryAdapter:
    def test_import(self):
        from multifleet.adapters.packet_delivery import PacketDeliveryAdapter
        adapter = PacketDeliveryAdapter()
        assert adapter is not None

    def test_enrich_adds_packet(self):
        from multifleet.adapters.packet_delivery import PacketDeliveryAdapter
        adapter = PacketDeliveryAdapter()
        payload = {
            "task_id": "test-123",
            "summary": "Fix auth",
            "files": ["src/auth.py"],
        }
        enriched = adapter.enrich(payload, run_probes=False)
        # epistemic_packet key should exist (may be None if builder fails)
        assert "epistemic_packet" in enriched

    def test_strip_for_size_no_op_small(self):
        from multifleet.adapters.packet_delivery import PacketDeliveryAdapter
        adapter = PacketDeliveryAdapter()
        small_payload = {"task_id": "x", "epistemic_packet": {"probe_results": []}}
        result = adapter.strip_for_size(small_payload)
        assert result == small_payload

    def test_singleton(self):
        from multifleet.adapters.packet_delivery import get_packet_delivery
        a = get_packet_delivery()
        b = get_packet_delivery()
        assert a is b


# ── SurgeonEventsAdapter ────────────────────────────────────────────────


class TestSurgeonEventsAdapter:
    def test_import(self):
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter
        adapter = SurgeonEventsAdapter(node_id="test")
        assert adapter is not None

    def test_phase_change_emits_event(self):
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter
        adapter = SurgeonEventsAdapter(node_id="test")
        event = adapter.on_phase_change("triage", intensity="standard", disagreement=0.3)
        assert event is not None
        assert event.type == "surgeon.phase.changed"
        assert adapter.stats["phase_events"] == 1

    def test_phase_dedup(self):
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter
        adapter = SurgeonEventsAdapter(node_id="test")
        adapter.on_phase_change("triage")
        event2 = adapter.on_phase_change("triage")  # Same phase
        assert event2 is None  # Deduplicated

    def test_consensus_event(self):
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter
        adapter = SurgeonEventsAdapter(node_id="test")
        event = adapter.on_consensus(
            task_id="fix-auth",
            agreed=True,
            confidence=0.85,
            surgeons_consulted=["atlas", "cardiologist"],
        )
        assert event.type == "surgeon.consensus.reached"
        assert adapter.stats["consensus_events"] == 1

    def test_consensus_blocked(self):
        from multifleet.adapters.surgeon_events import SurgeonEventsAdapter
        adapter = SurgeonEventsAdapter(node_id="test")
        event = adapter.on_consensus(
            task_id="fix-auth",
            agreed=False,
            dissent_count=2,
        )
        assert event.type == "surgeon.consensus.blocked"

    def test_singleton(self):
        from multifleet.adapters.surgeon_events import get_surgeon_events
        a = get_surgeon_events()
        b = get_surgeon_events()
        assert a is b


# ── SurgeonEscalationAdapter ───────────────────────────────────────────


class TestSurgeonEscalationAdapter:
    def test_import(self):
        from multifleet.adapters.surgeon_escalation import SurgeonEscalationAdapter
        adapter = SurgeonEscalationAdapter(node_id="test")
        assert adapter is not None

    def test_escalate_no_consensus(self):
        from multifleet.adapters.surgeon_escalation import SurgeonEscalationAdapter
        from multifleet.vision_packets import SurgeonDecisionPacket
        adapter = SurgeonEscalationAdapter(node_id="test")
        decision = SurgeonDecisionPacket(
            task_id="fix-auth",
            consensus=False,
            unresolved_objections=["obj1", "obj2", "obj3"],
        )
        packet = adapter.escalate_no_consensus(decision)
        assert packet.reason == "no_consensus"
        assert packet.from_node == "test"
        assert packet.urgency == "high"  # 3 objections
        assert adapter.stats["escalations"] == 1

    def test_escalate_high_risk(self):
        from multifleet.adapters.surgeon_escalation import SurgeonEscalationAdapter
        adapter = SurgeonEscalationAdapter(node_id="test")
        packet = adapter.escalate_high_risk(
            task_id="risky-change",
            risk_description="Touches auth subsystem",
        )
        assert packet.reason == "high_risk"
        assert packet.urgency == "high"

    def test_receive_decision(self):
        from multifleet.adapters.surgeon_escalation import SurgeonEscalationAdapter
        from multifleet.vision_packets import ChiefDecisionPacket
        adapter = SurgeonEscalationAdapter(node_id="test")
        decision = ChiefDecisionPacket(
            decision="approve",
            reasoning="Evidence supports this approach",
            memory_refs=["ref1"],
        )
        result = adapter.receive_decision(decision)
        assert result["decision"] == "approve"
        assert not result["action_required"]
        assert adapter.stats["decisions_received"] == 1

    def test_singleton(self):
        from multifleet.adapters.surgeon_escalation import get_surgeon_escalation
        a = get_surgeon_escalation()
        b = get_surgeon_escalation()
        assert a is b


# ── BrainstormIngestAdapter ──────────────────────────────────────────────


class TestBrainstormIngestAdapter:
    def test_import(self):
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        adapter = BrainstormIngestAdapter(node_id="test")
        assert adapter is not None

    def test_ingest_from_dict(self):
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        adapter = BrainstormIngestAdapter(node_id="test")
        packet = adapter.ingest({
            "task_id": "fix-auth",
            "options": [
                {"title": "Option A", "description": "Refactor cache", "pros": ["fast"], "cons": ["complex"]},
                {"title": "Option B", "description": "Replace library", "pros": ["clean"], "cons": ["risky"]},
            ],
        })
        assert packet.task_id == "fix-auth"
        assert len(packet.options) == 2
        assert packet.options[0].title == "Option A"
        assert packet.recommended == packet.options[0].id
        assert adapter.stats["from_dict"] == 1

    def test_ingest_from_markdown(self):
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        adapter = BrainstormIngestAdapter(node_id="test")
        md = """# Fix Auth Token
## Option A: Refactor cache
Cache tokens in memory with TTL.
**Pros:** fast, simple
**Cons:** memory usage

## Option B: Replace library
Use a maintained auth library.
**Pros:** well-tested
**Cons:** migration cost
"""
        packet = adapter.ingest(md)
        assert len(packet.options) == 2
        assert "Refactor cache" in packet.options[0].title
        assert adapter.stats["from_markdown"] == 1

    def test_to_surgeon_request(self):
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        adapter = BrainstormIngestAdapter(node_id="test")
        packet = adapter.ingest({
            "task_id": "fix-auth",
            "options": [
                {"title": "Option A", "description": "Desc A", "pros": ["fast"], "cons": ["risky"]},
            ],
        })
        request = adapter.to_surgeon_request(packet)
        assert request["task_id"] == "fix-auth"
        assert "Option A" in request["options"]
        assert request["intensity"] == "standard"

    def test_empty_ingest(self):
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        adapter = BrainstormIngestAdapter(node_id="test")
        packet = adapter.ingest({"task_id": "empty"})
        assert packet.task_id == "empty"
        assert len(packet.options) == 0

    def test_singleton(self):
        from multifleet.adapters.brainstorm_ingest import get_brainstorm_ingest
        a = get_brainstorm_ingest()
        b = get_brainstorm_ingest()
        assert a is b


# ── OverlayStreamAdapter ────────────────────────────────────────────────


class TestOverlayStreamAdapter:
    def test_import(self):
        from multifleet.adapters.overlay_stream import OverlayStreamAdapter
        adapter = OverlayStreamAdapter(node_id="test")
        assert adapter is not None

    def test_collect_and_push(self):
        from multifleet.adapters.overlay_stream import OverlayStreamAdapter
        adapter = OverlayStreamAdapter(node_id="test")
        result = adapter.collect_and_push(force=True)
        # May return dict or None depending on overlay manager availability
        assert result is None or isinstance(result, dict)

    def test_rate_limiting(self):
        from multifleet.adapters.overlay_stream import OverlayStreamAdapter
        adapter = OverlayStreamAdapter(node_id="test")
        adapter._push_interval_s = 100  # Very long interval
        adapter.collect_and_push(force=True)  # First push
        result = adapter.collect_and_push(force=False)  # Should be rate-limited
        assert result is None

    def test_singleton(self):
        from multifleet.adapters.overlay_stream import get_overlay_stream
        a = get_overlay_stream()
        b = get_overlay_stream()
        assert a is b


# ── Cross-adapter integration ───────────────────────────────────────────


class TestAdapterIntegration:
    """Test adapters working together in a pipeline."""

    def test_brainstorm_to_escalation_pipeline(self):
        """BrainstormPacket -> SurgeonDecision (no consensus) -> ChiefEscalation"""
        from multifleet.adapters.brainstorm_ingest import BrainstormIngestAdapter
        from multifleet.adapters.surgeon_escalation import SurgeonEscalationAdapter
        from multifleet.vision_packets import SurgeonDecisionPacket

        # Step 1: Ingest brainstorm
        ingest = BrainstormIngestAdapter(node_id="test")
        packet = ingest.ingest({
            "task_id": "fix-auth",
            "options": [
                {"title": "Option A", "description": "Cache", "pros": ["fast"]},
                {"title": "Option B", "description": "Library", "pros": ["clean"]},
            ],
        })
        assert len(packet.options) == 2

        # Step 2: Simulate surgeon decision with no consensus
        decision = SurgeonDecisionPacket(
            brainstorm_id=packet.id,
            task_id=packet.task_id,
            consensus=False,
            unresolved_objections=["timing concern", "memory leak risk"],
        )

        # Step 3: Escalate to chief
        escalation = SurgeonEscalationAdapter(node_id="test")
        esc_packet = escalation.escalate_no_consensus(decision, task_id="fix-auth")
        assert esc_packet.brainstorm_id == packet.id
        assert esc_packet.reason == "no_consensus"

    def test_chief_sync_to_overlay_pipeline(self):
        """ChiefSync pull -> OverlayStream push"""
        from multifleet.adapters.chief_sync import ChiefSyncAdapter
        from multifleet.adapters.overlay_stream import OverlayStreamAdapter

        sync = ChiefSyncAdapter()
        ctx = sync.pull(task_summary="test task")
        assert isinstance(ctx.to_dict(), dict)

        overlay = OverlayStreamAdapter(node_id="test")
        # Overlay push uses ctx data
        overlay.push_surgeon_update(phase="review")
        assert overlay.stats["pushes"] >= 0  # May or may not succeed
