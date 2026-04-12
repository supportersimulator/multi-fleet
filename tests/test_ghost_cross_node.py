"""Tests for CrossNodeSynthesizer remote evidence collection."""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from unittest.mock import patch, MagicMock

import pytest

from multifleet.ghost_cross_node import (
    CrossNodeSynthesizer,
    NodeEvidence,
    SynthesisResult,
)


# ── Fixtures ────────────────────────────────────────────────────────────

def _make_evidence_entries(node_id="mac2", count=2, task_id="task-abc"):
    """Generate fake evidence ledger entries as the daemon would return."""
    entries = []
    for i in range(count):
        entries.append({
            "entry_id": f"entry-{node_id}-{i}",
            "timestamp": time.time() - (count - i),
            "event_type": "merge_decision",
            "node_id": node_id,
            "subject": f"merge feature-{i}",
            "entry_hash": f"hash-{i}",
            "payload": {
                "task_id": task_id,
                "decision": "merge",
                "confidence": 0.85,
                "branch": f"feature-{i}",
            },
        })
    return entries


@pytest.fixture
def synth(tmp_path):
    peers = {
        "mac2": {"ip": "mac2.fleet.example.com", "port": 9900},
        "mac3": {"ip": "mac3.fleet.example.com", "port": 9901},
    }
    return CrossNodeSynthesizer(
        synthesis_dir=str(tmp_path / "synth"),
        peers=peers,
        timeout_s=2,
    )


# ── Tests ───────────────────────────────────────────────────────────────

class TestResolveUrl:
    def test_resolves_known_peer(self, synth):
        url = synth._resolve_peer_url("mac2")
        assert url == "http://mac2.fleet.example.com:9900"

    def test_returns_none_for_unknown(self, synth):
        assert synth._resolve_peer_url("mac99") is None

    def test_returns_none_for_missing_ip(self, tmp_path):
        s = CrossNodeSynthesizer(
            synthesis_dir=str(tmp_path),
            peers={"mac2": {"port": 8855}},
        )
        assert s._resolve_peer_url("mac2") is None


class TestEntriesToNodeEvidence:
    def test_converts_entries_to_evidence(self):
        entries = _make_evidence_entries("mac2", count=3, task_id="t1")
        result = CrossNodeSynthesizer._entries_to_node_evidence("mac2", entries)
        assert len(result) == 1  # all same task_id -> grouped
        ne = result[0]
        assert ne.node_id == "mac2"
        assert ne.task_id == "t1"
        assert ne.decision == "merge"
        assert ne.confidence == 0.85
        assert len(ne.evidence_refs) == 3

    def test_filters_by_task_id(self):
        entries = (
            _make_evidence_entries("mac2", count=2, task_id="t1")
            + _make_evidence_entries("mac2", count=1, task_id="t2")
        )
        result = CrossNodeSynthesizer._entries_to_node_evidence("mac2", entries, task_id="t1")
        assert len(result) == 1
        assert result[0].task_id == "t1"

    def test_empty_entries_returns_empty(self):
        assert CrossNodeSynthesizer._entries_to_node_evidence("mac2", []) == []

    def test_probe_findings_extracted(self):
        entries = [{
            "entry_id": "e1",
            "timestamp": time.time(),
            "event_type": "scan_finding",
            "node_id": "mac3",
            "subject": "stale lock",
            "entry_hash": "h1",
            "payload": {
                "task_id": "t1",
                "severity": "high",
                "title": "stale lock detected",
            },
        }]
        result = CrossNodeSynthesizer._entries_to_node_evidence("mac3", entries)
        assert len(result) == 1
        assert "high: stale lock detected" in result[0].probe_findings

    def test_surgeon_objections_extracted(self):
        entries = [{
            "entry_id": "e1",
            "timestamp": time.time(),
            "event_type": "merge_decision",
            "node_id": "mac2",
            "subject": "merge x",
            "entry_hash": "h1",
            "payload": {
                "task_id": "t1",
                "decision": "merge",
                "confidence": 0.7,
                "objections": ["test coverage too low", "no review"],
            },
        }]
        result = CrossNodeSynthesizer._entries_to_node_evidence("mac2", entries)
        assert len(result[0].surgeon_objections) == 2


