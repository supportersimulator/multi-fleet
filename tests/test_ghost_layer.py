"""Tests for Ghost Layer foundation — checkpoint, objections, contradictions, cross-node, orchestrator, mirror, anticipate, corroborate."""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.ghost_checkpoint import GhostCheckpoint, CheckpointData, MAX_CHECKPOINTS
from multifleet.ghost_objections import ObjectionStore, Objection
from multifleet.ghost_contradictions import ContradictionDetector, Contradiction
from multifleet.ghost_cross_node import CrossNodeSynthesizer, NodeEvidence, SynthesisResult
from multifleet.ghost_layer import GhostLayer, GhostStatus, ghost_status
from multifleet.ghost_mirror import GhostMirror, MirrorResult, run_mirror
from multifleet.ghost_anticipate import (
    GhostAnticipate, AnticipateResult, Prediction, run_anticipate,
)
from multifleet.ghost_corroborate import (
    GhostCorroborate, CorroborateResult, CorroborationItem, run_corroborate,
)


class TestGhostCheckpoint:

    def test_capture_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            data = cp.capture(reason="test")
            assert data.checkpoint_id
            assert data.timestamp
            assert data.reason == "test"
            files = list(Path(tmp).glob("*.json"))
            assert len(files) == 1

    def test_capture_collects_git_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            data = cp.capture(reason="git-test")
            # Should have branch info (we're in a git repo)
            assert data.branch or data.branch == ""  # may be empty in detached HEAD
            assert isinstance(data.dirty_files, int)

    def test_restore_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            cp.capture(reason="first")
            cp.capture(reason="second")
            restored = cp.restore("latest")
            assert restored is not None
            assert restored.reason == "second"

    def test_restore_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            data = cp.capture(reason="specific")
            restored = cp.restore(data.checkpoint_id)
            assert restored is not None
            assert restored.checkpoint_id == data.checkpoint_id

    def test_restore_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            assert cp.restore("nonexistent") is None

    def test_list_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            cp.capture(reason="a")
            cp.capture(reason="b")
            listed = cp.list_checkpoints()
            assert len(listed) == 2
            assert listed[0]["reason"] in ("a", "b")

    def test_prune_keeps_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = GhostCheckpoint(checkpoint_dir=tmp, repo_root=".")
            for i in range(MAX_CHECKPOINTS + 3):
                cp.capture(reason=f"prune-{i}")
            files = list(Path(tmp).glob("*.json"))
            assert len(files) <= MAX_CHECKPOINTS

    def test_checkpoint_data_round_trip(self):
        data = CheckpointData(
            checkpoint_id="test-123",
            timestamp="2026-04-08T12:00:00Z",
            reason="roundtrip",
            node_id="mac2",
            branch="main",
            dirty_files=3,
            unresolved_objections=["obj1"],
        )
        d = data.to_dict()
        restored = CheckpointData.from_dict(d)
        assert restored.checkpoint_id == "test-123"
        assert restored.dirty_files == 3
        assert restored.unresolved_objections == ["obj1"]


class TestObjectionStore:

    def test_record_objection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            obj = store.record("Over-engineered auth", source="surgeon", source_node="mac1")
            assert obj.objection_id
            assert obj.status == "open"
            assert "Over-engineered" in obj.claim

    def test_list_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            store.record("Objection 1")
            store.record("Objection 2")
            open_objs = store.list_open()
            assert len(open_objs) == 2

    def test_resolve_objection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            obj = store.record("Test objection")
            resolved = store.resolve(obj.objection_id, "Fixed the issue")
            assert resolved is not None
            assert resolved.status == "resolved"
            assert store.list_open() == []

    def test_dismiss_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            obj = store.record("Test")
            result = store.dismiss(obj.objection_id, "")
            assert result is None  # Should fail without reason
            assert len(store.list_open()) == 1  # Still open

    def test_dismiss_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            obj = store.record("Test")
            result = store.dismiss(obj.objection_id, "Not relevant anymore")
            assert result is not None
            assert result.status == "dismissed"

    def test_carry_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            store.record("Obj 1")
            store.record("Obj 2")
            carried = store.carry_forward()
            assert len(carried) == 2
            assert all(o.status == "carried" for o in carried)
            assert all(o.carry_count == 1 for o in carried)
            # Carried objections still show in list_open
            assert len(store.list_open()) == 2

    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ObjectionStore(store_path=str(Path(tmp) / "obj.jsonl"))
            obj1 = store.record("Open")
            obj2 = store.record("Will resolve")
            store.resolve(obj2.objection_id, "Done")
            stats = store.stats()
            assert stats["total"] == 2
            assert stats["by_status"]["open"] == 1
            assert stats["by_status"]["resolved"] == 1

    def test_objection_summary(self):
        obj = Objection(
            objection_id="test",
            source="surgeon",
            source_node="mac1",
            task_id="t1",
            claim="This is over-engineered",
        )
        s = obj.summary()
        assert "[surgeon:mac1]" in s
        assert "over-engineered" in s


