"""
Tests for fleet_nerve_security.py — HMAC signing, verification, replay prevention, peer validation.

Tests use FLEET_NERVE_HMAC_KEY env var to inject a known key (avoids keychain dependency in CI).
"""

import json
import os
import time
from unittest.mock import patch

import pytest

# Set env key BEFORE importing the module so FleetSecurity.__init__ picks it up
TEST_HMAC_KEY = "test-fleet-secret-key-for-ci-only"


@pytest.fixture
def security():
    """FleetSecurity instance with known HMAC key and peer allowlist."""
    with patch.dict(os.environ, {"FLEET_NERVE_HMAC_KEY": TEST_HMAC_KEY}):
        from tools.fleet_nerve_security import FleetSecurity
        sec = FleetSecurity(known_peers={"mac1", "mac2", "mac3"})
    return sec


@pytest.fixture
def security_no_key():
    """FleetSecurity instance without any HMAC key (degraded mode)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FLEET_NERVE_HMAC_KEY", None)
        from tools.fleet_nerve_security import FleetSecurity
        with patch.object(FleetSecurity, "_read_keychain", return_value=None):
            with patch.object(FleetSecurity, "_generate_and_store_key", return_value=False):
                sec = FleetSecurity(known_peers={"mac1", "mac2"})
    return sec


class TestHMACSigning:
    """sign_message adds _hmac and _signed_ts fields."""

    @pytest.mark.security
    def test_signed_message_has_hmac_field(self, security):
        msg = {"from": "mac1", "type": "context", "payload": {"body": "hello"}}
        signed = security.sign_message(msg)
        assert "_hmac" in signed
        assert isinstance(signed["_hmac"], str)
        assert len(signed["_hmac"]) == 64  # SHA256 hex digest

    @pytest.mark.security
    def test_signed_message_has_timestamp(self, security):
        msg = {"from": "mac1", "type": "context", "payload": {"body": "hello"}}
        before = time.time()
        signed = security.sign_message(msg)
        after = time.time()
        assert "_signed_ts" in signed
        assert before <= signed["_signed_ts"] <= after

    @pytest.mark.security
    def test_signing_increments_stat(self, security):
        initial = security._stats["signed"]
        security.sign_message({"from": "mac1", "type": "test"})
        assert security._stats["signed"] == initial + 1

    @pytest.mark.security
    def test_no_key_skips_signing(self, security_no_key):
        msg = {"from": "mac1", "type": "context"}
        result = security_no_key.sign_message(msg)
        assert "_hmac" not in result
        assert "_signed_ts" not in result


class TestHMACVerification:
    """verify_message checks signature, replay window, and peer identity."""

    @pytest.mark.security
    def test_valid_signed_message_passes(self, security):
        msg = {"from": "mac1", "id": "abc123", "type": "context", "payload": {"body": "hi"}}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is True
        assert reason is None

    @pytest.mark.security
    def test_tampered_body_rejected(self, security):
        """Changing payload after signing invalidates HMAC."""
        msg = {"from": "mac1", "id": "abc123", "type": "context", "payload": {"body": "original"}}
        security.sign_message(msg)
        # Tamper with the message
        msg["payload"]["body"] = "tampered"
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "bad_signature"

    @pytest.mark.security
    def test_tampered_sender_rejected(self, security):
        """Changing the from field after signing invalidates HMAC."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        msg["from"] = "mac2"
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "bad_signature"

    @pytest.mark.security
    def test_tampered_type_rejected(self, security):
        """Changing the type field after signing invalidates HMAC."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        msg["type"] = "repair"
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "bad_signature"

    @pytest.mark.security
    def test_bad_signature_counter(self, security):
        """Rejected bad signatures increment the counter."""
        initial = security._stats["rejected_bad_sig"]
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        msg["payload"] = "injected"
        security.verify_message(msg)
        assert security._stats["rejected_bad_sig"] == initial + 1


class TestReplayPrevention:
    """Messages older than 5 minutes are rejected as replay attacks."""

    @pytest.mark.security
    def test_message_within_window_accepted(self, security):
        """A message signed 30 seconds ago passes replay check."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        # Timestamp is current, should pass
        valid, reason = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_message_outside_window_rejected(self, security):
        """A message with timestamp > 5 min old is rejected as replay."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        # Backdate the signed timestamp by 6 minutes
        msg["_signed_ts"] = time.time() - 360
        # Must re-sign with the backdated timestamp to get a valid HMAC
        # (otherwise HMAC check fails first)
        from tools.fleet_nerve_security import HMAC_HEADER_KEY
        del msg[HMAC_HEADER_KEY]
        security.sign_message(msg)
        # Override timestamp after signing to simulate the actual age
        # We need to manually set the timestamp and recompute HMAC
        msg["_signed_ts"] = time.time() - 360
        msg[HMAC_HEADER_KEY] = security._compute_hmac(msg)

        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "replay"

    @pytest.mark.security
    def test_replay_counter_incremented(self, security):
        """Replay rejection increments the counter."""
        initial = security._stats["rejected_replay"]
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        # Manually construct a correctly-signed but old message
        msg["_signed_ts"] = time.time() - 400
        msg["_hmac"] = security._compute_hmac(msg)

        security.verify_message(msg)
        assert security._stats["rejected_replay"] == initial + 1

    @pytest.mark.security
    def test_future_timestamp_within_window_accepted(self, security):
        """A message with timestamp slightly in the future (clock skew) passes."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        msg["_signed_ts"] = time.time() + 60  # 1 min in the future
        msg["_hmac"] = security._compute_hmac(msg)
        valid, reason = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_future_timestamp_outside_window_rejected(self, security):
        """A message with timestamp >5min in the future is also rejected."""
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        msg["_signed_ts"] = time.time() + 400  # 6+ min in the future
        msg["_hmac"] = security._compute_hmac(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "replay"


class TestPeerValidation:
    """Unknown senders are rejected when peer allowlist is configured."""

    @pytest.mark.security
    def test_known_peer_accepted(self, security):
        msg = {"from": "mac2", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_unknown_peer_rejected(self, security):
        msg = {"from": "attacker-node", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "unknown_peer"

    @pytest.mark.security
    def test_unknown_peer_counter(self, security):
        initial = security._stats["rejected_unknown_peer"]
        msg = {"from": "evil-node", "id": "abc123", "type": "context"}
        security.verify_message(msg)
        assert security._stats["rejected_unknown_peer"] == initial + 1

    @pytest.mark.security
    def test_empty_peers_accepts_all(self):
        """When no peer allowlist is set, all senders are accepted."""
        with patch.dict(os.environ, {"FLEET_NERVE_HMAC_KEY": TEST_HMAC_KEY}):
            from tools.fleet_nerve_security import FleetSecurity
            sec = FleetSecurity(known_peers=set())
        msg = {"from": "any-node", "id": "abc123", "type": "context"}
        sec.sign_message(msg)
        valid, reason = sec.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_add_peer_updates_allowlist(self, security):
        """add_peer() adds a node to the allowlist dynamically."""
        msg = {"from": "new-node", "id": "abc123", "type": "context"}
        security.sign_message(msg)
        valid, _ = security.verify_message(msg)
        assert valid is False  # Not yet known

        security.add_peer("new-node")
        valid, _ = security.verify_message(msg)
        assert valid is True  # Now accepted


class TestGracefulDegradation:
    """When no HMAC key exists, messages are accepted unsigned."""

    @pytest.mark.security
    def test_unsigned_accepted_without_key(self, security_no_key):
        msg = {"from": "mac1", "id": "abc123", "type": "context"}
        valid, reason = security_no_key.verify_message(msg)
        assert valid is True
        assert reason is None

    @pytest.mark.security
    def test_signed_accepted_without_local_key(self, security_no_key):
        """If a message IS signed but we have no key, accept gracefully."""
        msg = {"from": "mac1", "id": "abc123", "type": "context", "_hmac": "abc", "_signed_ts": time.time()}
        valid, reason = security_no_key.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_unsigned_counter(self, security_no_key):
        initial = security_no_key._stats["accepted_unsigned"]
        security_no_key.verify_message({"from": "mac1", "id": "x", "type": "test"})
        assert security_no_key._stats["accepted_unsigned"] == initial + 1


class TestGoldSanitization:
    """sanitize_gold_for_publish strips sensitive fields."""

    @pytest.mark.security
    def test_only_safe_fields_survive(self, security):
        gold = {
            "node_id": "mac1",
            "session_id": "abcdef1234567890",
            "session_count": 5,
            "topic_keywords": ["webhook", "fleet"],
            "idle_s": 120,
            "timestamp": "2026-04-05T12:00:00Z",
            # Sensitive -- should be stripped
            "gold_text": "full conversation...",
            "raw_content": "sensitive data",
            "file_paths": ["/etc/passwd"],
            "api_responses": [{"key": "secret"}],
            "code_blocks": ["import os; os.system('rm -rf /')"],
        }
        safe = security.sanitize_gold_for_publish(gold)
        assert safe["node_id"] == "mac1"
        assert safe["session_id"] == "abcdef12"  # Truncated to 8 chars
        assert "gold_text" not in safe
        assert "raw_content" not in safe
        assert "file_paths" not in safe
        assert "api_responses" not in safe
        assert "code_blocks" not in safe

    @pytest.mark.security
    def test_session_id_truncated(self, security):
        safe = security.sanitize_gold_for_publish({"session_id": "abcdefghijklmnop"})
        assert safe["session_id"] == "abcdefgh"

    @pytest.mark.security
    def test_topic_keywords_capped(self, security):
        """Keywords list capped at 10 items, each capped at 50 chars."""
        long_keywords = [f"keyword-{i}" * 10 for i in range(20)]
        safe = security.sanitize_gold_for_publish({"topic_keywords": long_keywords})
        assert len(safe["topic_keywords"]) == 10
        assert all(len(k) <= 50 for k in safe["topic_keywords"])


class TestSecurityStats:
    """get_stats returns counters without leaking key material."""

    @pytest.mark.security
    def test_stats_structure(self, security):
        stats = security.get_stats()
        assert stats["hmac_enabled"] is True
        assert stats["known_peers"] == 3
        assert "signed" in stats
        assert "rejected_bad_sig" in stats
        assert "rejected_replay" in stats

    @pytest.mark.security
    def test_stats_no_key_material(self, security):
        """Stats must never contain the HMAC key itself."""
        stats = security.get_stats()
        stats_json = json.dumps(stats)
        assert TEST_HMAC_KEY not in stats_json