class TestFetchEvidence:
    def test_successful_fetch(self, synth):
        entries = _make_evidence_entries("mac2", count=2)
        response_data = json.dumps({"entries": entries, "count": 2}).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = synth._fetch_evidence_from_node("mac2", since=0)
            assert len(result) >= 1
            assert result[0].node_id == "mac2"
            # Verify URL constructed correctly
            call_args = mock_open.call_args
            req = call_args[0][0]
            assert "mac2.fleet.example.com:9900/evidence" in req.full_url

    def test_timeout_returns_empty(self, synth):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            result = synth._fetch_evidence_from_node("mac2")
            assert result == []

    def test_connection_refused_returns_empty(self, synth):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = synth._fetch_evidence_from_node("mac3")
            assert result == []

    def test_unknown_peer_returns_empty(self, synth):
        result = synth._fetch_evidence_from_node("mac99")
        assert result == []


class TestCollectRemoteEvidence:
    def test_collects_from_multiple_nodes(self, synth):
        mac2_entries = _make_evidence_entries("mac2", count=2, task_id="t1")
        mac3_entries = _make_evidence_entries("mac3", count=1, task_id="t1")

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            if "mac2.fleet.example.com" in url:
                data = {"entries": mac2_entries, "count": 2}
            elif "mac3.fleet.example.com" in url:
                data = {"entries": mac3_entries, "count": 1}
            else:
                raise ConnectionError("unknown")
            resp = MagicMock()
            resp.read.return_value = json.dumps(data).encode()
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = synth.collect_remote_evidence(["mac2", "mac3"], task_id="t1")
            assert len(result) == 2  # one NodeEvidence per node (grouped by task)
            node_ids = {ne.node_id for ne in result}
            assert node_ids == {"mac2", "mac3"}

    def test_skips_unreachable_nodes(self, synth):
        mac2_entries = _make_evidence_entries("mac2", count=1, task_id="t1")

        def fake_urlopen(req, timeout=None):
            if "mac2.fleet.example.com" in req.full_url:
                resp = MagicMock()
                resp.read.return_value = json.dumps(
                    {"entries": mac2_entries, "count": 1}
                ).encode()
                return resp
            raise TimeoutError("node offline")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = synth.collect_remote_evidence(["mac2", "mac3"])
            # mac3 timed out, only mac2 returned
            assert len(result) >= 1
            assert all(ne.node_id == "mac2" for ne in result)

    def test_empty_when_all_offline(self, synth):
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            result = synth.collect_remote_evidence(["mac2", "mac3"])
            assert result == []


class TestSynthesizeCrossNode:
    def test_end_to_end_with_local_and_remote(self, synth):
        remote_entries = _make_evidence_entries("mac2", count=2, task_id="t1")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"entries": remote_entries, "count": 2}
        ).encode()

        local = NodeEvidence(
            node_id="mac1", task_id="t1", decision="merge",
            confidence=0.9, evidence_refs=["ref1", "ref2"],
        )

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = synth.synthesize_cross_node(
                "t1", node_ids=["mac2"], local_evidence=[local]
            )
            assert isinstance(result, SynthesisResult)
            assert "mac1" in result.nodes_contributing
            assert "mac2" in result.nodes_contributing
            assert result.consensus_decision == "merge"
            assert result.consensus_confidence > 0

    def test_no_evidence_returns_empty_result(self, synth):
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            result = synth.synthesize_cross_node("t1", node_ids=["mac2"])
            assert result.synthesis_method == "no_evidence"
            assert result.task_id == "t1"

    def test_defaults_to_all_peers(self, synth):
        """When node_ids is None, queries all peers in config."""
        with patch.object(synth, "collect_remote_evidence", return_value=[]) as mock_collect:
            synth.synthesize_cross_node("t1")
            call_args = mock_collect.call_args
            # Should have passed all peer keys
            assert set(call_args[0][0]) == {"mac2", "mac3"}

    def test_persists_result(self, synth):
        local = NodeEvidence(
            node_id="mac1", task_id="persist-test", decision="reject",
            confidence=0.6,
        )
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            synth.synthesize_cross_node(
                "persist-test", node_ids=[], local_evidence=[local]
            )
            # Verify saved to disk
            saved = synth.get_latest("persist-test")
            assert saved is not None
            assert saved.consensus_decision == "reject"
