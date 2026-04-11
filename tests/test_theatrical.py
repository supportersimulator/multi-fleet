"""Tests for theatrical fleet presentation system."""

import io
import sys
import unittest
from unittest.mock import patch


class TestTheatricalGracefulDegradation(unittest.TestCase):
    """Verify all methods work when Rich is not installed."""

    def _make_theater_no_rich(self):
        """Create a FleetTheater that thinks Rich is unavailable."""
        import multifleet.theatrical as mod
        orig_rich = mod.HAS_RICH
        orig_console = mod.console
        mod.HAS_RICH = False
        mod.console = None
        theater = mod.FleetTheater()
        # Restore after creating instance — instance captured state
        mod.HAS_RICH = orig_rich
        mod.console = orig_console
        return theater

    def test_no_crash_without_rich(self):
        """Every method must complete without error when Rich is absent."""
        theater = self._make_theater_no_rich()
        # Capture stdout to avoid noise
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            theater.race_banner("Fix auth", ["mac1", "mac2"])
            theater.race_scoreboard({"participants": {"mac1": {"status": "racing"}}, "started_at": 0})
            theater.race_winner("mac1", "Fastest", ["evidence-1"])
            theater.gains_gate_display({"passed": True, "checks": [
                {"name": "daemon", "passed": True, "message": "ok", "latency_ms": 5, "critical": True}
            ], "total": 1, "passed_count": 1, "failed_count": 0, "critical_failures": 0,
                "duration_ms": 10, "recommendation": "All good"})
            theater.evidence_chain([{"seq": 1, "event_type": "test", "subject": "s",
                                     "entry_hash": "abc123", "prev_hash": "000000", "node_id": "mac1"}],
                                   verified=True)
            theater.probe_findings([{"severity": "high", "probe": "nats", "message": "down"}])
            theater.node_status_cards([{"name": "mac1", "status": "ok", "nats": True, "peers": 2}])
            theater.truth_ladder_display({"inbox": 10, "vision": 5, "snapshot": 2, "dao": 1})
            theater.supervisor_progress({"phase": "racing"})
            theater.fleet_banner()
            theater.doctor_display([{"code": "FD-001", "name": "test", "status": "pass", "message": "ok"}])

        output = buf.getvalue()
        self.assertIn("FLEET RACE", output)
        self.assertIn("mac1", output)


class TestTheatricalWithRich(unittest.TestCase):
    """Test Rich-powered output produces content without errors."""

    def setUp(self):
        try:
            from multifleet.theatrical import FleetTheater, HAS_RICH
            if not HAS_RICH:
                self.skipTest("Rich not installed")
            from rich.console import Console
            self._buf = io.StringIO()
            self._console = Console(file=self._buf, force_terminal=True, width=100)
            self.theater = FleetTheater(console_instance=self._console)
        except ImportError:
            self.skipTest("theatrical module not importable")

    def test_race_banner_output(self):
        self.theater.race_banner("Optimize the query layer", ["mac1", "mac2", "mac3"])
        output = self._buf.getvalue()
        self.assertIn("TASK", output)
        self.assertIn("Optimize the query layer", output)
        self.assertIn("Lane 1", output)
        self.assertIn("ON YOUR MARKS", output)
        self.assertIn("MAC1", output)

    def test_gains_gate_display(self):
        result = {
            "passed": True,
            "checks": [
                {"name": "daemon_health", "passed": True, "message": "Daemon up 500s",
                 "latency_ms": 3.2, "critical": True},
                {"name": "nats_connected", "passed": True, "message": "NATS connected",
                 "latency_ms": 1.1, "critical": True},
                {"name": "dedup_health", "passed": False, "message": "Large filter",
                 "latency_ms": 0.5, "critical": False},
            ],
            "total": 3, "passed_count": 2, "failed_count": 1,
            "critical_failures": 0, "duration_ms": 12.5,
            "recommendation": "Gate PASSED. 1 non-critical issue.",
        }
        self.theater.gains_gate_display(result)
        output = self._buf.getvalue()
        self.assertIn("Gains Gate", output)
        self.assertIn("PASSED", output)

    def test_probe_findings_display(self):
        findings = [
            {"severity": "critical", "probe": "nats_health", "message": "NATS unreachable", "node": "mac1"},
            {"severity": "medium", "probe": "disk_space", "message": "78% used", "node": "mac2"},
            {"severity": "low", "probe": "uptime", "message": "3 days", "node": "mac3"},
        ]
        self.theater.probe_findings(findings)
        output = self._buf.getvalue()
        self.assertIn("CRITICAL", output)
        self.assertIn("MEDIUM", output)

    def test_evidence_chain_display(self):
        entries = [
            {"seq": 1, "event_type": "race_started", "node_id": "mac1",
             "subject": "race:abc123", "entry_hash": "aabbccdd11223344", "prev_hash": "0000000000000000"},
            {"seq": 2, "event_type": "race_completed", "node_id": "mac2",
             "subject": "race:abc123", "entry_hash": "11223344aabbccdd", "prev_hash": "aabbccdd11223344"},
        ]
        self.theater.evidence_chain(entries, verified=True)
        output = self._buf.getvalue()
        self.assertIn("VERIFIED", output)

    def test_race_winner_display(self):
        self.theater.race_winner("mac2", "Highest confidence synthesis", ["Strong test coverage", "Clean diff"])
        output = self._buf.getvalue()
        self.assertIn("WINNER", output)
        self.assertIn("MAC2", output)

    def test_doctor_display(self):
        checks = [
            {"code": "FD-001", "name": "daemon_running", "status": "pass", "message": "OK", "latency_ms": 2},
            {"code": "FD-002", "name": "nats_transport", "status": "fail", "message": "Timeout", "latency_ms": 3000},
        ]
        self.theater.doctor_display(checks, "1/2 passed")
        output = self._buf.getvalue()
        self.assertIn("Fleet Doctor", output)

    def test_truth_ladder(self):
        self.theater.truth_ladder_display({"inbox": 20, "vision": 8, "snapshot": 3, "dao": 1})
        output = self._buf.getvalue()
        self.assertIn("Truth Ladder", output)
        self.assertIn("DAO", output)


if __name__ == "__main__":
    unittest.main()
