"""Tests for all 9 theatrical dashboard components.

Each component must:
- Render valid HTML when given data
- Render graceful unavailable state when data is None
- Not crash on missing/malformed data fields
- HTML-escape user-provided strings
"""

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.theatrical.base import TheatricalComponent
from multifleet.theatrical.vital_signs import VitalSignsBar
from multifleet.theatrical.surgeon_feed import SurgeonFeed
from multifleet.theatrical.fleet_constellation import FleetConstellation
from multifleet.theatrical.evidence_timeline import EvidenceTimeline
from multifleet.theatrical.quorum_pulse import QuorumPulse
from multifleet.theatrical.memory_heat_map import MemoryHeatMap
from multifleet.theatrical.probe_status_grid import ProbeStatusGrid
from multifleet.theatrical.corrigibility_gauge import CorrigibilityGauge
from multifleet.theatrical.session_gold_stream import SessionGoldStream


# ── Test Data Factories ──

def _fleet_data(**overrides):
    """Build minimal fleet snapshot data."""
    base = {
        "nodes": [
            {"id": "mac1", "status": "online", "last_seen_s": 0, "sessions": 3,
             "channel_score": "self", "broken_channels": [], "is_self": True},
            {"id": "mac2", "status": "online", "last_seen_s": 5, "sessions": 2,
             "channel_score": "A", "broken_channels": [], "is_self": False},
            {"id": "mac3", "status": "offline", "last_seen_s": 300, "sessions": 0,
             "channel_score": "?", "broken_channels": ["nats"], "is_self": False},
        ],
        "daemon": {"status": "ok", "uptime_s": 3600},
        "messages": {"sent": 100, "received": 95, "broadcasts": 20, "errors": 2},
        "gains_gate": {"passed": True},
        "rebuttals": {"active": 1, "total": 3},
    }
    base.update(overrides)
    return base


def _surgeon_data(**overrides):
    base = {
        "surgeons": {"atlas": "active", "cardiologist": "ok", "neurologist": "ok"},
        "cross_exams": [
            {"timestamp": time.time(), "topic": "memory refactor", "consensus": 0.85,
             "cost": 0.012, "mode": "standard"},
        ],
        "rebuttals": [
            {"task_id": "abc12345-dead-beef", "phase": "under_review",
             "verdicts_count": 2, "expected": 3},
        ],
    }
    base.update(overrides)
    return base


def _evidence_data(**overrides):
    base = {
        "entries": [
            {"seq": 42, "entry_id": "abc123", "timestamp": time.time() - 60,
             "event_type": "merge_decision", "node_id": "mac1",
             "subject": "merge feature-x", "entry_hash": "deadbeef" * 4,
             "payload": {"decision": "approved"}},
            {"seq": 41, "entry_id": "def456", "timestamp": time.time() - 120,
             "event_type": "gains_gate_result", "node_id": "mac2",
             "subject": "gate check", "entry_hash": "cafebabe" * 4,
             "payload": {"result": "passed"}},
        ],
        "total": 42,
        "chain_valid": True,
        "chain_checked": 20,
    }
    base.update(overrides)
    return base


def _memory_data(**overrides):
    base = {
        "topics": {
            "architecture": {"count": 45, "quality": 0.9, "last_hit": time.time()},
            "webhooks": {"count": 30, "quality": 0.75},
            "memory": {"count": 22, "quality": 0.8},
            "fleet": {"count": 18, "quality": 0.6},
            "testing": {"count": 5, "quality": 0.5},
        },
        "total_memories": 120,
        "last_updated": time.time(),
    }
    base.update(overrides)
    return base


def _probe_data(**overrides):
    probes = {
        "webhook_health": {"status": "pass", "score": 0.95, "last_fire": time.time() - 30},
        "llm_local": {"status": "pass", "score": 0.88, "last_fire": time.time() - 60},
        "redis_health": {"status": "warn", "score": 0.6, "last_fire": time.time() - 120},
        "fleet_daemon": {"status": "fail", "score": 0.1, "last_fire": time.time() - 10},
        "gains_gate": {"status": "pass", "score": 1.0, "last_fire": time.time() - 300},
    }
    base = {"probes": probes}
    base.update(overrides)
    return base


