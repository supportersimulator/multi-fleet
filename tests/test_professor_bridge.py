"""Tests for multifleet.professor_bridge -- Professor wisdom bridge."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.professor_bridge import (
    CachedWisdom,
    ProfessorBridge,
    get_professor_bridge,
)


# ── Fixtures ──


@pytest.fixture
def bridge():
    """Fresh ProfessorBridge with short cache TTL for testing."""
    return ProfessorBridge(cache_ttl=5.0)


@pytest.fixture
def mock_guidance():
    """A mock ProfessorGuidance object."""
    g = MagicMock()
    g.the_one_thing = "Always read before writing"
    g.landmines = ["Never skip validation", "Watch for race conditions"]
    g.pattern = "Read -> Plan -> Execute -> Verify"
    g.domains = ["memory", "fleet"]
    g.context = "Working on fleet integration"
    g.first_principles = ["Understand before acting"]
    return g


# ── CachedWisdom ──


class TestCachedWisdom:
    def test_not_expired_within_ttl(self):
        cw = CachedWisdom(topic="test", guidance={}, queried_at=time.time(), ttl=300.0)
        assert not cw.expired

    def test_expired_after_ttl(self):
        cw = CachedWisdom(topic="test", guidance={}, queried_at=time.time() - 400, ttl=300.0)
        assert cw.expired


# ── Query ──


class TestQuery:
    def test_query_with_professor_available(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        result = bridge.query("fleet integration")

        assert result["topic"] == "fleet integration"
        assert result["the_one_thing"] == "Always read before writing"
        assert len(result["landmines"]) == 2
        assert result["pattern"] == "Read -> Plan -> Execute -> Verify"
        assert result["cached"] is False
        assert bridge._query_count == 1

    def test_query_returns_cached_on_repeat(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.query("fleet integration")
        result2 = bridge.query("fleet integration")

        assert result2["cached"] is True
        assert mock_prof.consult.call_count == 1  # only called once
        assert bridge._cache_hits == 1

    def test_query_fallback_when_professor_unavailable(self, bridge):
        # No professor set, import will fail
        with patch("multifleet.professor_bridge.ProfessorBridge._get_professor", return_value=None):
            result = bridge.query("anything")

        assert result["topic"] == "anything"
        assert "error" in result
        assert result["the_one_thing"] == ""

    def test_query_handles_exception(self, bridge):
        mock_prof = MagicMock()
        mock_prof.consult.side_effect = RuntimeError("LLM down")
        bridge._professor = mock_prof

        result = bridge.query("broken query")
        assert "error" in result
        assert result["topic"] == "broken query"


# ── Cache ──


class TestCache:
    def test_clear_cache(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.query("topic1")
        bridge.query("topic2")
        assert len(bridge._cache) == 2

        count = bridge.clear_cache()
        assert count == 2
        assert len(bridge._cache) == 0

    def test_expired_cache_requeried(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.query("topic")
        # Manually expire the cache entry
        bridge._cache["topic"].queried_at = time.time() - 999

        bridge.query("topic")
        assert mock_prof.consult.call_count == 2  # re-queried


# ── Injection Context ──


class TestInjectionContext:
    def test_injection_context_with_branch(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        ctx = bridge.get_injection_context(branch="feature/professor")
        assert "Professor Wisdom" in ctx
        assert "Always read before writing" in ctx
        assert "AVOID:" in ctx

    def test_injection_context_empty_when_no_topics(self, bridge):
        result = bridge.get_injection_context()
        assert result == ""

    def test_injection_context_caps_files(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.get_injection_context(files=["a.py", "b.py", "c.py", "d.py", "e.py"])
        # Only first 3 files queried
        assert mock_prof.consult.call_count == 3


# ── Panel Data ──


class TestPanelData:
    def test_panel_data_empty(self, bridge):
        data = bridge.get_panel_data()
        assert data["recent_wisdom"] == []
        assert data["active_gotchas"] == []
        assert data["architecture_notes"] == []
        assert data["stats"]["total_queries"] == 0

    def test_panel_data_after_queries(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.query("topic1")
        bridge.query("topic2")
        bridge.query("topic1")  # cache hit

        data = bridge.get_panel_data()
        assert len(data["recent_wisdom"]) == 2
        assert data["stats"]["total_queries"] == 3
        assert data["stats"]["cache_hits"] == 1
        assert data["stats"]["cache_size"] == 2

    def test_gotchas_from_panel(self, bridge, mock_guidance):
        mock_prof = MagicMock()
        mock_prof.consult.return_value = mock_guidance
        bridge._professor = mock_prof

        bridge.query("topic1")
        data = bridge.get_panel_data()
        assert len(data["active_gotchas"]) == 2  # 2 landmines


# ── Web Server Endpoints ──


class TestWebEndpoints:
    def test_professor_query_endpoint(self):
        """Test that web_server routes /api/professor correctly."""
        from multifleet.web_server import FleetWebHandler
        handler = MagicMock(spec=FleetWebHandler)
        handler.path = "/api/professor?topic=fleet+integration"
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        mock_bridge = MagicMock()
        mock_bridge.query.return_value = {"topic": "fleet integration", "cached": False}

        with patch("multifleet.web_server._get_professor_bridge", return_value=mock_bridge):
            FleetWebHandler._serve_professor(handler, "/api/professor")

        mock_bridge.query.assert_called_once_with("fleet integration")

    def test_professor_panel_endpoint(self):
        """Test that /api/professor/panel returns panel data."""
        from multifleet.web_server import FleetWebHandler
        handler = MagicMock(spec=FleetWebHandler)
        handler.path = "/api/professor/panel"

        mock_bridge = MagicMock()
        mock_bridge.get_panel_data.return_value = {"recent_wisdom": [], "stats": {}}

        with patch("multifleet.web_server._get_professor_bridge", return_value=mock_bridge):
            FleetWebHandler._serve_professor(handler, "/api/professor/panel")

        mock_bridge.get_panel_data.assert_called_once()


# ── IDE Bridge Integration ──


class TestIDEBridgeIntegration:
    def test_status_bar_includes_gotcha_count(self):
        """Status bar data should include gotcha_count from professor."""
        from multifleet.ide_bridge import IDEBridge

        mock_agg = MagicMock()
        mock_agg.snapshot.return_value = {
            "peers": {"reachable": 1, "total": 2},
            "health_dot": "green",
            "node_id": "mac1",
            "rebuttals": {"active": 0},
            "liaison": {"pending_reviews": 0, "unread": 0},
        }
        mock_agg.status_bar_text.return_value = "Fleet: 2/3"

        bridge = IDEBridge(aggregator=mock_agg)

        mock_prof = MagicMock()
        mock_prof._get_gotchas.return_value = [{"gotcha": "x"}, {"gotcha": "y"}]
        bridge._professor_bridge = mock_prof

        data = bridge.get_status_bar_data()
        assert "gotcha_count" in data
        assert data["gotcha_count"] == 2

    def test_activity_bar_includes_professor_section(self):
        """Activity bar should include a Professor Wisdom section."""
        from multifleet.ide_bridge import IDEBridge

        mock_agg = MagicMock()
        mock_agg.snapshot.return_value = {
            "daemon": {"status": "ok", "peers": {}},
            "rebuttals": {"active": 0, "total": 0},
            "leases": {"active": 0, "held_by_me": 0},
            "liaison": {"unread": 0},
            "node_id": "mac1",
        }

        bridge = IDEBridge(aggregator=mock_agg)

        mock_prof = MagicMock()
        mock_prof.get_panel_data.return_value = {
            "recent_wisdom": [
                {"topic": "fleet", "the_one_thing": "coordinate first", "landmine_count": 1, "queried_at": time.time()},
            ],
            "active_gotchas": [{"topic": "fleet", "gotcha": "check daemon"}],
        }
        bridge._professor_bridge = mock_prof

        data = bridge.get_activity_bar_data()
        section_ids = [s["id"] for s in data["sections"]]
        assert "professor" in section_ids

        prof_section = next(s for s in data["sections"] if s["id"] == "professor")
        assert prof_section["count"] >= 1


# ── Liaison Integration ──


class TestLiaisonIntegration:
    def test_batch_injection_includes_professor(self):
        """get_batch_for_injection should include professor context."""
        from multifleet.liaison import FleetLiaison

        liaison = FleetLiaison(node_id="mac1")
        # Add a message so there's something to inject
        liaison.process_message({"type": "context", "from": "mac2", "payload": {"subject": "test"}})

        mock_bridge = MagicMock()
        mock_bridge.get_panel_data.return_value = {
            "active_gotchas": [{"topic": "test", "gotcha": "watch out"}],
            "recent_wisdom": [{"topic": "test", "the_one_thing": "be careful", "queried_at": time.time()}],
        }

        with patch("multifleet.professor_bridge.get_professor_bridge", return_value=mock_bridge):
            batch = liaison.get_batch_for_injection()

        assert batch is not None
        assert "Professor Wisdom" in batch


# ── Singleton ──


class TestSingleton:
    def test_get_professor_bridge_returns_same_instance(self):
        import multifleet.professor_bridge as mod
        mod._bridge = None  # reset
        b1 = get_professor_bridge()
        b2 = get_professor_bridge()
        assert b1 is b2
        mod._bridge = None  # cleanup
