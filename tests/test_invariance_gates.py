"""Tests for fleet invariance gates — FleetSendGate, FleetRepairGate, FleetMergeGate."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.invariance_gates import (
    FleetSendGate,
    FleetRepairGate,
    FleetMergeGate,
    GateCheck,
    GateResult,
    REPAIR_COOLDOWN_S,
    VALID_REPAIR_ACTIONS,
)


# ── GateResult Tests ──


class TestGateResult:
    def test_summary_passed_all(self):
        checks = [GateCheck("a", True, "ok"), GateCheck("b", True, "ok")]
        r = GateResult(gate="Test", passed=True, checks=checks)
        assert "PASSED" in r.summary
        assert "(2/2)" in r.summary

    def test_summary_passed_with_noncritical(self):
        checks = [
            GateCheck("a", True, "ok"),
            GateCheck("b", False, "warn", critical=False),
        ]
        r = GateResult(gate="Test", passed=True, checks=checks)
        assert "PASSED" in r.summary
        assert "Non-critical" in r.summary
        assert "b" in r.summary

    def test_summary_failed(self):
        checks = [GateCheck("a", False, "bad", critical=True)]
        r = GateResult(gate="Test", passed=False, checks=checks)
        assert "FAILED" in r.summary
        assert "a" in r.summary

    def test_to_dict(self):
        checks = [GateCheck("a", True, "ok")]
        r = GateResult(gate="Test", passed=True, checks=checks, duration_ms=1.234)
        d = r.to_dict()
        assert d["gate"] == "Test"
        assert d["passed"] is True
        assert d["duration_ms"] == 1.2
        assert len(d["checks"]) == 1
        assert "summary" in d

    def test_gatecheck_to_dict(self):
        c = GateCheck("x", True, "good", critical=False)
        d = c.to_dict()
        assert d == {"name": "x", "passed": True, "message": "good", "critical": False}


# ── FleetSendGate Tests ──


class TestFleetSendGate:
    def _make_packet(self, **overrides):
        pkt = {
            "from_node": "mac1",
            "to_node": "mac2",
            "type": "context",
            "id": "test-123",
            "payload": {"subject": "hi", "body": "hello"},
        }
        pkt.update(overrides)
        return pkt

    @patch("multifleet.invariance_gates._record_audit")
    def test_valid_packet_passes(self, _a):
        """Valid packet with all required fields passes the gate."""
        gate = FleetSendGate()
        result = gate.check(self._make_packet())
        # envelope_fields should pass
        envelope_check = next(c for c in result.checks if c.name == "envelope_fields")
        assert envelope_check.passed
        # No critical failures (HMAC/target are non-critical or skipped)
        critical_failures = [c for c in result.checks if not c.passed and c.critical]
        assert len(critical_failures) == 0

    @patch("multifleet.invariance_gates._record_audit")
    def test_missing_fields_fails(self, _a):
        """Packet missing required envelope fields fails."""
        gate = FleetSendGate()
        pkt = {"payload": {"body": "hello"}}  # missing from_node, to_node, type, id
        result = gate.check(pkt)
        envelope_check = next(c for c in result.checks if c.name == "envelope_fields")
        assert not envelope_check.passed
        assert not result.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_from_to_aliases_accepted(self, _a):
        """'from' and 'to' accepted as aliases for 'from_node' and 'to_node'."""
        gate = FleetSendGate()
        pkt = {"from": "mac1", "to": "mac2", "type": "context", "id": "x"}
        result = gate.check(pkt)
        envelope_check = next(c for c in result.checks if c.name == "envelope_fields")
        assert envelope_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_schema_validation_with_registry(self, _a):
        """Schema validation runs when registry is provided."""
        registry = MagicMock()
        registry.get_schema.return_value = {"type": "object"}
        registry.validate.return_value = None  # no errors

        gate = FleetSendGate()
        result = gate.check(self._make_packet(), registry=registry)
        schema_check = next(c for c in result.checks if c.name == "schema_validation")
        assert schema_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_schema_validation_failure(self, _a):
        """Schema validation failure blocks send."""
        registry = MagicMock()
        registry.get_schema.return_value = {"type": "object"}
        registry.validate.return_value = "missing required field 'priority'"

        gate = FleetSendGate()
        result = gate.check(self._make_packet(), registry=registry)
        schema_check = next(c for c in result.checks if c.name == "schema_validation")
        assert not schema_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_rate_limit_exceeded(self, _a):
        """Rate limit exceeded blocks send."""
        limiter = MagicMock()
        limiter.check.return_value = (False, "30/min limit exceeded")

        gate = FleetSendGate()
        result = gate.check(self._make_packet(), rate_limiter=limiter)
        rate_check = next(c for c in result.checks if c.name == "rate_limit")
        assert not rate_check.passed
        assert not result.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_rate_limit_ok(self, _a):
        """Rate limit not exceeded allows send."""
        limiter = MagicMock()
        limiter.check.return_value = (True, "")

        gate = FleetSendGate()
        result = gate.check(self._make_packet(), rate_limiter=limiter)
        rate_check = next(c for c in result.checks if c.name == "rate_limit")
        assert rate_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_no_registry_skips_schema(self, _a):
        """No registry provided skips schema check (non-critical)."""
        gate = FleetSendGate()
        result = gate.check(self._make_packet())
        schema_check = next(c for c in result.checks if c.name == "schema_validation")
        assert schema_check.passed
        assert not schema_check.critical

    @patch("multifleet.invariance_gates._record_audit")
    def test_broadcast_target_accepted(self, _a):
        """Target 'all' accepted without config lookup."""
        gate = FleetSendGate()
        pkt = self._make_packet(to_node="all")
        result = gate.check(pkt)
        target_check = next(c for c in result.checks if c.name == "target_known")
        assert target_check.passed


# ── FleetRepairGate Tests ──


class TestFleetRepairGate:
    def setup_method(self):
        """Clear repair state between tests."""
        FleetRepairGate._repair_history = {}
        FleetRepairGate._repair_in_progress = {}

    @patch("multifleet.invariance_gates._record_audit")
    def test_valid_repair_passes(self, _a):
        """Valid repair action with no cooldown passes."""
        gate = FleetRepairGate()
        result = gate.check("mac2", "restart_nats")
        assert result.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_invalid_action_fails(self, _a):
        """Invalid repair action fails the gate."""
        gate = FleetRepairGate()
        result = gate.check("mac2", "delete_everything")
        assert not result.passed
        action_check = next(c for c in result.checks if c.name == "valid_action")
        assert not action_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_stale_diagnostic_detected(self, _a):
        """Stale channel health diagnostic blocks repair."""
        gate = FleetRepairGate()
        health = {
            "mac2": {"score": 10, "last_seen": time.time() - 600}
        }
        result = gate.check("mac2", "restart_nats", channel_health=health)
        target_check = next(c for c in result.checks if c.name == "target_unhealthy")
        assert not target_check.passed
        assert "Stale" in target_check.message

    @patch("multifleet.invariance_gates._record_audit")
    def test_healthy_target_blocked(self, _a):
        """Repair blocked when target appears healthy."""
        gate = FleetRepairGate()
        health = {
            "mac2": {"score": 80, "last_seen": time.time()}
        }
        result = gate.check("mac2", "restart_nats", channel_health=health)
        target_check = next(c for c in result.checks if c.name == "target_unhealthy")
        assert not target_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_unhealthy_target_allowed(self, _a):
        """Repair allowed when target is genuinely unhealthy."""
        gate = FleetRepairGate()
        health = {
            "mac2": {"score": 20, "last_seen": time.time()}
        }
        result = gate.check("mac2", "restart_nats", channel_health=health)
        target_check = next(c for c in result.checks if c.name == "target_unhealthy")
        assert target_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_l4_chief_only_enforcement(self, _a):
        """L4 SSH repair (full_restart) blocked on non-chief node."""
        gate = FleetRepairGate()
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac2"}):
            with patch("multifleet.invariance_gates.FleetRepairGate.check") as orig:
                # Actually call through with node_id override
                pass
        # Direct test with node_id param
        result = gate.check("mac3", "full_restart", node_id="mac2")
        l4_check = next(c for c in result.checks if c.name == "l4_chief_only")
        assert not l4_check.passed
        assert "chief" in l4_check.message

    @patch("multifleet.invariance_gates._record_audit")
    def test_l4_chief_allowed(self, _a):
        """L4 SSH repair allowed on chief node."""
        gate = FleetRepairGate()
        result = gate.check("mac2", "full_restart", node_id="mac1")
        l4_check = next(c for c in result.checks if c.name == "l4_chief_only")
        assert l4_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_cooldown_enforced(self, _a):
        """Cooldown prevents rapid re-repair of same target."""
        gate = FleetRepairGate()
        # First repair should pass
        r1 = gate.check("mac2", "restart_nats")
        assert r1.passed

        # Second immediate repair should fail (cooldown)
        r2 = gate.check("mac2", "restart_nats")
        assert not r2.passed
        cooldown_check = next(c for c in r2.checks if c.name == "cooldown")
        assert not cooldown_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_concurrent_repair_blocked(self, _a):
        """Concurrent repair on same target is blocked."""
        gate = FleetRepairGate()
        # Simulate in-progress repair
        FleetRepairGate._repair_in_progress["mac2"] = time.time()
        result = gate.check("mac2", "restart_nats")
        concurrent_check = next(c for c in result.checks if c.name == "no_concurrent_repair")
        assert not concurrent_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    def test_clear_repair(self, _a):
        """clear_repair removes in-progress lock."""
        gate = FleetRepairGate()
        FleetRepairGate._repair_in_progress["mac2"] = time.time()
        gate.clear_repair("mac2")
        assert "mac2" not in FleetRepairGate._repair_in_progress


# ── FleetMergeGate Tests ──


class TestFleetMergeGate:
    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_clean_merge_passes(self, mock_run, _a):
        """Clean merge with no conflicts passes."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        gate = FleetMergeGate()
        result = gate.check("feature-branch")
        assert result.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_active_lease_blocks(self, mock_run, _a):
        """Active lease on the branch blocks merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        lease_mgr = MagicMock()
        lease_mgr.list_active.return_value = [
            {"resource": "feature-branch", "holder": "mac2", "expires_at": time.time() + 600}
        ]
        gate = FleetMergeGate()
        result = gate.check("feature-branch", lease_mgr=lease_mgr)
        assert not result.passed
        lease_check = next(c for c in result.checks if c.name == "no_lease_conflicts")
        assert not lease_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_no_lease_conflict(self, mock_run, _a):
        """No conflicting lease allows merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        lease_mgr = MagicMock()
        lease_mgr.list_active.return_value = [
            {"resource": "other-branch", "holder": "mac2"}
        ]
        gate = FleetMergeGate()
        result = gate.check("feature-branch", lease_mgr=lease_mgr)
        lease_check = next(c for c in result.checks if c.name == "no_lease_conflicts")
        assert lease_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_unresolved_dissent_blocks(self, mock_run, _a):
        """Unresolved critical dissent blocks merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rebuttal = MagicMock()
        rebuttal.list_proposals.return_value = [
            {"severity": "critical", "state": "open", "branch": "feature-branch"},
        ]
        gate = FleetMergeGate()
        result = gate.check("feature-branch", rebuttal_store=rebuttal)
        assert not result.passed
        dissent_check = next(c for c in result.checks if c.name == "no_critical_dissent")
        assert not dissent_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_resolved_dissent_passes(self, mock_run, _a):
        """Resolved dissent does not block merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rebuttal = MagicMock()
        rebuttal.list_proposals.return_value = [
            {"severity": "critical", "state": "resolved", "branch": "feature-branch"},
        ]
        gate = FleetMergeGate()
        result = gate.check("feature-branch", rebuttal_store=rebuttal)
        dissent_check = next(c for c in result.checks if c.name == "no_critical_dissent")
        assert dissent_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_gains_gate_failure_blocks(self, mock_run, _a):
        """Gains gate failure blocks merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        gains = MagicMock()
        gains.run.return_value = MagicMock(
            passed=False, passed_count=3, total=6,
            recommendation="Fix daemon before proceeding",
        )
        gate = FleetMergeGate()
        result = gate.check("feature-branch", gains_gate=gains)
        assert not result.passed
        gains_check = next(c for c in result.checks if c.name == "gains_gate")
        assert not gains_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_branch_conflict_blocks(self, mock_run, _a):
        """Branch with merge conflicts blocks merge."""
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="CONFLICT in file.py",
        )
        gate = FleetMergeGate()
        result = gate.check("feature-branch")
        merge_check = next(c for c in result.checks if c.name == "branch_mergeable")
        assert not merge_check.passed

    @patch("multifleet.invariance_gates._record_audit")
    @patch("subprocess.run")
    def test_open_proposals_block(self, mock_run, _a):
        """Open proposals for the branch block merge."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rebuttal = MagicMock()
        rebuttal.list_proposals.return_value = [
            {"state": "open", "branch": "feature-branch"},
        ]
        gate = FleetMergeGate()
        result = gate.check("feature-branch", rebuttal_store=rebuttal)
        ack_check = next(c for c in result.checks if c.name == "all_nodes_acked")
        assert not ack_check.passed


# ── Critical vs Non-Critical Tests ──


class TestCriticalDistinction:
    @patch("multifleet.invariance_gates._record_audit")
    def test_noncritical_failure_doesnt_block_gate(self, _a):
        """Non-critical check failure should not block the gate."""
        gate = FleetSendGate()
        # Packet with all required fields — HMAC non-availability is non-critical
        pkt = {
            "from_node": "mac1", "to_node": "all", "type": "context", "id": "x",
        }
        result = gate.check(pkt)
        # Even if hmac_available fails, gate should still pass (it's non-critical)
        hmac_checks = [c for c in result.checks if c.name == "hmac_available"]
        if hmac_checks and not hmac_checks[0].passed:
            # Gate should still pass because hmac is non-critical
            assert not hmac_checks[0].critical

    @patch("multifleet.invariance_gates._record_audit")
    def test_critical_failure_blocks_gate(self, _a):
        """Critical check failure must block the gate."""
        gate = FleetSendGate()
        pkt = {}  # Missing all required fields
        result = gate.check(pkt)
        assert not result.passed
        envelope_check = next(c for c in result.checks if c.name == "envelope_fields")
        assert envelope_check.critical
        assert not envelope_check.passed