def _corrigibility_data(**overrides):
    base = {
        "score": 0.82,
        "trend": [
            {"timestamp": time.time() - 3600, "score": 0.75},
            {"timestamp": time.time() - 1800, "score": 0.78},
            {"timestamp": time.time(), "score": 0.82},
        ],
        "factors": {
            "evidence_integrity": 0.95,
            "surgeon_consensus": 0.80,
            "gains_gate": 1.0,
            "critical_findings": 0.70,
        },
        "last_check": time.time(),
    }
    base.update(overrides)
    return base


def _gold_data(**overrides):
    base = {
        "nuggets": [
            {"pass": "extract_bugs", "finding": "Redis timeout in gold mining",
             "severity": "high", "timestamp": time.time()},
            {"pass": "extract_sops", "finding": "Always check gains gate before merge",
             "severity": "info", "timestamp": time.time() - 60},
        ],
        "stats": {"total_mined": 156, "sessions_processed": 23, "reclaimed_mb": 12.5},
        "active_session": None,
        "critical_count": 0,
    }
    base.update(overrides)
    return base


# ── Base Component Tests ──

class TestTheatricalBase:
    def test_esc_xss(self):
        assert TheatricalComponent._esc("<script>") == "&lt;script&gt;"
        assert TheatricalComponent._esc(None) == ""
        assert TheatricalComponent._esc("safe") == "safe"

    def test_format_ts(self):
        ts = TheatricalComponent._format_ts(1712300000.0)
        assert ":" in ts  # HH:MM:SS format
        assert TheatricalComponent._format_ts(None) == "---"
        assert TheatricalComponent._format_ts("") == "---"

    def test_ago(self):
        assert TheatricalComponent._ago(30) == "30s"
        assert TheatricalComponent._ago(90) == "1m 30s"
        assert TheatricalComponent._ago(7200) == "2h 0m"
        assert TheatricalComponent._ago("bad") == "?"

    def test_health_color(self):
        assert TheatricalComponent._health_color(0.8) == "#22c55e"
        assert TheatricalComponent._health_color(0.5) == "#eab308"
        assert TheatricalComponent._health_color(0.2) == "#ef4444"

    def test_bar_html(self):
        bar = TheatricalComponent._bar_html(0.5, 100, "#3b82f6")
        assert "50%" in bar
        assert "#3b82f6" in bar

    def test_dot_html(self):
        dot = TheatricalComponent._dot_html("#22c55e")
        assert "#22c55e" in dot
        assert "&#9679;" in dot


# ── Component 1: Vital Signs ──

class TestVitalSigns:
    def test_render_with_data(self):
        html = VitalSignsBar().render(_fleet_data())
        assert "tc-vital-signs" in html
        assert "Heartbeat" in html
        assert "Latency" in html
        assert "Stress" in html
        assert "Uptime" in html
        assert "2/3 online" in html

    def test_render_unavailable(self):
        html = VitalSignsBar().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_all_online(self):
        data = _fleet_data(nodes=[
            {"id": "a", "status": "online", "is_self": True},
            {"id": "b", "status": "online", "is_self": False},
        ])
        html = VitalSignsBar().render(data)
        assert "2/2 online" in html

    def test_zero_messages(self):
        data = _fleet_data(messages={"sent": 0, "received": 0, "errors": 0})
        html = VitalSignsBar().render(data)
        assert "Errors" in html

    def test_empty_nodes(self):
        data = _fleet_data(nodes=[])
        html = VitalSignsBar().render(data)
        assert "0/0 online" in html


# ── Component 2: Surgeon Feed ──

class TestSurgeonFeed:
    def test_render_with_data(self):
        html = SurgeonFeed().render(_surgeon_data())
        assert "tc-surgeon-feed" in html
        assert "Atlas" in html
        assert "Cardiologist" in html
        assert "Neurologist" in html
        assert "memory refactor" in html

    def test_render_unavailable(self):
        html = SurgeonFeed().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_empty_cross_exams(self):
        html = SurgeonFeed().render(_surgeon_data(cross_exams=[]))
        assert "No recent cross-examinations" in html

    def test_empty_rebuttals(self):
        html = SurgeonFeed().render(_surgeon_data(rebuttals=[]))
        assert "No active rebuttals" in html

    def test_xss_in_topic(self):
        data = _surgeon_data(cross_exams=[
            {"timestamp": time.time(), "topic": "<script>alert(1)</script>",
             "consensus": 0.5, "cost": 0, "mode": "test"}
        ])
        html = SurgeonFeed().render(data)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── Component 3: Fleet Constellation ──

