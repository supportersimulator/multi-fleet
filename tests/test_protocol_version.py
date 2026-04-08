"""Protocol version negotiation tests.

Validates version parsing, compatibility checks, forward/backward compat,
and graceful handling of missing/malformed version strings.
"""

import logging
import pytest
from multifleet.protocol import (
    PROTOCOL_VERSION,
    parse_major,
    protocol_compatible,
    check_peer_version,
)


# ── parse_major ──


class TestParseMajor:
    def test_parse_v1(self):
        assert parse_major("fleet/v1") == 1

    def test_parse_v2(self):
        assert parse_major("fleet/v2") == 2

    def test_parse_v99(self):
        assert parse_major("fleet/v99") == 99

    def test_parse_garbage_returns_none(self):
        assert parse_major("not-a-version") is None

    def test_parse_empty_returns_none(self):
        assert parse_major("") is None

    def test_parse_partial_prefix(self):
        assert parse_major("fleet/") is None

    def test_parse_wrong_prefix(self):
        assert parse_major("http/v1") is None

    def test_parse_no_number(self):
        assert parse_major("fleet/vX") is None


# ── protocol_compatible ──


class TestProtocolCompatible:
    def test_same_version_ok(self):
        assert protocol_compatible("fleet/v1", "fleet/v1") is True

    def test_packet_with_version_field_accepted(self):
        """Packet carrying a matching version string is accepted."""
        assert protocol_compatible(PROTOCOL_VERSION, PROTOCOL_VERSION) is True

    def test_packet_without_version_gets_default(self):
        """None/empty version treated as legacy — compatible with warning."""
        assert protocol_compatible("fleet/v1", None) is True
        assert protocol_compatible("fleet/v1", "") is True

    def test_version_mismatch_logged_as_warning(self, caplog):
        """Mismatched major versions log a warning."""
        with caplog.at_level(logging.WARNING):
            result = protocol_compatible("fleet/v1", "fleet/v2")
        assert result is False
        assert "version mismatch" in caplog.text.lower()

    def test_forward_compat_newer_version(self, caplog):
        """Newer peer version accepted when same major (future minor/patch).

        Current protocol uses only major — fleet/v1 vs fleet/v1 is same.
        A truly newer major (v2) is incompatible by design.
        """
        # Same major = compatible
        assert protocol_compatible("fleet/v1", "fleet/v1") is True

    def test_backward_compat_older_version(self, caplog):
        """Older peer version. With major-only versioning, v1 vs v1 is fine.
        v1 local vs None peer = legacy compat with warning.
        """
        with caplog.at_level(logging.WARNING):
            result = protocol_compatible("fleet/v1", None)
        assert result is True
        assert "legacy" in caplog.text.lower()

    def test_unparseable_peer_incompatible(self, caplog):
        """Unparseable peer version returns False and logs warning."""
        with caplog.at_level(logging.WARNING):
            result = protocol_compatible("fleet/v1", "garbage")
        assert result is False
        assert "unparseable" in caplog.text.lower()


# ── check_peer_version ──


class TestCheckPeerVersion:
    def test_matching_version_returns_ok(self):
        compat, status = check_peer_version(PROTOCOL_VERSION)
        assert compat is True
        assert status == "ok"

    def test_none_returns_legacy(self):
        compat, status = check_peer_version(None)
        assert compat is True
        assert status == "legacy"

    def test_empty_returns_legacy(self):
        compat, status = check_peer_version("")
        assert compat is True
        assert status == "legacy"

    def test_unparseable_returns_incompatible(self):
        compat, status = check_peer_version("random-string")
        assert compat is False
        assert status == "unparseable"

    def test_different_major_returns_mismatch(self):
        compat, status = check_peer_version("fleet/v999")
        assert compat is False
        assert status == "version_mismatch"

    def test_return_types(self):
        """Return value is always (bool, str)."""
        for v in [PROTOCOL_VERSION, None, "", "bad", "fleet/v999"]:
            compat, status = check_peer_version(v)
            assert isinstance(compat, bool)
            assert isinstance(status, str)
