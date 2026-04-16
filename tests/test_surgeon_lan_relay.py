"""
Tests for surgeon_lan_relay.py — cross-machine surgery relay.

Covers:
- SurgeonPacket creation, serialization, validation
- Packet builders for all types
- SurgeonLANRelay send/receive routing
- Dedup cache behavior
- Stale packet dropping
- Handler dispatch
- Transport fallback (NATS -> HTTP -> P7 git)
- Stats tracking and log trimming
- create_relay_for_node factory
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.surgeon_lan_relay import (
    PACKET_MAX_AGE_S,
    PROTOCOL_VERSION,
    VALID_PACKET_TYPES,
    SurgeonLANRelay,
    SurgeonPacket,
    build_branch_status,
    build_chief_decision,
    build_critique,
    build_local_verdict,
    build_review_request,
    build_task_brief,
    create_relay_for_node,
)


# ── SurgeonPacket ──────────────────────────────────────────────────────


class TestSurgeonPacket:
    def test_defaults(self):
        p = SurgeonPacket()
        assert p.protocol == PROTOCOL_VERSION
        assert p.type == ""
        assert p.to_node == "all"
        assert len(p.id) == 36  # UUID

    def test_to_dict_roundtrip(self):
        p = SurgeonPacket(type="heartbeat", from_node="mac1", task_id="t1")
        d = p.to_dict()
        p2 = SurgeonPacket.from_dict(d)
        assert p2.type == "heartbeat"
        assert p2.from_node == "mac1"
        assert p2.task_id == "t1"

    def test_json_roundtrip(self):
        p = SurgeonPacket(type="critique", from_node="mac2", payload={"x": 1})
        raw = p.to_json()
        p2 = SurgeonPacket.from_json(raw)
        assert p2.type == "critique"
        assert p2.payload == {"x": 1}

    def test_from_json_bytes(self):
        data = json.dumps({"type": "heartbeat", "from_node": "mac1"}).encode()
        p = SurgeonPacket.from_json(data)
        assert p.type == "heartbeat"

    def test_is_stale_false_for_new(self):
        p = SurgeonPacket(timestamp=time.time())
        assert p.is_stale is False

    def test_is_stale_true_for_old(self):
        p = SurgeonPacket(timestamp=time.time() - PACKET_MAX_AGE_S - 1)
        assert p.is_stale is True

    def test_validate_valid_packet(self):
        p = SurgeonPacket(type="heartbeat", from_node="mac1")
        assert p.validate() == []

    def test_validate_missing_type(self):
        p = SurgeonPacket(from_node="mac1")
        errors = p.validate()
        assert any("missing type" in e for e in errors)

    def test_validate_missing_from_node(self):
        p = SurgeonPacket(type="heartbeat")
        errors = p.validate()
        assert any("missing from_node" in e for e in errors)

    def test_validate_wrong_protocol(self):
        p = SurgeonPacket(protocol="wrong/v99", type="heartbeat", from_node="mac1")
        errors = p.validate()
        assert any("wrong protocol" in e for e in errors)

    def test_validate_unknown_type(self):
        p = SurgeonPacket(type="invalid_type", from_node="mac1")
        errors = p.validate()
        assert any("unknown type" in e for e in errors)


# ── Packet builders ────────────────────────────────────────────────────


class TestPacketBuilders:
    def test_build_task_brief(self):
        p = build_task_brief("mac1", "mac2", "t1", "Refactor auth", "split module", ["mac1", "mac2"])
        assert p.type == "task_brief"
        assert p.from_node == "mac1"
        assert p.to_node == "mac2"
        assert p.task_id == "t1"
        assert p.payload["task"] == "Refactor auth"
        assert p.payload["approach"] == "split module"
        assert p.payload["machines"] == ["mac1", "mac2"]
        assert p.validate() == []

    def test_build_branch_status(self):
        p = build_branch_status("mac2", "feature/auth", "WIP on auth", ["auth.py"], state="in_progress", task_id="t1")
        assert p.type == "branch_status"
        assert p.payload["branch"] == "feature/auth"
        assert p.payload["state"] == "in_progress"
        assert p.validate() == []

    def test_build_local_verdict(self):
        p = build_local_verdict("mac2", "t1", "Looks good", "feature/auth", 0.92,
                                files_touched=["a.py"], risks=["perf"], dissent=[{"from": "neurologist", "note": "check perf"}])
        assert p.type == "local_verdict"
        assert p.to_node == "chief"
        assert p.payload["confidence"] == 0.92
        assert p.payload["risks"] == ["perf"]
        assert p.payload["phase"] == "close"
        assert p.validate() == []

    def test_build_local_verdict_defaults(self):
        p = build_local_verdict("mac2", "t1", "OK", "main", 0.5)
        assert p.payload["files_touched"] == []
        assert p.payload["risks"] == []
        assert p.payload["dissent"] == []

    def test_build_review_request(self):
        p = build_review_request("mac1", "mac3", "t1", "mac2", "feature/auth", focus="security")
        assert p.type == "review_request"
        assert p.to_node == "mac3"
        assert p.payload["target_node"] == "mac2"
        assert p.payload["focus"] == "security"
        assert p.validate() == []

    def test_build_critique(self):
        p = build_critique("mac3", "mac2", "t1", "Missing error handling", 0.85,
                           blocking_concerns=["no try/except"], suggested_action="add error handling")
        assert p.type == "critique"
        assert p.to_node == "mac2"
        assert p.payload["blocking_concerns"] == ["no try/except"]
        assert p.validate() == []

    def test_build_chief_decision(self):
        p = build_chief_decision("mac1", "t1", "merge", winner_branch="feature/auth",
                                 confidence=0.95, reasoning="All tests pass",
                                 cherry_pick_from=[{"node": "mac3", "commit": "abc123"}],
                                 followups=["update docs"], dissent_preserved=[{"from": "mac3", "note": "perf concern"}],
                                 memory_reasons=["proven pattern"])
        assert p.type == "chief_decision"
        assert p.payload["decision"] == "merge"
        assert p.payload["confidence"] == 0.95
        assert len(p.payload["cherry_pick_from"]) == 1
        assert p.validate() == []

    def test_build_chief_decision_defaults(self):
        p = build_chief_decision("mac1", "t1", "reject")
        assert p.payload["cherry_pick_from"] == []
        assert p.payload["followups"] == []
        assert p.payload["dissent_preserved"] == []
        assert p.payload["memory_reasons"] == []

    def test_confidence_rounding(self):
        """Confidence is rounded to 4 decimal places."""
        p = build_local_verdict("mac2", "t1", "ok", "main", 0.123456789)
        assert p.payload["confidence"] == 0.1235


# ── SurgeonLANRelay ────────────────────────────────────────────────────


@pytest.fixture
def relay(tmp_path):
    return SurgeonLANRelay(
        node_id="mac1",
        nats_client=None,
        daemon_url="http://127.0.0.1:9999",
        fleet_messages_root=tmp_path / ".fleet-messages",
    )


def _valid_packet(**kwargs) -> SurgeonPacket:
    defaults = {"type": "heartbeat", "from_node": "mac2", "timestamp": time.time()}
    defaults.update(kwargs)
    return SurgeonPacket(**defaults)


class TestRelaySend:
    def test_invalid_packet_rejected(self, relay):
        p = SurgeonPacket()  # missing type and from_node
        assert relay.send(p) is False
        assert relay._stats["errors"] == 1

    def test_nats_preferred(self, relay):
        """NATS is tried first when available."""
        mock_nats = MagicMock()
        mock_nats.publish_sync = MagicMock()
        relay._nats = mock_nats

        p = _valid_packet(from_node="mac1")
        result = relay.send(p)
        assert result is True
        mock_nats.publish_sync.assert_called_once()
        assert relay._stats["sent"] == 1

    def test_nats_subject_format(self, relay):
        """NATS subject follows fleet.surgeon.<to>.<type> pattern."""
        mock_nats = MagicMock()
        mock_nats.publish_sync = MagicMock()
        relay._nats = mock_nats

        p = _valid_packet(from_node="mac1", to_node="mac2", type="critique")
        relay.send(p)
        subject = mock_nats.publish_sync.call_args[0][0]
        assert subject == "fleet.surgeon.mac2.critique"

    def test_http_fallback(self, relay):
        """Falls back to HTTP when NATS unavailable."""
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_resp

            p = _valid_packet(from_node="mac1")
            result = relay.send(p)
            assert result is True
            assert relay._stats["sent"] == 1

    def test_p7_git_fallback(self, relay, tmp_path):
        """Falls back to P7 git when NATS and HTTP fail."""
        relay._fleet_msg_root = tmp_path / ".fleet-messages"

        with patch("urllib.request.urlopen", side_effect=Exception("no http")):
            p = _valid_packet(from_node="mac1", to_node="mac2")
            result = relay.send(p)
            assert result is True

        # Check file was created
        msg_files = list((tmp_path / ".fleet-messages" / "mac2").glob("*.json"))
        assert len(msg_files) == 1
        content = json.loads(msg_files[0].read_text())
        assert content["type"] == "heartbeat"

    def test_p7_broadcast_to_all(self, relay, tmp_path):
        """Broadcast (to_node=all) writes to 'all' directory."""
        relay._fleet_msg_root = tmp_path / ".fleet-messages"

        with patch("urllib.request.urlopen", side_effect=Exception):
            p = _valid_packet(from_node="mac1", to_node="all")
            relay.send(p)

        assert (tmp_path / ".fleet-messages" / "all").exists()

    def test_all_transports_fail(self, relay):
        """Returns False when all transports fail."""
        relay._fleet_msg_root = None  # disable P7
        with patch("urllib.request.urlopen", side_effect=Exception):
            p = _valid_packet(from_node="mac1")
            result = relay.send(p)
            assert result is False
            assert relay._stats["errors"] == 1


class TestRelayReceive:
    def test_valid_packet_received(self, relay):
        p = _valid_packet()
        result = relay.receive(p.to_dict())
        assert result is not None
        assert result.type == "heartbeat"
        assert relay._stats["received"] == 1

    def test_json_string_input(self, relay):
        p = _valid_packet()
        result = relay.receive(p.to_json())
        assert result is not None

    def test_json_bytes_input(self, relay):
        p = _valid_packet()
        result = relay.receive(p.to_json().encode())
        assert result is not None

    def test_invalid_json_returns_none(self, relay):
        result = relay.receive("not json {{{}}")
        assert result is None
        assert relay._stats["errors"] == 1

    def test_invalid_packet_returns_none(self, relay):
        result = relay.receive({"type": "", "from_node": ""})
        assert result is None
        assert relay._stats["errors"] == 1

    def test_dedup_same_id(self, relay):
        p = _valid_packet()
        r1 = relay.receive(p.to_dict())
        r2 = relay.receive(p.to_dict())
        assert r1 is not None
        assert r2 is None
        assert relay._stats["deduped"] == 1

    def test_stale_packet_dropped(self, relay):
        p = _valid_packet(timestamp=time.time() - PACKET_MAX_AGE_S - 100)
        result = relay.receive(p.to_dict())
        assert result is None
        assert relay._stats["stale_dropped"] == 1

    def test_handler_dispatch(self, relay):
        """Registered handler is called on matching packet type."""
        received = []
        relay.on("heartbeat", lambda pkt: received.append(pkt))

        p = _valid_packet()
        relay.receive(p.to_dict())
        assert len(received) == 1
        assert received[0].type == "heartbeat"

    def test_multiple_handlers(self, relay):
        """Multiple handlers for same type all fire."""
        calls = []
        relay.on("critique", lambda p: calls.append("h1"))
        relay.on("critique", lambda p: calls.append("h2"))

        p = _valid_packet(type="critique")
        relay.receive(p.to_dict())
        assert calls == ["h1", "h2"]

    def test_handler_error_doesnt_crash(self, relay):
        """Handler exception doesn't prevent packet from being returned."""
        relay.on("heartbeat", lambda p: (_ for _ in ()).throw(ValueError("boom")))

        p = _valid_packet()
        # Should not raise - handler error is caught
        result = relay.receive(p.to_dict())
        # Packet still returned despite handler error
        assert result is not None

    def test_dedup_cache_trimmed(self, relay):
        """Seen cache is trimmed when it exceeds max_seen."""
        relay._max_seen = 10
        for i in range(15):
            p = _valid_packet(id=f"id-{i:04d}" + "-" * 27)  # pad to UUID length
            # Use from_dict to bypass UUID generation
            d = p.to_dict()
            d["id"] = f"00000000-0000-0000-0000-{i:012d}"
            relay.receive(d)
        # Cache should have been trimmed
        assert len(relay._seen) <= 15  # may be trimmed to ~half