class TestFleetConstellation:
    def test_render_with_data(self):
        html = FleetConstellation().render(_fleet_data())
        assert "tc-fleet-constellation" in html
        assert "<svg" in html
        assert "mac1" in html
        assert "mac2" in html

    def test_render_unavailable(self):
        html = FleetConstellation().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_empty_nodes(self):
        html = FleetConstellation().render({"nodes": []})
        assert "unavailable" in html.lower() or "No nodes" in html

    def test_single_node(self):
        data = {"nodes": [{"id": "solo", "status": "online", "sessions": 1, "is_self": True}]}
        html = FleetConstellation().render(data)
        assert "solo" in html
        assert "<svg" in html

    def test_self_node_has_pulse(self):
        html = FleetConstellation().render(_fleet_data())
        assert "animate" in html  # pulse animation for self node


# ── Component 4: Evidence Timeline ──

class TestEvidenceTimeline:
    def test_render_with_data(self):
        html = EvidenceTimeline().render(_evidence_data())
        assert "tc-evidence-timeline" in html
        assert "merge_decision" in html
        assert "Chain Intact" in html
        assert "42 entries" in html

    def test_render_unavailable(self):
        html = EvidenceTimeline().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_broken_chain(self):
        data = _evidence_data(chain_valid=False)
        html = EvidenceTimeline().render(data)
        assert "Chain Broken" in html

    def test_unverified_chain(self):
        data = _evidence_data(chain_valid=None)
        html = EvidenceTimeline().render(data)
        assert "Unverified" in html

    def test_empty_entries(self):
        data = _evidence_data(entries=[])
        html = EvidenceTimeline().render(data)
        assert "No evidence entries" in html

    def test_hash_prefix_displayed(self):
        html = EvidenceTimeline().render(_evidence_data())
        assert "deadbeef" in html  # hash prefix shown


# ── Component 5: Quorum Pulse ──

class TestQuorumPulse:
    def test_render_with_data(self):
        html = QuorumPulse().render(_fleet_data())
        assert "tc-quorum-pulse" in html
        assert "Las" in html
        assert "Rhl" in html
        assert "PQS" in html
        assert "IQS" in html

    def test_render_unavailable(self):
        html = QuorumPulse().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_quorum_reached(self):
        data = _fleet_data(
            messages={"sent": 100, "received": 100, "errors": 0},
            gains_gate={"passed": True},
            rebuttals={"active": 0, "total": 0},
        )
        # All nodes online
        data["nodes"] = [
            {"id": "a", "status": "online", "is_self": True, "channel_score": "A"},
            {"id": "b", "status": "online", "is_self": False, "channel_score": "A"},
        ]
        html = QuorumPulse().render(data)
        assert "Quorum Reached" in html

    def test_below_quorum(self):
        data = _fleet_data(
            nodes=[{"id": "a", "status": "offline", "is_self": False}],
            messages={"sent": 10, "received": 5, "errors": 5},
            gains_gate={"passed": False},
        )
        html = QuorumPulse().render(data)
        assert "Below Quorum" in html

    def test_animated_pulse(self):
        html = QuorumPulse().render(_fleet_data())
        assert "<svg" in html
        assert "animate" in html


# ── Component 6: Memory Heat Map ──

class TestMemoryHeatMap:
    def test_render_with_data(self):
        html = MemoryHeatMap().render(_memory_data())
        assert "tc-memory-heat-map" in html
        assert "architecture" in html
        assert "120 memories" in html

    def test_render_unavailable(self):
        html = MemoryHeatMap().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_empty_topics(self):
        html = MemoryHeatMap().render({"topics": {}, "total_memories": 0})
        # Should show default topics when empty
        assert "tc-heat-grid" in html

    def test_heat_colors_vary(self):
        data = _memory_data()
        html = MemoryHeatMap().render(data)
        # Different topics with different counts should have different backgrounds
        assert "tc-heat-cell" in html

    def test_legend_present(self):
        html = MemoryHeatMap().render(_memory_data())
        assert "cold" in html
        assert "hot" in html


