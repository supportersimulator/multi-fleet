"""Tests for ContextDNA Probe Engine.

Covers: registration, execution, result contracts, caching,
timing, serialization, and the 3 built-in probes.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import (
    ProbeCategory,
    ProbeEngine,
    ProbeFinding,
    ProbeRegistry,
    ProbeResult,
    ProbeSeverity,
)


# ── Helpers ──

def _ok_probe(context: dict) -> ProbeResult:
    """A simple probe that always returns ok."""
    return ProbeResult(
        probe_id="test_ok",
        category="structure",
        status="ok",
        confidence=0.95,
        severity="info",
    )


def _warn_probe(context: dict) -> ProbeResult:
    """A probe that returns a warning with findings."""
    return ProbeResult(
        probe_id="test_warn",
        category="context_quality",
        status="warning",
        confidence=0.7,
        severity="medium",
        findings=[
            ProbeFinding(
                title="Something drifted",
                evidence=["file_a.py moved", "import changed"],
                blast_radius="medium",
            )
        ],
    )


def _crash_probe(context: dict) -> ProbeResult:
    """A probe that raises an exception."""
    raise RuntimeError("probe exploded")


def _slow_probe(context: dict) -> ProbeResult:
    """A probe that takes measurable time."""
    time.sleep(0.05)
    return ProbeResult(
        probe_id="test_slow",
        category="reliability",
        status="ok",
        confidence=0.9,
        severity="info",
    )


def _bad_return_probe(context: dict):
    """A probe that returns the wrong type."""
    return {"not": "a ProbeResult"}


# ── ProbeResult tests ──

class TestProbeResult:

    def test_to_dict_roundtrip(self):
        r = ProbeResult(
            probe_id="test", category="structure", status="ok",
            confidence=0.8, severity="low",
            findings=[ProbeFinding(title="x", evidence=["y"])],
        )
        d = r.to_dict()
        assert d["probe_id"] == "test"
        assert d["status"] == "ok"
        assert d["confidence"] == 0.8
        assert len(d["findings"]) == 1
        assert d["findings"][0]["title"] == "x"

    def test_to_json_valid(self):
        r = ProbeResult(
            probe_id="test", category="change", status="warning",
            confidence=0.5, severity="high",
        )
        j = r.to_json()
        parsed = json.loads(j)
        assert parsed["probe_id"] == "test"
        assert parsed["severity"] == "high"

    def test_error_factory(self):
        r = ProbeResult.error("broken", "security", "disk full")
        assert r.status == "error"
        assert r.confidence == 0.0
        assert "disk full" in r.findings[0].title

    def test_skipped_factory(self):
        r = ProbeResult.skipped("missing", "structure", ["git", "ripgrep"])
        assert r.status == "skipped"
        assert r.missing_inputs == ["git", "ripgrep"]

    def test_generated_at_populated(self):
        r = ProbeResult(
            probe_id="t", category="change", status="ok",
            confidence=1.0, severity="info",
        )
        assert r.generated_at.endswith("Z")
        assert "T" in r.generated_at


# ── ProbeRegistry tests ──

class TestProbeRegistry:

    def test_register_and_list(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, "desc1")
        reg.register("p2", _warn_probe, ProbeCategory.CONTEXT_QUALITY, "desc2")
        probes = reg.list_probes()
        assert len(probes) == 2
        ids = {p["probe_id"] for p in probes}
        assert ids == {"p1", "p2"}

    def test_register_overwrite_warns(self, caplog):
        reg = ProbeRegistry()
        reg.register("dup", _ok_probe, ProbeCategory.STRUCTURE)
        import logging
        with caplog.at_level(logging.WARNING, logger="fleet-nerve.probes"):
            reg.register("dup", _warn_probe, ProbeCategory.CHANGE)
        assert any("overwrite" in r.message for r in caplog.records)

    def test_run_all(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("p2", _warn_probe, ProbeCategory.CONTEXT_QUALITY)
        results = reg.run_all()
        assert len(results) == 2
        statuses = {r.status for r in results}
        assert "ok" in statuses
        assert "warning" in statuses

    def test_run_category_filters(self):
        reg = ProbeRegistry()
        reg.register("s1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("cq1", _warn_probe, ProbeCategory.CONTEXT_QUALITY)
        results = reg.run_category(ProbeCategory.STRUCTURE)
        assert len(results) == 1
        assert results[0].category == "structure" or results[0].probe_id == "test_ok"

    def test_run_one_existing(self):
        reg = ProbeRegistry()
        reg.register("s1", _ok_probe, ProbeCategory.STRUCTURE)
        result = reg.run_one("s1")
        assert result.status == "ok"

    def test_run_one_missing(self):
        reg = ProbeRegistry()
        result = reg.run_one("nonexistent")
        assert result.status == "error"
        assert "not registered" in result.findings[0].title

    def test_run_by_trigger(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, triggers=["commit"])
        reg.register("p2", _warn_probe, ProbeCategory.CHANGE, triggers=["file_save"])
        results = reg.run_by_trigger("commit")
        assert len(results) == 1

    def test_crash_probe_returns_error(self):
        reg = ProbeRegistry()
        reg.register("crash", _crash_probe, ProbeCategory.SECURITY)
        result = reg.run_one("crash")
        assert result.status == "error"
        assert "exploded" in result.findings[0].title

    def test_bad_return_type_returns_error(self):
        reg = ProbeRegistry()
        reg.register("bad", _bad_return_probe, ProbeCategory.STRUCTURE)
        result = reg.run_one("bad")
        assert result.status == "error"
        assert "dict" in result.findings[0].title

    def test_timing_captured(self):
        reg = ProbeRegistry()
        reg.register("slow", _slow_probe, ProbeCategory.RELIABILITY)
        result = reg.run_one("slow")
        assert result.duration_ms >= 40  # at least 40ms for 50ms sleep


# ── ProbeEngine tests ──

class TestProbeEngine:

    def test_scan_all(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, triggers=["manual"])
        reg.register("p2", _warn_probe, ProbeCategory.CHANGE, triggers=["manual"])
        engine = ProbeEngine(registry=reg)
        results = engine.scan(trigger="all")
        assert len(results) == 2

    def test_scan_trigger_filters(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, triggers=["commit"])
        reg.register("p2", _warn_probe, ProbeCategory.CHANGE, triggers=["file_save"])
        engine = ProbeEngine(registry=reg)
        results = engine.scan(trigger="commit")
        assert len(results) == 1

    def test_scan_category(self):
        reg = ProbeRegistry()
        reg.register("s1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("cq1", _warn_probe, ProbeCategory.CONTEXT_QUALITY)
        engine = ProbeEngine(registry=reg)
        results = engine.scan_category("structure")
        assert len(results) == 1

    def test_scan_category_invalid(self):
        engine = ProbeEngine()
        results = engine.scan_category("fake_category")
        assert len(results) == 1
        assert results[0].status == "error"

    def test_latest_results_cached(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, triggers=["manual"])
        engine = ProbeEngine(registry=reg)
        engine.scan(trigger="all")
        cached = engine.latest_results()
        assert len(cached) == 1
        assert cached[0].status == "ok"

    def test_findings_summary(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE, triggers=["manual"])
        reg.register("p2", _warn_probe, ProbeCategory.CONTEXT_QUALITY, triggers=["manual"])
        engine = ProbeEngine(registry=reg)
        engine.scan(trigger="all")
        summary = engine.findings_summary()
        assert summary["total_probes"] == 2
        assert summary["total_findings"] == 1
        assert "ok" in summary["by_status"]
        assert "warning" in summary["by_status"]

    def test_findings_summary_empty(self):
        engine = ProbeEngine()
        summary = engine.findings_summary()
        assert summary["total_probes"] == 0
        assert summary["total_findings"] == 0
        assert summary["last_scan_at"] is None

    def test_scan_one(self):
        reg = ProbeRegistry()
        reg.register("p1", _ok_probe, ProbeCategory.STRUCTURE)
        engine = ProbeEngine(registry=reg)
        result = engine.scan_one("p1")
        assert result.status == "ok"
        # Verify it was added to cache
        assert len(engine.latest_results()) == 1


# ── Mirror Drift Probe tests ──

class TestMirrorDriftProbe:

    def test_detects_missing_paths(self, tmp_path):
        """CLAUDE.md references a file that doesn't exist."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("Use `scripts/missing-tool.sh` for deployment.\n")

        from multifleet.probes.mirror_drift import probe_mirror_drift
        result = probe_mirror_drift({"repo_root": str(tmp_path)})
        assert result.status == "warning"
        assert len(result.findings) > 0
        assert any("missing" in f.title.lower() or "non-existent" in f.title.lower() for f in result.findings)

    def test_ok_when_paths_exist(self, tmp_path):
        """All referenced paths exist — should return ok."""
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "deploy.sh").touch()
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("Run `scripts/deploy.sh` to deploy.\n")

        from multifleet.probes.mirror_drift import probe_mirror_drift
        result = probe_mirror_drift({"repo_root": str(tmp_path)})
        assert result.status == "ok"

    def test_skipped_when_no_docs(self, tmp_path):
        """No CLAUDE.md or README.md — should skip."""
        from multifleet.probes.mirror_drift import probe_mirror_drift
        result = probe_mirror_drift({"repo_root": str(tmp_path)})
        assert result.status == "skipped"
        assert len(result.missing_inputs) == 2


