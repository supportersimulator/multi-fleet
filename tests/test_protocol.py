"""Tests for protocol version negotiation."""
import logging
from multifleet.protocol import (
    PROTOCOL_VERSION, check_peer_version, parse_major, protocol_compatible,
)


class TestParseMajor:
    def test_valid_v1(self):
        assert parse_major("fleet/v1") == 1

    def test_valid_v2(self):
        assert parse_major("fleet/v2") == 2

    def test_valid_large(self):
        assert parse_major("fleet/v99") == 99

    def test_invalid_format(self):
        assert parse_major("v1") is None

    def test_garbage(self):
        assert parse_major("not-a-version") is None

    def test_empty(self):
        assert parse_major("") is None


class TestProtocolCompatible:
    def test_same_version(self):
        assert protocol_compatible("fleet/v1", "fleet/v1") is True

    def test_same_major_both_v2(self):
        assert protocol_compatible("fleet/v2", "fleet/v2") is True

    def test_different_major(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert protocol_compatible("fleet/v1", "fleet/v2") is False
        assert "mismatch" in caplog.text

    def test_legacy_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert protocol_compatible("fleet/v1", None) is True
        assert "legacy" in caplog.text

    def test_legacy_empty(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert protocol_compatible("fleet/v1", "") is True
        assert "legacy" in caplog.text

    def test_unparseable_peer(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert protocol_compatible("fleet/v1", "garbage") is False
        assert "unparseable" in caplog.text


class TestCheckPeerVersion:
    def test_ok(self):
        assert check_peer_version(PROTOCOL_VERSION) == (True, "ok")

    def test_legacy_none(self):
        assert check_peer_version(None) == (True, "legacy")

    def test_legacy_empty(self):
        assert check_peer_version("") == (True, "legacy")

    def test_mismatch(self):
        assert check_peer_version("fleet/v999") == (False, "version_mismatch")

    def test_unparseable(self):
        assert check_peer_version("bogus") == (False, "unparseable")