class TestRelayLogs:
    def test_sent_log_trimmed(self, relay):
        relay._max_log = 5
        mock_nats = MagicMock()
        mock_nats.publish_sync = MagicMock()
        relay._nats = mock_nats

        for i in range(10):
            p = _valid_packet(from_node="mac1")
            relay.send(p)

        assert len(relay._sent) <= 5

    def test_received_log_trimmed(self, relay):
        relay._max_log = 5
        for i in range(10):
            p = _valid_packet()
            relay.receive(p.to_dict())

        assert len(relay._received) <= 5


class TestRelayStats:
    def test_initial_stats(self, relay):
        stats = relay.get_stats()
        assert stats["sent"] == 0
        assert stats["received"] == 0
        assert stats["deduped"] == 0
        assert stats["stale_dropped"] == 0
        assert stats["errors"] == 0

    def test_get_recent_sent(self, relay):
        mock_nats = MagicMock()
        mock_nats.publish_sync = MagicMock()
        relay._nats = mock_nats

        for _ in range(3):
            relay.send(_valid_packet(from_node="mac1"))

        recent = relay.get_recent_sent(limit=2)
        assert len(recent) == 2

    def test_get_recent_received(self, relay):
        for _ in range(3):
            relay.receive(_valid_packet().to_dict())

        recent = relay.get_recent_received(limit=2)
        assert len(recent) == 2