# ── Stale Context Probe tests ──

class TestStaleContextProbe:

    def test_detects_stale_messages(self, tmp_path):
        """Fleet message older than 7 days triggers finding."""
        msg_dir = tmp_path / ".fleet-messages" / "mac1"
        msg_dir.mkdir(parents=True)
        old_msg = msg_dir / "old-task.md"
        old_msg.write_text("---\nfrom: mac2\nsubject: old task\n---\n")
        # Set mtime to 10 days ago
        old_time = time.time() - (10 * 86400)
        os.utime(old_msg, (old_time, old_time))

        from multifleet.probes.stale_context import probe_stale_context
        result = probe_stale_context({"repo_root": str(tmp_path)})
        assert result.status == "warning"
        assert any("7 days" in f.title for f in result.findings)

    def test_ok_when_fresh(self, tmp_path):
        """Recent messages should not trigger."""
        msg_dir = tmp_path / ".fleet-messages" / "mac1"
        msg_dir.mkdir(parents=True)
        (msg_dir / "fresh.md").write_text("fresh message")

        from multifleet.probes.stale_context import probe_stale_context
        result = probe_stale_context({"repo_root": str(tmp_path)})
        assert result.status == "ok"

    def test_detects_stale_plans(self, tmp_path):
        """Plan doc >30 days with NOT STARTED triggers finding."""
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        plan = plans_dir / "old-feature.md"
        plan.write_text("# Old Feature\nStatus: NOT STARTED\n")
        old_time = time.time() - (45 * 86400)
        os.utime(plan, (old_time, old_time))

        from multifleet.probes.stale_context import probe_stale_context
        result = probe_stale_context({"repo_root": str(tmp_path)})
        assert result.status == "warning"
        assert any("NOT STARTED" in f.title for f in result.findings)