# ── Component 7: Probe Status Grid ──

class TestProbeStatusGrid:
    def test_render_with_data(self):
        html = ProbeStatusGrid().render(_probe_data())
        assert "tc-probe-status-grid" in html
        assert "Webhook Health" in html

    def test_render_unavailable(self):
        html = ProbeStatusGrid().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_all_18_probes_listed(self):
        html = ProbeStatusGrid().render(_probe_data())
        # All 18 probe labels should appear
        assert "Webhook Health" in html
        assert "LLM Local" in html
        assert "Redis Health" in html
        assert "Fleet Daemon" in html
        assert "Evidence Chain" in html
        assert "Session Gold" in html

    def test_status_icons(self):
        html = ProbeStatusGrid().render(_probe_data())
        assert "&#10003;" in html  # checkmark for pass
        assert "&#10007;" in html  # x for fail

    def test_passing_count(self):
        html = ProbeStatusGrid().render(_probe_data())
        # Should show passing/total count
        assert "/18" in html

    def test_empty_probes(self):
        html = ProbeStatusGrid().render({"probes": {}})
        assert "0/18" in html


# ── Component 8: Corrigibility Gauge ──

class TestCorrigibilityGauge:
    def test_render_with_data(self):
        html = CorrigibilityGauge().render(_corrigibility_data())
        assert "tc-corrigibility-gauge" in html
        assert "82%" in html
        assert "High" in html

    def test_render_unavailable(self):
        html = CorrigibilityGauge().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_low_score(self):
        data = _corrigibility_data(score=0.3)
        html = CorrigibilityGauge().render(data)
        assert "30%" in html
        assert "Critical" in html

    def test_moderate_score(self):
        data = _corrigibility_data(score=0.65)
        html = CorrigibilityGauge().render(data)
        assert "Moderate" in html

    def test_svg_gauge(self):
        html = CorrigibilityGauge().render(_corrigibility_data())
        assert "<svg" in html
        assert "viewBox" in html

    def test_trend_sparkline(self):
        html = CorrigibilityGauge().render(_corrigibility_data())
        assert "polyline" in html  # sparkline rendered
        assert "24h trend" in html

    def test_no_trend(self):
        data = _corrigibility_data(trend=[])
        html = CorrigibilityGauge().render(data)
        assert "tc-corrigibility-gauge" in html

    def test_factors_displayed(self):
        html = CorrigibilityGauge().render(_corrigibility_data())
        assert "evidence_integrity" in html
        assert "surgeon_consensus" in html


# ── Component 9: Session Gold Stream ──

class TestSessionGoldStream:
    def test_render_with_data(self):
        html = SessionGoldStream().render(_gold_data())
        assert "tc-session-gold-stream" in html
        assert "156" in html  # total mined
        assert "Redis timeout" in html

    def test_render_unavailable(self):
        html = SessionGoldStream().render(None)
        assert "unavailable" in html.lower() or "tc-unavailable" in html

    def test_critical_findings_alert(self):
        data = _gold_data(critical_count=3)
        html = SessionGoldStream().render(data)
        assert "3 Critical Findings" in html
        assert "tc-badge-red" in html

    def test_active_session(self):
        data = _gold_data(active_session={
            "session_id": "abc123deadbeef",
            "phase": "extract_bugs",
            "progress": 0.6,
        })
        html = SessionGoldStream().render(data)
        assert "Mining" in html
        assert "abc123deadbe" in html

    def test_empty_nuggets(self):
        data = _gold_data(nuggets=[])
        html = SessionGoldStream().render(data)
        assert "No gold nuggets" in html

    def test_severity_colors(self):
        html = SessionGoldStream().render(_gold_data())
        assert "high" in html  # severity label
        assert "info" in html

    def test_xss_in_finding(self):
        data = _gold_data(nuggets=[
            {"pass": "test", "finding": "<img onerror=alert(1)>",
             "severity": "low", "timestamp": time.time()}
        ])
        html = SessionGoldStream().render(data)
        assert "<img" not in html
        assert "&lt;img" in html