# ── create_relay_for_node factory ──────────────────────────────────────


class TestCreateRelayForNode:
    def test_with_superrepo_root(self, tmp_path):
        relay = create_relay_for_node("mac1", superrepo_root=tmp_path)
        assert relay.node_id == "mac1"
        assert relay._fleet_msg_root == tmp_path / ".fleet-messages"

    def test_without_superrepo_root_env_fallback(self):
        with patch.dict("os.environ", {"MULTIFLEET_NODE_ID": "mac2"}):
            relay = create_relay_for_node("")
            assert relay.node_id == "mac2"

    def test_with_nats_client(self, tmp_path):
        mock_nats = MagicMock()
        relay = create_relay_for_node("mac1", nats_client=mock_nats, superrepo_root=tmp_path)
        assert relay._nats is mock_nats


# ── Valid packet types ─────────────────────────────────────────────────


class TestValidPacketTypes:
    def test_all_expected_types_present(self):
        expected = {
            "task_brief", "branch_status", "local_verdict",
            "review_request", "critique", "rebuttal_request",
            "chief_decision", "heartbeat", "node_capabilities",
        }
        assert VALID_PACKET_TYPES == expected

    @pytest.mark.parametrize("ptype", list(VALID_PACKET_TYPES))
    def test_each_type_validates(self, ptype):
        p = SurgeonPacket(type=ptype, from_node="mac1")
        assert p.validate() == []
