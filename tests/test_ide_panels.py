"""Tests for ide_panels module — IDE panel server endpoints."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure multifleet is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.ide_panels import (
    IDEPanelServer,
    _panel_envelope,
    _payload_summary,
    _read_decisions_jsonl,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_health_response(**overrides):
    """Build a mock daemon /health response."""
    base = {
        "status": "ok",
        "node_id": "mac1",
        "nats": {"connected": True},
        "branch": "main",
        "sessions": 2,
        "peers": {
            "mac2": {"status": "online", "sessions": 1, "last_seen": 1712300000.0},
            "mac3": {"status": "offline", "sessions": 0, "last_seen": 1712290000.0},
        },
    }
    base.update(overrides)
    return base


class FakeEvidenceLedger:
    """Minimal evidence ledger stub for testing."""

    def __init__(self, entries=None, chain_valid=True):
        self._entries = entries or []
        self._chain_valid = chain_valid

    def query(self, event_type=None, node_id=None, since=None, limit=50):
        results = self._entries
        if since:
            results = [e for e in results if e.get("timestamp", 0) >= since]
        return results[:limit]

    def verify_chain(self, limit=50):
        return {"valid": self._chain_valid, "checked": limit, "broken_at": None}


class FakeProbeResult:
    """Minimal probe result stub."""

    def __init__(self, probe_id="test-probe", category="quality",
                 status="ok", findings=None):
        self.probe_id = probe_id
        self.category = category
        self.status = status
        self.confidence = 0.9
        self.severity = "low"
        self.findings = findings or []
        self.missing_inputs = []
        self.duration_ms = 42.0
        self.generated_at = "2026-04-10T00:00:00Z"

    def to_dict(self):
        return {
            "probe_id": self.probe_id,
            "category": self.category,
            "status": self.status,
            "confidence": self.confidence,
            "severity": self.severity,
            "findings": [f.to_dict() if hasattr(f, "to_dict") else f
                         for f in self.findings],
            "missing_inputs": self.missing_inputs,
            "duration_ms": self.duration_ms,
            "generated_at": self.generated_at,
        }


class FakeProbeEngine:
    """Minimal probe engine stub."""

    def __init__(self, results=None):
        self._results = results or []

    def latest_results(self):
        return self._results

    def findings_summary(self):
        return {
            "total_probes": len(self._results),
            "total_findings": sum(len(r.findings) for r in self._results),
            "has_blocking": False,
            "surgeon_intensity": "light",
            "by_severity": {},
            "by_category": {},
            "by_status": {},
        }


class FakeProfessorBridge:
    """Minimal professor bridge stub."""

    def get_panel_data(self):
        return {
            "recent_wisdom": [
                {
                    "topic": "redis locking",
                    "the_one_thing": "Use SETNX with TTL",
                    "landmine_count": 2,
                    "queried_at": time.time(),
                    "expired": False,
                },
            ],
            "active_gotchas": [
                {
                    "topic": "redis locking",
                    "gotcha": "Never use WATCH in hot paths",
                    "queried_at": time.time(),
                },
            ],
            "architecture_notes": [],
            "stats": {"total_queries": 5, "cache_hits": 3,
                      "cache_size": 2, "cache_hit_rate": 0.6},
        }


# ── Panel envelope tests ────────────────────────────────────────────


def test_panel_envelope_shape():
    result = _panel_envelope("test-id", "Test Title", [1, 2, 3], "3 items")
    assert result["panel_id"] == "test-id"
    assert result["title"] == "Test Title"
    assert result["data"] == [1, 2, 3]
    assert result["summary"] == "3 items"
    assert "updated_at" in result
    # ISO format check
    assert result["updated_at"].endswith("Z")
    assert "T" in result["updated_at"]


def test_panel_envelope_empty_data():
    result = _panel_envelope("empty", "Empty", [], "nothing")
    assert result["data"] == []


# ── Fleet Status Panel ───────────────────────────────────────────────


def test_fleet_status_panel_with_health():
    server = IDEPanelServer()
    health = _make_health_response()

    with patch("multifleet.ide_panels._fetch_daemon", return_value=health):
        result = server.fleet_status_panel()

    assert result["panel_id"] == "fleet-status"
    assert result["title"] == "Fleet Status"
    assert len(result["data"]) == 3  # mac1 + mac2 + mac3

    # Self node
    self_node = result["data"][0]
    assert self_node["id"] == "mac1"
    assert self_node["role"] == "self"
    assert self_node["nats_connected"] is True

    # Peer nodes
    peer_ids = {n["id"] for n in result["data"] if n["role"] == "peer"}
    assert peer_ids == {"mac2", "mac3"}

    assert "3" in result["summary"]  # total count


def test_fleet_status_panel_daemon_unreachable():
    server = IDEPanelServer()

    with patch("multifleet.ide_panels._fetch_daemon", return_value=None):
        result = server.fleet_status_panel()

    assert result["panel_id"] == "fleet-status"
    assert result["data"] == []
    assert "unreachable" in result["summary"].lower()


def test_fleet_status_panel_peers_as_list():
    """Daemon may return peers as a list of names instead of dict."""
    server = IDEPanelServer()
    health = _make_health_response(peers=["mac2", "mac3"])

    with patch("multifleet.ide_panels._fetch_daemon", return_value=health):
        result = server.fleet_status_panel()

    assert len(result["data"]) == 3  # self + 2 peers


# ── Evidence Timeline Panel ──────────────────────────────────────────


def test_evidence_timeline_panel_with_entries():
    entries = [
        {
            "seq": 1, "entry_id": "ev-001",
            "timestamp": time.time() - 100,
            "event_type": "synthesis", "node_id": "mac1",
            "subject": "task-42", "entry_hash": "abcdef0123456789abcdef",
            "prev_hash": "genesis0000000000000000",
            "verified": 1,
            "payload": json.dumps({"reasoning": "Chose branch A"}),
        },
        {
            "seq": 2, "entry_id": "ev-002",
            "timestamp": time.time() - 50,
            "event_type": "probe", "node_id": "mac2",
            "subject": "quality-check", "entry_hash": "fedcba9876543210fedcba",
            "prev_hash": "abcdef0123456789abcdef",
            "verified": 1,
            "payload": json.dumps({"summary": "All probes green"}),
        },
    ]
    ledger = FakeEvidenceLedger(entries=entries, chain_valid=True)
    server = IDEPanelServer(evidence_ledger=ledger)

    result = server.evidence_timeline_panel(hours=1)

    assert result["panel_id"] == "evidence-timeline"
    assert len(result["data"]) == 2
    assert result["data"][0]["entry_hash"] == "abcdef0123456789"  # truncated to 16
    assert "intact" in result["summary"]
    assert result["data"][1]["payload_summary"] == "All probes green"


def test_evidence_timeline_panel_broken_chain():
    ledger = FakeEvidenceLedger(
        entries=[{"seq": 1, "entry_id": "ev-001", "timestamp": time.time(),
                  "event_type": "test", "node_id": "mac1", "subject": "x",
                  "entry_hash": "a" * 32, "prev_hash": "b" * 32,
                  "verified": 1, "payload": "{}"}],
        chain_valid=False,
    )
    server = IDEPanelServer(evidence_ledger=ledger)
    result = server.evidence_timeline_panel(hours=1)
    assert "BROKEN" in result["summary"]


def test_evidence_timeline_panel_no_ledger():
    server = IDEPanelServer()
    # Don't inject a ledger, and mock the import to fail
    server._evidence_ledger = None
    with patch.dict("sys.modules", {"multifleet.evidence_ledger": None}):
        # Force re-init
        server._evidence_ledger = None
        result = server.evidence_timeline_panel()
    assert result["data"] == []
    assert "unavailable" in result["summary"].lower()


# ── Synthesis Decisions Panel ────────────────────────────────────────


def test_synthesis_decisions_panel_from_jsonl(tmp_path):
    """Read decisions from a JSONL ledger file."""
    decisions = [
        {"timestamp": time.time() - 200, "task_id": "task-1",
         "decision": "merge", "winner_branch": "mac1/feat",
         "confidence": 0.85, "reasoning": "High agreement",
         "cherry_pick_from": [], "dissent_count": 0, "duration_ms": 42},
        {"timestamp": time.time() - 100, "task_id": "task-2",
         "decision": "cherry_pick", "winner_branch": "mac2/fix",
         "confidence": 0.65, "reasoning": "Partial agreement",
         "cherry_pick_from": [{"machine": "mac1", "commit": "abc123"}],
         "dissent_count": 1, "duration_ms": 88},
    ]
    ledger_file = tmp_path / "decisions.jsonl"
    ledger_file.write_text("\n".join(json.dumps(d) for d in decisions))

    # Create a mock synthesis with the right ledger path
    mock_synthesis = MagicMock()
    mock_synthesis._ledger_path = ledger_file

    server = IDEPanelServer(synthesis=mock_synthesis)
    result = server.synthesis_decisions_panel(limit=10)

    assert result["panel_id"] == "synthesis-decisions"
    assert len(result["data"]) == 2
    # Newest first
    assert result["data"][0]["task_id"] == "task-2"
    assert "75%" in result["summary"]  # avg of 0.85 and 0.65


def test_synthesis_decisions_panel_empty(tmp_path):
    ledger_file = tmp_path / "decisions.jsonl"
    # Don't create the file
    mock_synthesis = MagicMock()
    mock_synthesis._ledger_path = ledger_file

    server = IDEPanelServer(synthesis=mock_synthesis)
    result = server.synthesis_decisions_panel()
    assert result["data"] == []
    assert "no decisions" in result["summary"].lower()


# ── Probe Findings Panel ────────────────────────────────────────────


def test_probe_findings_panel_with_results():
    results = [
        FakeProbeResult(probe_id="quality-001", status="ok"),
        FakeProbeResult(probe_id="security-001", status="warning"),
    ]
    engine = FakeProbeEngine(results=results)
    server = IDEPanelServer(probe_engine=engine)

    result = server.probe_findings_panel()

    assert result["panel_id"] == "probe-findings"
    assert len(result["data"]) == 2
    assert result["data"][0]["probe_id"] == "quality-001"
    assert "2 probes" in result["summary"]
    assert "intensity: light" in result["summary"]


def test_probe_findings_panel_no_engine():
    server = IDEPanelServer()
    server._probe_engine = None
    with patch.dict("sys.modules", {"multifleet.probe_engine": None}):
        server._probe_engine = None
        result = server.probe_findings_panel()
    assert result["data"] == []
    assert "unavailable" in result["summary"].lower()


# ── Wisdom Feedback Panel ────────────────────────────────────────────


def test_wisdom_feedback_panel_with_professor():
    prof = FakeProfessorBridge()
    server = IDEPanelServer(professor_bridge=prof)

    result = server.wisdom_feedback_panel()

    assert result["panel_id"] == "wisdom-feedback"
    # 1 wisdom + 1 gotcha from the fake
    wisdom_items = [d for d in result["data"] if d["type"] == "wisdom"]
    gotcha_items = [d for d in result["data"] if d["type"] == "gotcha"]
    assert len(wisdom_items) == 1
    assert len(gotcha_items) == 1
    assert wisdom_items[0]["topic"] == "redis locking"
    assert gotcha_items[0]["gotcha"] == "Never use WATCH in hot paths"
    assert "1 wisdom" in result["summary"]
    assert "1 gotchas" in result["summary"]


def test_wisdom_feedback_panel_empty():
    server = IDEPanelServer()
    # No professor, no wisdom consumer
    server._professor_bridge = None
    server._wisdom_consumer = None
    # Prevent lazy init from succeeding
    with patch.object(server, "_get_professor_bridge", return_value=None), \
         patch.object(server, "_get_wisdom_consumer", return_value=None):
        result = server.wisdom_feedback_panel()
    assert result["data"] == []
    assert "no wisdom" in result["summary"].lower()


# ── All Panels ───────────────────────────────────────────────────────


def test_all_panels_returns_five():
    server = IDEPanelServer()
    with patch("multifleet.ide_panels._fetch_daemon", return_value=None), \
         patch.object(server, "_get_evidence_ledger", return_value=None), \
         patch.object(server, "_get_synthesis", return_value=None), \
         patch.object(server, "_get_probe_engine", return_value=None), \
         patch.object(server, "_get_professor_bridge", return_value=None), \
         patch.object(server, "_get_wisdom_consumer", return_value=None):
        result = server.all_panels()

    assert "panels" in result
    assert len(result["panels"]) == 5
    assert "generated_at" in result

    panel_ids = {p["panel_id"] for p in result["panels"]}
    assert panel_ids == {
        "fleet-status", "evidence-timeline", "synthesis-decisions",
        "probe-findings", "wisdom-feedback",
    }


# ── Utility function tests ──────────────────────────────────────────


def test_payload_summary_extracts_reasoning():
    assert _payload_summary({"reasoning": "Branch A wins"}) == "Branch A wins"


def test_payload_summary_truncates():
    long_text = "x" * 200
    result = _payload_summary({"summary": long_text})
    assert len(result) == 80


def test_payload_summary_empty():
    assert _payload_summary({}) == ""
    assert _payload_summary(None) == ""


def test_read_decisions_jsonl_tail(tmp_path):
    """Reads last N lines from JSONL."""
    path = tmp_path / "test.jsonl"
    lines = [json.dumps({"id": i}) for i in range(30)]
    path.write_text("\n".join(lines))

    result = _read_decisions_jsonl(path, limit=5)
    assert len(result) == 5
    # Newest first (id=29, 28, 27, 26, 25)
    assert result[0]["id"] == 29
    assert result[4]["id"] == 25


def test_read_decisions_jsonl_missing_file(tmp_path):
    result = _read_decisions_jsonl(tmp_path / "nonexistent.jsonl")
    assert result == []
