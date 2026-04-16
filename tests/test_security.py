"""
Tests for multifleet.security — HMAC signing/verification, peer IP validation (CV-001),
replay prevention, gold sanitization, and graceful degradation.

Uses FLEET_NERVE_HMAC_KEY env var to inject a known key (avoids keychain dependency).
"""

import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

TEST_HMAC_KEY = "test-security-module-key-for-tests"


@pytest.fixture
def security():
    """FleetSecurity with known HMAC key and 3-node peer allowlist."""
    with patch.dict(os.environ, {"FLEET_NERVE_HMAC_KEY": TEST_HMAC_KEY}):
        from multifleet.security import FleetSecurity
        sec = FleetSecurity(known_peers={"mac1", "mac2", "mac3"})
    return sec


@pytest.fixture
def security_no_key():
    """FleetSecurity without HMAC key (degraded security mode)."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FLEET_NERVE_HMAC_KEY", None)
        from multifleet.security import FleetSecurity
        with patch.object(FleetSecurity, "_read_keychain", return_value=None):
            with patch.object(FleetSecurity, "_generate_and_store_key", return_value=False):
                sec = FleetSecurity(known_peers={"mac1", "mac2"})
    return sec


# ══════════════════════════════════════════════════════════════
# HMAC Signing
# ══════════════════════════════════════════════════════════════


class TestHMACSigning:
    """sign_message adds HMAC signature and timestamp."""

    @pytest.mark.security
    def test_signature_is_sha256_hex(self, security):
        msg = {"from": "mac1", "type": "context", "payload": {}}
        signed = security.sign_message(msg)
        assert "_hmac" in signed
        assert len(signed["_hmac"]) == 64  # SHA256 hex = 64 chars

    @pytest.mark.security
    def test_timestamp_added(self, security):
        msg = {"from": "mac1", "type": "context"}
        before = time.time()
        signed = security.sign_message(msg)
        after = time.time()
        assert before <= signed["_signed_ts"] <= after

    @pytest.mark.security
    def test_signing_counter_incremented(self, security):
        initial = security._stats["signed"]
        security.sign_message({"from": "mac1", "type": "test"})
        assert security._stats["signed"] == initial + 1

    @pytest.mark.security
    def test_no_key_returns_message_unchanged(self, security_no_key):
        msg = {"from": "mac1", "type": "context"}
        result = security_no_key.sign_message(msg)
        assert "_hmac" not in result
        assert "_signed_ts" not in result

    @pytest.mark.security
    def test_signature_deterministic(self, security):
        """Same message content produces same HMAC."""
        msg1 = {"from": "mac1", "type": "test", "_signed_ts": 1000.0}
        sig1 = security._compute_hmac(msg1)
        msg2 = {"from": "mac1", "type": "test", "_signed_ts": 1000.0}
        sig2 = security._compute_hmac(msg2)
        assert sig1 == sig2

    @pytest.mark.security
    def test_different_content_different_signature(self, security):
        msg1 = {"from": "mac1", "type": "test", "_signed_ts": 1000.0}
        msg2 = {"from": "mac2", "type": "test", "_signed_ts": 1000.0}
        assert security._compute_hmac(msg1) != security._compute_hmac(msg2)


# ══════════════════════════════════════════════════════════════
# HMAC Verification
# ══════════════════════════════════════════════════════════════


class TestHMACVerification:
    """verify_message checks signature integrity."""

    @pytest.mark.security
    def test_valid_signed_message_passes(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is True
        assert reason is None

    @pytest.mark.security
    def test_tampered_payload_rejected(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context", "payload": {"body": "original"}}
        security.sign_message(msg)
        msg["payload"]["body"] = "tampered"
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "bad_signature"

    @pytest.mark.security
    def test_tampered_sender_rejected(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        msg["from"] = "mac2"
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "bad_signature"

    @pytest.mark.security
    def test_bad_sig_counter(self, security):
        initial = security._stats["rejected_bad_sig"]
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        msg["type"] = "injected"
        security.verify_message(msg)
        assert security._stats["rejected_bad_sig"] == initial + 1

    @pytest.mark.security
    def test_verified_counter(self, security):
        initial = security._stats["verified"]
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        security.verify_message(msg)
        assert security._stats["verified"] == initial + 1


# ══════════════════════════════════════════════════════════════
# Peer Identity Validation (CV-001)
# ══════════════════════════════════════════════════════════════


class TestPeerValidation:
    """Unknown peers are rejected when allowlist is configured."""

    @pytest.mark.security
    def test_known_peer_accepted(self, security):
        msg = {"from": "mac2", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        valid, _ = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_unknown_peer_rejected(self, security):
        msg = {"from": "attacker", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "unknown_peer"

    @pytest.mark.security
    def test_unknown_peer_counter(self, security):
        initial = security._stats["rejected_unknown_peer"]
        msg = {"from": "evil-node", "id": "test-1"}
        security.verify_message(msg)
        assert security._stats["rejected_unknown_peer"] == initial + 1

    @pytest.mark.security
    def test_empty_allowlist_accepts_all(self):
        with patch.dict(os.environ, {"FLEET_NERVE_HMAC_KEY": TEST_HMAC_KEY}):
            from multifleet.security import FleetSecurity
            sec = FleetSecurity(known_peers=set())
        msg = {"from": "any-node", "id": "test-1", "type": "test"}
        sec.sign_message(msg)
        valid, _ = sec.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_add_peer_allows_new_node(self, security):
        msg = {"from": "new-node", "id": "test-1", "type": "test"}
        security.sign_message(msg)
        valid, _ = security.verify_message(msg)
        assert valid is False

        security.add_peer("new-node")
        valid, _ = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_update_known_peers(self, security):
        security.update_known_peers({"nodeA", "nodeB"})
        msg = {"from": "mac1", "id": "test-1", "type": "test"}
        security.sign_message(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "unknown_peer"

        msg2 = {"from": "nodeA", "id": "test-2", "type": "test"}
        security.sign_message(msg2)
        valid2, _ = security.verify_message(msg2)
        assert valid2 is True


# ══════════════════════════════════════════════════════════════
# Replay Prevention
# ══════════════════════════════════════════════════════════════


class TestReplayPrevention:
    """Messages older than 5 minutes are rejected."""

    @pytest.mark.security
    def test_fresh_message_accepted(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        security.sign_message(msg)
        valid, _ = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_old_message_rejected(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        msg["_signed_ts"] = time.time() - 400  # 6+ minutes old
        msg["_hmac"] = security._compute_hmac(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "replay"

    @pytest.mark.security
    def test_replay_counter(self, security):
        initial = security._stats["rejected_replay"]
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        msg["_signed_ts"] = time.time() - 400
        msg["_hmac"] = security._compute_hmac(msg)
        security.verify_message(msg)
        assert security._stats["rejected_replay"] == initial + 1

    @pytest.mark.security
    def test_future_within_window_accepted(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        msg["_signed_ts"] = time.time() + 60  # 1 min future (clock skew)
        msg["_hmac"] = security._compute_hmac(msg)
        valid, _ = security.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_future_outside_window_rejected(self, security):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        msg["_signed_ts"] = time.time() + 400  # 6+ min future
        msg["_hmac"] = security._compute_hmac(msg)
        valid, reason = security.verify_message(msg)
        assert valid is False
        assert reason == "replay"


# ══════════════════════════════════════════════════════════════
# Graceful Degradation
# ══════════════════════════════════════════════════════════════


class TestGracefulDegradation:
    """When no HMAC key, messages are accepted unsigned."""

    @pytest.mark.security
    def test_unsigned_accepted_without_key(self, security_no_key):
        msg = {"from": "mac1", "id": "test-1", "type": "context"}
        valid, reason = security_no_key.verify_message(msg)
        assert valid is True
        assert reason is None

    @pytest.mark.security
    def test_signed_msg_accepted_without_local_key(self, security_no_key):
        msg = {"from": "mac1", "id": "test-1", "_hmac": "abc", "_signed_ts": time.time()}
        valid, _ = security_no_key.verify_message(msg)
        assert valid is True

    @pytest.mark.security
    def test_unsigned_counter(self, security_no_key):
        initial = security_no_key._stats["accepted_unsigned"]
        security_no_key.verify_message({"from": "mac1", "id": "x"})
        assert security_no_key._stats["accepted_unsigned"] == initial + 1

    @pytest.mark.security
    def test_has_hmac_key_property(self, security, security_no_key):
        assert security.has_hmac_key is True
        assert security_no_key.has_hmac_key is False


# ══════════════════════════════════════════════════════════════
# Session Gold Sanitization
# ══════════════════════════════════════════════════════════════


class TestGoldSanitization:
    """sanitize_gold_for_publish strips sensitive fields."""

    @pytest.mark.security
    def test_safe_fields_preserved(self):
        from multifleet.security import FleetSecurity
        gold = {
            "node_id": "mac1",
            "session_id": "abcdef1234567890",
            "session_count": 5,
            "topic_keywords": ["webhook", "fleet"],
            "idle_s": 120,
            "timestamp": "2026-04-05T12:00:00Z",
        }
        safe = FleetSecurity.sanitize_gold_for_publish(gold)
        assert safe["node_id"] == "mac1"
        assert safe["session_count"] == 5
        assert safe["idle_s"] == 120
        assert safe["timestamp"] == "2026-04-05T12:00:00Z"

    @pytest.mark.security
    def test_sensitive_fields_stripped(self):
        from multifleet.security import FleetSecurity
        gold = {
            "node_id": "mac1",
            "gold_text": "full conversation...",
            "raw_content": "sensitive data",
            "file_paths": ["/etc/passwd"],
            "api_responses": [{"key": "secret"}],
            "code_blocks": ["import os"],
            "error_details": "traceback...",
        }
        safe = FleetSecurity.sanitize_gold_for_publish(gold)
        assert "gold_text" not in safe
        assert "raw_content" not in safe
        assert "file_paths" not in safe
        assert "api_responses" not in safe
        assert "code_blocks" not in safe
        assert "error_details" not in safe

    @pytest.mark.security
    def test_session_id_truncated_to_8(self):
        from multifleet.security import FleetSecurity
        safe = FleetSecurity.sanitize_gold_for_publish({"session_id": "abcdefghijklmnop"})
        assert safe["session_id"] == "abcdefgh"

    @pytest.mark.security
    def test_nodeId_alias(self):
        from multifleet.security import FleetSecurity
        safe = FleetSecurity.sanitize_gold_for_publish({"nodeId": "mac3"})
        assert safe["node_id"] == "mac3"

    @pytest.mark.security
    def test_sessionId_alias(self):
        from multifleet.security import FleetSecurity
        safe = FleetSecurity.sanitize_gold_for_publish({"sessionId": "xyz123456789"})
        assert safe["session_id"] == "xyz12345"

    @pytest.mark.security
    def test_topic_keywords_list_capped(self):
        from multifleet.security import FleetSecurity
        keywords = [f"kw-{i}" * 20 for i in range(25)]
        safe = FleetSecurity.sanitize_gold_for_publish({"topic_keywords": keywords})
        assert len(safe["topic_keywords"]) == 10
        assert all(len(k) <= 50 for k in safe["topic_keywords"])

    @pytest.mark.security
    def test_topic_keywords_string(self):
        from multifleet.security import FleetSecurity
        safe = FleetSecurity.sanitize_gold_for_publish({"topic_keywords": "a" * 500})
        assert len(safe["topic_keywords"]) <= 200

    @pytest.mark.security
    def test_empty_gold_returns_empty(self):
        from multifleet.security import FleetSecurity
        safe = FleetSecurity.sanitize_gold_for_publish({})
        assert safe == {}


# ══════════════════════════════════════════════════════════════
# Security Stats
# ══════════════════════════════════════════════════════════════


class TestSecurityStats:
    """get_stats returns counters without key material."""

    @pytest.mark.security
    def test_stats_structure(self, security):
        stats = security.get_stats()
        assert stats["hmac_enabled"] is True
        assert stats["known_peers"] == 3
        assert "signed" in stats
        assert "rejected_bad_sig" in stats
        assert "rejected_replay" in stats
        assert "rejected_unknown_peer" in stats
        assert "accepted_unsigned" in stats

    @pytest.mark.security
    def test_no_key_material_leaked(self, security):
        stats = security.get_stats()
        stats_json = json.dumps(stats)
        assert TEST_HMAC_KEY not in stats_json

    @pytest.mark.security
    def test_peer_ids_listed(self, security):
        stats = security.get_stats()
        assert sorted(stats["peer_ids"]) == ["mac1", "mac2", "mac3"]


# ══════════════════════════════════════════════════════════════
# Key Management
# ══════════════════════════════════════════════════════════════


class TestKeyManagement:
    """HMAC key loading from env, keychain, or generation."""

    @pytest.mark.security
    def test_env_var_takes_priority(self):
        with patch.dict(os.environ, {"FLEET_NERVE_HMAC_KEY": "my-env-key"}):
            from multifleet.security import FleetSecurity
            sec = FleetSecurity()
        assert sec._hmac_key == b"my-env-key"

    @pytest.mark.security
    def test_keychain_fallback(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_NERVE_HMAC_KEY", None)
            from multifleet.security import FleetSecurity
            with patch.object(FleetSecurity, "_read_keychain", return_value="keychain-key"):
                sec = FleetSecurity()
        assert sec._hmac_key == b"keychain-key"

    @pytest.mark.security
    def test_generate_when_no_key_available(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLEET_NERVE_HMAC_KEY", None)
            from multifleet.security import FleetSecurity
            with patch.object(FleetSecurity, "_read_keychain", return_value=None):
                with patch.object(FleetSecurity, "_generate_and_store_key", return_value=False):
                    sec = FleetSecurity()
        assert sec._hmac_key is None
        assert sec.has_hmac_key is False