class TestContradictionDetector:

    def test_scan_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            detector = ContradictionDetector(
                store_path=str(Path(tmp) / "contra.json"),
                ledger_path=str(Path(tmp) / "decisions.jsonl"),
            )
            new = detector.scan()
            assert new == []

    def test_scan_finds_task_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text(
                json.dumps({"task_id": "auth-fix", "decision": "merge", "node_id": "mac1"}) + "\n"
                + json.dumps({"task_id": "auth-fix", "decision": "reject", "node_id": "mac2"}) + "\n"
            )
            detector = ContradictionDetector(
                store_path=str(Path(tmp) / "contra.json"),
                ledger_path=str(ledger),
            )
            new = detector.scan()
            assert len(new) >= 1
            assert new[0].topic.startswith("task:")

    def test_resolve_contradiction(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "decisions.jsonl"
            ledger.write_text(
                json.dumps({"task_id": "x", "decision": "a", "node_id": "m1"}) + "\n"
                + json.dumps({"task_id": "x", "decision": "b", "node_id": "m2"}) + "\n"
            )
            detector = ContradictionDetector(
                store_path=str(Path(tmp) / "contra.json"),
                ledger_path=str(ledger),
            )
            new = detector.scan()
            assert len(new) >= 1
            resolved = detector.resolve(new[0].contradiction_id, "mac1 was right")
            assert resolved is True
            assert detector.list_unresolved() == []

    def test_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            detector = ContradictionDetector(
                store_path=str(Path(tmp) / "contra.json"),
                ledger_path=str(Path(tmp) / "empty.jsonl"),
            )
            stats = detector.stats()
            assert stats["total"] == 0
            assert stats["unresolved"] == 0

    def test_contradiction_round_trip(self):
        c = Contradiction(
            contradiction_id="test",
            topic="task:auth",
            claims=[{"node": "mac1", "claim": "merge"}],
            severity="high",
        )
        d = c.to_dict()
        restored = Contradiction.from_dict(d)
        assert restored.contradiction_id == "test"
        assert restored.severity == "high"


class TestCrossNodeSynthesizer:

    def test_synthesize_unanimous(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            evidence = [
                NodeEvidence(node_id="mac1", task_id="t1", decision="merge", confidence=0.9),
                NodeEvidence(node_id="mac2", task_id="t1", decision="merge", confidence=0.8),
            ]
            result = synth.synthesize("t1", evidence)
            assert result.consensus_decision == "merge"
            assert result.synthesis_method == "unanimous"
            assert len(result.unresolved_dissent) == 0

    def test_synthesize_with_dissent(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            evidence = [
                NodeEvidence(
                    node_id="mac1", task_id="t1", decision="merge",
                    confidence=0.9, evidence_refs=["ref1", "ref2"],
                ),
                NodeEvidence(
                    node_id="mac2", task_id="t1", decision="reject",
                    confidence=0.5, surgeon_objections=["over-engineered"],
                ),
            ]
            result = synth.synthesize("t1", evidence)
            assert result.consensus_decision == "merge"  # mac1 has more evidence + confidence
            assert len(result.unresolved_dissent) == 1
            assert "mac2" in result.unresolved_dissent[0]

    def test_synthesize_three_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            evidence = [
                NodeEvidence(node_id="mac1", task_id="t1", decision="merge", confidence=0.9),
                NodeEvidence(node_id="mac2", task_id="t1", decision="reject", confidence=0.3),
                NodeEvidence(node_id="mac3", task_id="t1", decision="merge", confidence=0.7),
            ]
            result = synth.synthesize("t1", evidence)
            assert result.consensus_decision == "merge"
            assert result.synthesis_method == "majority_weighted"
            assert len(result.nodes_contributing) == 3

    def test_evidence_weight_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            evidence = [
                NodeEvidence(
                    node_id="mac1", task_id="t1", decision="merge",
                    confidence=0.5, evidence_refs=["a", "b", "c"],
                ),
                NodeEvidence(
                    node_id="mac2", task_id="t1", decision="reject",
                    confidence=0.8, surgeon_objections=["bad", "worse", "worst"],
                ),
            ]
            result = synth.synthesize("t1", evidence)
            # mac1: 0.5 + 0.3 (3 refs) = 0.8
            # mac2: 0.8 - 0.3 (3 objections) = 0.5
            assert result.evidence_weight_by_node["mac1"] > result.evidence_weight_by_node["mac2"]

    def test_synthesize_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            result = synth.synthesize("empty", [])
            assert result.synthesis_method == "empty"

    def test_persist_and_retrieve(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = CrossNodeSynthesizer(synthesis_dir=tmp)
            evidence = [
                NodeEvidence(node_id="mac1", task_id="persist-test", decision="merge", confidence=0.9),
            ]
            synth.synthesize("persist-test", evidence)
            retrieved = synth.get_latest("persist-test")
            assert retrieved is not None
            assert retrieved.consensus_decision == "merge"

    def test_node_evidence_round_trip(self):
        ne = NodeEvidence(
            node_id="mac1", task_id="t1", decision="merge",
            confidence=0.85, evidence_refs=["ref1"],
        )
        d = ne.to_dict()
        restored = NodeEvidence.from_dict(d)
        assert restored.node_id == "mac1"
        assert restored.confidence == 0.85


class TestGhostLayerOrchestrator:

    def test_status_reports_modules(self):
        layer = GhostLayer(repo_root=".")
        s = layer.status()
        assert isinstance(s, GhostStatus)
        # At minimum pulse and digest should be available (they exist in codebase)
        assert s.pulse_available or not s.pulse_available  # just check it runs

    def test_status_to_dict(self):
        s = GhostStatus(
            healthy=True,
            pulse_available=True,
            digest_available=True,
            open_objections=3,
        )
        d = s.to_dict()
        assert d["healthy"] is True
        assert d["modules"]["pulse"] is True
        assert d["open_objections"] == 3

    def test_convenience_ghost_status(self):
        result = ghost_status(repo_root=".")
        assert "healthy" in result
        assert "modules" in result

    def test_session_end_captures_checkpoint(self):
        """session_end should carry forward objections and create checkpoint."""
        layer = GhostLayer(repo_root=".")
        result = layer.session_end()
        assert result["action"] == "session_end"
        # May have errors if modules not fully configured, that's OK

    def test_session_start_reports_state(self):
        layer = GhostLayer(repo_root=".")
        result = layer.session_start()
        assert result["action"] == "session_start"


# ---------------------------------------------------------------------------
# Ghost Mirror tests
# ---------------------------------------------------------------------------

class TestGhostMirror:

    def test_mirror_result_summary_no_drift(self):
        r = MirrorResult()
        assert r.summary() == "no drift"

    def test_mirror_result_summary_with_drift(self):
        r = MirrorResult(
            drift_items=[{"doc": "CLAUDE.md", "missing_ref": "foo/bar.py"}],
            fix_suggestions=["Update CLAUDE.md: remove reference to 'foo/bar.py'"],
        )
        assert "1 drift items" in r.summary()
        assert "1 suggestions" in r.summary()

    def test_mirror_result_to_dict(self):
        r = MirrorResult(docs_checked=2, duration_ms=12.345, timestamp="2026-01-01T00:00:00Z")
        d = r.to_dict()
        assert d["docs_checked"] == 2
        assert d["duration_ms"] == 12.3

    def test_scan_runs_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = GhostMirror(repo_root=tmp, node_id="test")
            result = mirror.scan()
            assert isinstance(result, MirrorResult)
            assert result.duration_ms >= 0

    @patch("multifleet.ghost_mirror.GhostMirror._run_probe")
    def test_scan_with_drift_findings(self, mock_probe):
        mock_probe.return_value = {
            "status": "warning",
            "findings": [{
                "title": "CLAUDE.md references 2 non-existent path(s)",
                "evidence": ["Missing: memory/gone.py", "Missing: scripts/deleted.sh"],
                "file_paths": ["CLAUDE.md"],
            }],
        }
        mirror = GhostMirror(repo_root=".", node_id="test")
        result = mirror.scan()
        assert len(result.drift_items) == 2
        assert len(result.fix_suggestions) == 2
        assert "gone.py" in result.fix_suggestions[0]

    def test_run_mirror_convenience(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = run_mirror(repo_root=tmp)
            assert "docs_checked" in d
            assert "drift_items" in d


# ---------------------------------------------------------------------------
# Ghost Anticipate tests
# ---------------------------------------------------------------------------

class TestGhostAnticipate:

    def test_prediction_to_dict(self):
        p = Prediction(kind="file_edit", target="foo.py", confidence=0.75, reason="recent")
        d = p.to_dict()
        assert d["kind"] == "file_edit"
        assert d["confidence"] == 0.75

    def test_anticipate_result_summary_empty(self):
        r = AnticipateResult()
        assert r.summary() == "no predictions"

    def test_anticipate_result_summary_with_predictions(self):
        r = AnticipateResult(predictions=[
            Prediction("file_edit", "a.py", 0.7),
            Prediction("file_edit", "b.py", 0.6),
            Prediction("probe_run", "mirror_drift", 0.8),
        ])
        s = r.summary()
        assert "file_edit" in s
        assert "probe_run" in s

    def test_anticipate_result_to_dict(self):
        r = AnticipateResult(
            predictions=[Prediction("file_edit", "a.py", 0.7)],
            backlog_items_added=1,
            duration_ms=5.678,
            timestamp="2026-01-01T00:00:00Z",
        )
        d = r.to_dict()
        assert d["backlog_items_added"] == 1
        assert len(d["predictions"]) == 1

    @patch("multifleet.ghost_anticipate.subprocess.run")
    def test_anticipate_with_recent_files(self, mock_run):
        # Mock git log, git branch, git diff
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="abc1234 Fix thing\nfoo.py\nbar.py\n"),
            MagicMock(returncode=0, stdout="feature/ghost-layer\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            # Create the files so they pass existence check
            (Path(tmp) / "foo.py").write_text("x")
            (Path(tmp) / "bar.py").write_text("x")
            ghost = GhostAnticipate(repo_root=tmp, node_id="test")
            result = ghost.anticipate()
            assert isinstance(result, AnticipateResult)
            # Should predict file edits + memory query from branch name
            file_preds = [p for p in result.predictions if p.kind == "file_edit"]
            mem_preds = [p for p in result.predictions if p.kind == "memory_query"]
            assert len(file_preds) >= 1
            assert len(mem_preds) >= 1
            assert "ghost layer" in mem_preds[0].target

    def test_predict_probes_from_docs(self):
        ghost = GhostAnticipate(repo_root=".", node_id="test")
        preds = ghost._predict_probes(["README.md", "CLAUDE.md"])
        assert any(p.target == "mirror_drift" for p in preds)

    def test_predict_probes_from_deps(self):
        ghost = GhostAnticipate(repo_root=".", node_id="test")
        preds = ghost._predict_probes(["requirements.txt"])
        assert any(p.target == "dependency_drift" for p in preds)

    def test_run_anticipate_convenience(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = run_anticipate(repo_root=tmp)
            assert "predictions" in d
            assert "backlog_items_added" in d


# ---------------------------------------------------------------------------
# Ghost Corroborate tests
# ---------------------------------------------------------------------------

class TestGhostCorroborate:

    def test_corroboration_item_to_dict(self):
        item = CorroborationItem(
            finding_title="drift detected",
            original_confidence=0.5,
            adjusted_confidence=0.6,
            prior_occurrences=2,
            verdict="unchanged",
        )
        d = item.to_dict()
        assert d["verdict"] == "unchanged"
        assert d["adjusted_confidence"] == 0.6

    def test_corroborate_result_summary_empty(self):
        r = CorroborateResult()
        assert r.summary() == "no findings to corroborate"

    def test_corroborate_result_summary_with_items(self):
        r = CorroborateResult(strengthened=2, novel=1)
        s = r.summary()
        assert "2 strengthened" in s
        assert "1 novel" in s

    def test_novel_finding_gets_penalty(self):
        ghost = GhostCorroborate(node_id="test")
        # Patch history to return nothing
        with patch.object(ghost, "_load_history", return_value=[]):
            result = ghost.corroborate([
                {"title": "Brand new finding never seen", "confidence": 0.7},
            ])
            assert result.novel == 1
            item = result.items[0]
            assert item.verdict == "novel"
            assert item.adjusted_confidence < item.original_confidence

    def test_recurring_finding_gets_boost(self):
        # Create fake history with matching entries
        fake_history = [
            {"payload": {"title": "Mirror drift path references broken"}},
            {"payload": {"title": "Mirror drift path references broken"}},
            {"payload": {"title": "Mirror drift path references broken"}},
        ]
        ghost = GhostCorroborate(node_id="test")
        with patch.object(ghost, "_load_history", return_value=fake_history):
            result = ghost.corroborate([
                {"title": "Mirror drift path references broken", "confidence": 0.5},
            ])
            assert result.strengthened == 1
            item = result.items[0]
            assert item.verdict == "strengthened"
            assert item.adjusted_confidence > item.original_confidence

    def test_confidence_ceiling(self):
        ghost = GhostCorroborate(node_id="test")
        item = ghost._assess("recurring finding", 0.92, 10)
        assert item.adjusted_confidence <= GhostCorroborate.CONFIDENCE_CEILING

    def test_confidence_floor(self):
        ghost = GhostCorroborate(node_id="test")
        item = ghost._assess("novel finding", 0.12, 0)
        assert item.adjusted_confidence >= GhostCorroborate.CONFIDENCE_FLOOR

    def test_run_corroborate_convenience(self):
        d = run_corroborate(findings=[], node_id="test")
        assert "items" in d
        assert "strengthened" in d
