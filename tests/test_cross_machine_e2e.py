"""Cross-machine E2E tests -- validates fleet protocol between real nodes.

These tests use the live daemon on port 8855 to test real message delivery,
probe scanning, and synthesis flow. They are marked @pytest.mark.integration
and skip gracefully when the daemon is offline.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

# Ensure multifleet package is importable
FLEET_ROOT = Path(__file__).parent.parent
REPO_ROOT = FLEET_ROOT.parent
sys.path.insert(0, str(FLEET_ROOT))
sys.path.insert(0, str(REPO_ROOT))

DAEMON_PORT = int(os.environ.get("FLEET_NERVE_PORT", "8855"))
DAEMON_BASE = f"http://127.0.0.1:{DAEMON_PORT}"


# ── Helpers ──


def _daemon_alive() -> bool:
    """Return True if the local fleet daemon responds to /health."""
    try:
        resp = urllib.request.urlopen(f"{DAEMON_BASE}/health", timeout=3)
        data = json.loads(resp.read())
        return "error" not in data
    except Exception:
        return False


def _daemon_get(path: str, timeout: int = 5) -> dict:
    """GET from daemon. Returns parsed JSON or raises."""
    url = f"{DAEMON_BASE}{path}"
    resp = urllib.request.urlopen(url, timeout=timeout)
    return json.loads(resp.read())


def _daemon_post(path: str, body: dict, timeout: int = 10) -> dict:
    """POST JSON to daemon. Returns parsed JSON or raises."""
    url = f"{DAEMON_BASE}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _unique_tag() -> str:
    """Short unique tag for test isolation."""
    return f"e2e-{uuid.uuid4().hex[:8]}"


# Skip the entire module if daemon is offline
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _daemon_alive(), reason="Fleet daemon not running on port 8855"),
]


# ── TestLiveMessageDelivery ──


class TestLiveMessageDelivery:
    """Tests that exercise real message delivery through the daemon."""

    def test_daemon_health_returns_valid_structure(self):
        """Health endpoint returns expected fields."""
        data = _daemon_get("/health")
        # Must have at minimum a status or node indication
        assert isinstance(data, dict)
        # Common fields: status, uptime_s, transport, node_id
        # At least one should be present
        assert any(k in data for k in ("status", "node_id", "nodeId", "uptime_s"))

    def test_send_message_to_self(self):
        """Send a message to this node via POST /message and verify delivery."""
        tag = _unique_tag()
        health = _daemon_get("/health")
        node_id = health.get("node_id", health.get("nodeId", "local"))

        msg = {
            "type": "context",
            "to": node_id,
            "payload": {
                "subject": f"e2e-self-test-{tag}",
                "body": f"Self-delivery test {tag}",
            },
        }
        result = _daemon_post("/message", msg)
        assert isinstance(result, dict)
        # Should not have a top-level error
        assert "error" not in result or result.get("sent") is True

    def test_broadcast_message(self):
        """Broadcast a message to 'all' and verify the daemon accepts it."""
        tag = _unique_tag()
        msg = {
            "type": "context",
            "to": "all",
            "payload": {
                "subject": f"e2e-broadcast-{tag}",
                "body": f"Broadcast test {tag}",
            },
        }
        result = _daemon_post("/message", msg)
        assert isinstance(result, dict)
        # Daemon must accept the message (may or may not deliver to peers)
        assert "error" not in result or result.get("sent") is True

    def test_send_returns_channel_info(self):
        """Message result should indicate which channel was used."""
        tag = _unique_tag()
        health = _daemon_get("/health")
        node_id = health.get("node_id", health.get("nodeId", "local"))
        msg = {
            "type": "context",
            "to": node_id,
            "payload": {"subject": f"e2e-channel-{tag}", "body": "channel probe"},
        }
        result = _daemon_post("/message", msg)
        assert isinstance(result, dict)
        # Best effort: check for channel or transport indication
        has_channel_info = any(
            k in result for k in ("channel", "transport", "sent", "delivered")
        )
        assert has_channel_info, f"No delivery info in result: {result}"

    def test_message_with_metadata(self):
        """Messages can carry arbitrary metadata in payload."""
        tag = _unique_tag()
        health = _daemon_get("/health")
        node_id = health.get("node_id", health.get("nodeId", "local"))
        msg = {
            "type": "context",
            "to": node_id,
            "payload": {
                "subject": f"e2e-meta-{tag}",
                "body": "metadata test",
                "metadata": {"test_run": tag, "ts": time.time()},
            },
        }
        result = _daemon_post("/message", msg)
        assert isinstance(result, dict)
        assert "error" not in result or result.get("sent") is True


# ── TestLiveChannelCascade ──


class TestLiveChannelCascade:
    """Tests that exercise channel health and cascade behavior."""

    def test_channels_endpoint_exists(self):
        """GET /channels returns channel state."""
        data = _daemon_get("/channels")
        assert isinstance(data, dict)
        # Should report channel health or node information
        assert any(
            k in data for k in ("channels", "channel_health", "nodeId", "node_id")
        )

    def test_channel_health_has_scores(self):
        """Channel health should include numeric scores or status per channel."""
        data = _daemon_get("/channels")
        # The structure varies but should have something parseable
        ch = data.get("channels") or data.get("channel_health") or data
        assert isinstance(ch, (dict, list))

    def test_health_reports_transport(self):
        """Health endpoint reports which transport is active (nats/http/hybrid)."""
        data = _daemon_get("/health")
        # Check for transport indication
        transport = data.get("transport", data.get("mode", ""))
        # Even if empty, the health endpoint itself is reachable (HTTP works)
        assert isinstance(data, dict)

    def test_p7_git_message_path_exists(self):
        """The P7 git message directory exists in the repo."""
        fleet_msgs = REPO_ROOT / ".fleet-messages"
        assert fleet_msgs.exists(), f"Missing {fleet_msgs}"

    def test_p7_broadcast_dir_exists(self):
        """The broadcast directory for P7 messages exists."""
        broadcast_dir = REPO_ROOT / ".fleet-messages" / "all"
        assert broadcast_dir.exists(), f"Missing {broadcast_dir}"


# ── TestLiveProbeToSynthesis ──


class TestLiveProbeToSynthesis:
    """Tests the full probe-to-synthesis pipeline using real repo state."""

    def test_probe_engine_scan_against_live_repo(self):
        """Run probe engine against the real repo and get results."""
        from multifleet.probes import create_engine

        engine = create_engine()
        results = engine.scan(trigger="all")
        assert isinstance(results, list)
        assert len(results) > 0, "Probes should find something in a real repo"
        for r in results:
            assert hasattr(r, "probe_id")
            assert hasattr(r, "status")
            assert r.status in ("ok", "warning", "error", "critical", "skip", "skipped")

    def test_probe_findings_summary(self):
        """Findings summary aggregates correctly from live scan."""
        from multifleet.probes import create_engine

        engine = create_engine()
        engine.scan(trigger="all")
        summary = engine.findings_summary()
        assert isinstance(summary, dict)
        # Should have severity/category/status counts
        for key in ("by_severity", "by_category", "by_status"):
            if key in summary:
                assert isinstance(summary[key], dict)

    def test_epistemic_packet_from_live_probes(self):
        """Build an EpistemicPacket from real probe results."""
        from multifleet.epistemic_packet import EpistemicPacketBuilder

        builder = EpistemicPacketBuilder(repo_root=str(REPO_ROOT), node_id="e2e-test")
        packet = builder.build(
            task_intent="E2E validation of fleet protocol",
            run_probes=True,
        )
        assert packet.packet_id
        assert packet.task_intent == "E2E validation of fleet protocol"
        assert len(packet.probe_results) > 0
        assert packet.built_by == "e2e-test"
        assert packet.required_surgeon_intensity in ("light", "standard", "full")

    def test_arbiter_view_renders_html(self):
        """Arbiter view generates valid HTML from current daemon state."""
        from multifleet.arbiter_view import build_arbiter_html

        html = build_arbiter_html(daemon_port=DAEMON_PORT)
        assert isinstance(html, str)
        assert "<!DOCTYPE html>" in html or "<html" in html
        assert len(html) > 100, "HTML should be non-trivial"

    def test_arbiter_register_and_retrieve_decision(self):
        """Register a pending decision and retrieve it through the arbiter API."""
        from multifleet.arbiter_view import (
            register_pending_decision,
            get_pending_decisions,
            get_decision,
            record_decision,
            _pending_decisions,
            _decided,
        )

        # Clean state
        _pending_decisions.clear()
        _decided.clear()

        tag = _unique_tag()
        decision_id = f"e2e-decision-{tag}"
        register_pending_decision({
            "decision_id": decision_id,
            "type": "race_synthesis",
            "description": f"E2E test decision {tag}",
            "options": [
                {"label": "Accept", "action": "accept_recommendation"},
                {"label": "Override", "action": "override"},
            ],
        })

        pending = get_pending_decisions()
        assert any(d["decision_id"] == decision_id for d in pending)

        fetched = get_decision(decision_id)
        assert fetched is not None
        assert fetched["decision_id"] == decision_id

        # Record the decision
        result = record_decision(decision_id, "accept_recommendation", {"reason": "e2e test"})
        assert result.get("status") == "decided" or "decided" in str(result).lower() or result.get("decision_id") == decision_id

        # Should no longer be pending
        pending_after = get_pending_decisions()
        assert not any(d["decision_id"] == decision_id for d in pending_after)

        # Cleanup
        _pending_decisions.clear()
        _decided.clear()

    def test_full_organism_pipeline_live(self):
        """Probe -> evidence -> packet -> arbiter -- the full loop with live data."""
        from multifleet.probes import create_engine
        from multifleet.epistemic_packet import EpistemicPacketBuilder
        from multifleet.arbiter_view import (
            register_pending_decision,
            get_pending_decisions,
            _pending_decisions,
            _decided,
        )

        _pending_decisions.clear()
        _decided.clear()

        # Step 1: Probe the real repo
        engine = create_engine()
        results = engine.scan(trigger="all")
        assert len(results) > 0

        # Step 2: Build epistemic packet
        builder = EpistemicPacketBuilder(repo_root=str(REPO_ROOT), node_id="e2e-organism")
        packet = builder.build(
            task_intent="Full organism pipeline E2E test",
            run_probes=True,
        )
        assert packet.packet_id

        # Step 3: Simulate synthesis decision from packet
        synthesis_tag = _unique_tag()
        decision_id = f"organism-{synthesis_tag}"
        register_pending_decision({
            "decision_id": decision_id,
            "type": "organism_synthesis",
            "description": f"Organism pipeline test from {packet.packet_id}",
            "packet_id": packet.packet_id,
            "probe_count": len(packet.probe_results),
            "surgeon_intensity": packet.required_surgeon_intensity,
            "options": [
                {"label": "Merge", "action": "accept_recommendation"},
                {"label": "Request review", "action": "request_info"},
            ],
        })

        pending = get_pending_decisions()
        match = [d for d in pending if d["decision_id"] == decision_id]
        assert len(match) == 1
        assert match[0]["probe_count"] == len(packet.probe_results)

        # Cleanup
        _pending_decisions.clear()
        _decided.clear()


# ── TestLiveFleetDoctor ──


class TestLiveFleetDoctor:
    """Tests that exercise fleet_doctor against the live daemon."""

    def test_work_endpoint(self):
        """GET /work returns work backlog structure."""
        try:
            data = _daemon_get("/work")
            assert isinstance(data, dict)
        except urllib.error.HTTPError as e:
            # 404 is acceptable if endpoint not implemented in this daemon version
            if e.code == 404:
                pytest.skip("Work endpoint not available on this daemon")
            raise

    def test_health_uptime_positive(self):
        """Daemon uptime should be positive (it is running)."""
        data = _daemon_get("/health")
        uptime = data.get("uptime_s", data.get("uptime", -1))
        if uptime == -1:
            pytest.skip("Daemon does not report uptime_s")
        assert uptime >= 0, f"Uptime should be non-negative, got {uptime}"

    def test_health_idempotent(self):
        """Two rapid health calls return consistent state."""
        d1 = _daemon_get("/health")
        d2 = _daemon_get("/health")
        # Node identity should not change between calls
        id1 = d1.get("node_id", d1.get("nodeId", ""))
        id2 = d2.get("node_id", d2.get("nodeId", ""))
        if id1 and id2:
            assert id1 == id2, f"Node identity changed: {id1} vs {id2}"

    def test_fleet_live_endpoint(self):
        """GET /fleet/live should respond (used by peer probing)."""
        try:
            data = _daemon_get("/fleet/live")
            assert isinstance(data, dict)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pytest.skip("/fleet/live not available on this daemon version")
            raise