# ── Fleet Health Probe tests ──

class TestFleetHealthProbe:

    def test_wraps_doctor_results(self):
        """Fleet health probe translates doctor checks to findings."""
        from multifleet.probes.fleet_health import probe_fleet_health
        from multifleet.doctor import Check, DoctorReport

        mock_report = DoctorReport(
            checks=[
                Check("FD-001", "daemon_running", "pass", "Up 100s"),
                Check("FD-002", "nats_server", "fail", "Not reachable"),
                Check("FD-003", "peers_visible", "warn", "Only 1 peer"),
            ],
            duration_ms=50.0,
            node_id="mac1",
        )

        with patch("multifleet.doctor.FleetDoctor") as MockDoc:
            MockDoc.return_value.run.return_value = mock_report
            result = probe_fleet_health({})

        assert result.probe_id == "fleet_health"
        assert result.status == "warning"
        assert len(result.findings) == 2  # fail + warn
        assert any("nats_server" in f.title for f in result.findings)

    def test_healthy_fleet_returns_ok(self):
        """All checks pass — should return ok."""
        from multifleet.probes.fleet_health import probe_fleet_health
        from multifleet.doctor import Check, DoctorReport

        mock_report = DoctorReport(
            checks=[
                Check("FD-001", "daemon_running", "pass", "Up 100s"),
                Check("FD-002", "nats_server", "pass", "Connected"),
            ],
            duration_ms=20.0,
            node_id="mac1",
        )

        with patch("multifleet.doctor.FleetDoctor") as MockDoc:
            MockDoc.return_value.run.return_value = mock_report
            result = probe_fleet_health({})

        assert result.status == "ok"
        assert len(result.findings) == 0


# ── Integration: register_all + create_engine ──

class TestProbeIntegration:

    def test_create_engine_registers_all(self):
        from multifleet.probes import create_engine
        engine = create_engine()
        probes = engine.registry.list_probes()
        ids = {p["probe_id"] for p in probes}
        assert "mirror_drift" in ids
        assert "stale_context" in ids
        assert "fleet_health" in ids

    def test_register_all_categories(self):
        from multifleet.probes import create_engine
        engine = create_engine()
        probes = engine.registry.list_probes()
        cats = {p["category"] for p in probes}
        assert "context_quality" in cats
        assert "reliability" in cats
