"""Tests for ContextDNA Code Scanner (GhostScan Probe Engine).

DEPRECATED: code_scanner has been merged into the unified probes/ system.
These tests are skipped if code_scanner is not available.
See test_probes_unified.py for current probe tests.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

try:
    from multifleet.code_scanner import ProbeEngine, create_engine
    from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult
    from multifleet.code_scanner.evidence_bridge import ProbeEvidenceBridge
    _HAS_CODE_SCANNER = True
except (ImportError, ModuleNotFoundError, NameError):
    _HAS_CODE_SCANNER = False
    # Stubs so module-level type annotations don't raise NameError at collection
    ProbeContext = ProbeResult = Finding = ProbeEngine = create_engine = None
    ProbeEvidenceBridge = None

pytestmark = pytest.mark.skipif(
    not _HAS_CODE_SCANNER,
    reason="code_scanner removed — probes unified into multifleet.probes",
)


# ── ProbeContract tests ──

class TestProbeContract:

    def test_finding_to_dict(self):
        f = Finding(
            title="Stale ref", summary="File missing", severity="high",
            confidence=0.9, blast_radius="medium",
            evidence_refs=["ref1"], suggested_actions=["fix it"],
            file_path="foo.py", line_number=42,
        )
        d = f.to_dict()
        assert d["title"] == "Stale ref"
        assert d["severity"] == "high"
        assert d["line_number"] == 42

    def test_probe_result_ok(self):
        r = ProbeResult.ok("test_probe", confidence=0.95)
        assert r.status == "ok"
        assert r.confidence == 0.95
        assert r.findings == []

    def test_probe_result_error(self):
        r = ProbeResult.error("broken", "disk full")
        assert r.status == "error"
        assert "disk full" in r.findings[0].title

    def test_probe_result_skipped(self):
        r = ProbeResult.skipped("missing", ["git", "rg"])
        assert r.status == "skipped"
        assert r.missing_inputs == ["git", "rg"]

    def test_probe_result_to_dict(self):
        r = ProbeResult(
            probe_id="test", status="warn", confidence=0.7,
            findings=[Finding(
                title="drift", summary="x", severity="medium", confidence=0.8,
            )],
        )
        d = r.to_dict()
        assert d["probe_id"] == "test"
        assert len(d["findings"]) == 1
        assert isinstance(d["generated_at"], float)

    def test_probe_context_to_dict(self):
        ctx = ProbeContext(
            repo_path="/tmp/repo", branch="main",
            changed_files=["a.py"], node_id="mac1",
        )
        d = ctx.to_dict()
        assert d["repo_path"] == "/tmp/repo"
        assert d["branch"] == "main"


# ── ProbeEngine tests ──

def _ok_probe(ctx: ProbeContext) -> ProbeResult:
    return ProbeResult.ok("ok_probe")

def _warn_probe(ctx: ProbeContext) -> ProbeResult:
    return ProbeResult(
        probe_id="warn_probe", status="warn", confidence=0.7,
        findings=[Finding(title="drift", summary="x", severity="medium", confidence=0.8)],
    )

def _crash_probe(ctx: ProbeContext) -> ProbeResult:
    raise RuntimeError("boom")

def _bad_probe(ctx: ProbeContext):
    return {"not": "a ProbeResult"}


class TestProbeEngine:

    def test_register_and_list(self):
        engine = ProbeEngine()
        engine.register("p1", _ok_probe, "structure", "cheap")
        engine.register("p2", _warn_probe, "context_quality", "medium")
        probes = engine.list_probes()
        assert len(probes) == 2
        ids = {p["probe_id"] for p in probes}
        assert ids == {"p1", "p2"}

    def test_register_invalid_cost(self):
        engine = ProbeEngine()
        with pytest.raises(ValueError, match="Invalid cost"):
            engine.register("p1", _ok_probe, "structure", "ultra")

    def test_run_all(self):
        engine = ProbeEngine()
        engine.register("p1", _ok_probe, "structure", "cheap")
        engine.register("p2", _warn_probe, "context_quality", "medium")
        ctx = ProbeContext(repo_path="/tmp")
        results = engine.run_all(ctx)
        assert len(results) == 2

    def test_cost_filtering(self):
        engine = ProbeEngine()
        engine.register("cheap1", _ok_probe, "structure", "cheap")
        engine.register("med1", _warn_probe, "context_quality", "medium")
        engine.register("exp1", _ok_probe, "structure", "expensive")
        ctx = ProbeContext(repo_path="/tmp")

        cheap = engine.run_cheap(ctx)
        assert len(cheap) == 1

        medium = engine.run_medium(ctx)
        assert len(medium) == 2

        all_results = engine.run_all(ctx)
        assert len(all_results) == 3

    def test_run_one(self):
        engine = ProbeEngine()
        engine.register("p1", _ok_probe, "structure", "cheap")
        ctx = ProbeContext(repo_path="/tmp")
        result = engine.run_one("p1", ctx)
        assert result.status == "ok"

    def test_run_one_missing(self):
        engine = ProbeEngine()
        ctx = ProbeContext(repo_path="/tmp")
        result = engine.run_one("nonexistent", ctx)
        assert result.status == "error"

    def test_crash_probe_returns_error(self):
        engine = ProbeEngine()
        engine.register("crash", _crash_probe, "structure", "cheap")
        ctx = ProbeContext(repo_path="/tmp")
        result = engine.run_one("crash", ctx)
        assert result.status == "error"
        assert "boom" in result.findings[0].title

    def test_bad_return_type(self):
        engine = ProbeEngine()
        engine.register("bad", _bad_probe, "structure", "cheap")
        ctx = ProbeContext(repo_path="/tmp")
        result = engine.run_one("bad", ctx)
        assert result.status == "error"

    def test_timing_captured(self):
        def slow_probe(ctx):
            time.sleep(0.05)
            return ProbeResult.ok("slow")
        engine = ProbeEngine()
        engine.register("slow", slow_probe, "structure", "cheap")
        ctx = ProbeContext(repo_path="/tmp")
        result = engine.run_one("slow", ctx)
        assert result.duration_ms >= 40

    def test_stats_updated(self):
        engine = ProbeEngine()
        engine.register("p1", _ok_probe, "structure", "cheap")
        engine.register("p2", _warn_probe, "context_quality", "cheap")
        ctx = ProbeContext(repo_path="/tmp")
        engine.run_all(ctx)
        assert engine.stats["total_runs"] == 2
        assert engine.stats["total_findings"] == 1

    def test_discover_probes(self):
        engine = ProbeEngine()
        count = engine.discover_probes()
        assert count >= 6  # mirror_drift, hidden_dependency, blast_radius, stale_context, security_surface, injection_prioritizer
        ids = {p["probe_id"] for p in engine.list_probes()}
        assert "mirror_drift" in ids
        assert "hidden_dependency" in ids
        assert "blast_radius" in ids
        assert "stale_context" in ids
        assert "security_surface" in ids
        assert "injection_prioritizer" in ids

    def test_create_engine_factory(self):
        engine = create_engine()
        ids = {p["probe_id"] for p in engine.list_probes()}
        assert "mirror_drift" in ids


# ── Mirror Drift Probe tests ──

class TestMirrorDriftProbe:

    def test_detects_missing_paths(self, tmp_path):
        """CLAUDE.md references a file that doesn't exist."""
        (tmp_path / "CLAUDE.md").write_text(
            "Use `scripts/missing-tool.sh` for deployment.\n"
        )
        from multifleet.code_scanner.probes.mirror_drift import probe_mirror_drift
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_mirror_drift(ctx)
        assert result.status == "warn"
        assert any("missing" in f.title.lower() for f in result.findings)

    def test_ok_when_paths_exist(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "deploy.sh").touch()
        (tmp_path / "CLAUDE.md").write_text("Run `scripts/deploy.sh` to deploy.\n")

        from multifleet.code_scanner.probes.mirror_drift import probe_mirror_drift
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_mirror_drift(ctx)
        assert result.status == "ok"

    def test_skipped_when_no_docs(self, tmp_path):
        from multifleet.code_scanner.probes.mirror_drift import probe_mirror_drift
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_mirror_drift(ctx)
        assert result.status == "skipped"

    def test_detects_env_var_drift(self, tmp_path):
        """Code uses env vars not in .env.example."""
        (tmp_path / "CLAUDE.md").write_text("# Config\n")
        (tmp_path / ".env.example").write_text("DATABASE_URL=\nAPI_KEY=\n")
        (tmp_path / "app.py").write_text(
            'import os\nSECRET = os.environ.get("CUSTOM_SECRET")\n'
        )
        from multifleet.code_scanner.probes.mirror_drift import probe_mirror_drift
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_mirror_drift(ctx)
        # Should detect CUSTOM_SECRET not in .env.example
        env_findings = [f for f in result.findings if "env var" in f.title.lower()]
        assert len(env_findings) > 0

    def test_detects_param_drift(self, tmp_path):
        """Docstring documents a param that doesn't exist in signature."""
        (tmp_path / "CLAUDE.md").write_text("# Docs\n")
        code = '''
def process(data, timeout):
    """Process data.

    :param data: The input data.
    :param timeout: Timeout in seconds.
    :param callback: Called on completion.
    """
    pass
'''
        (tmp_path / "module.py").write_text(code)
        from multifleet.code_scanner.probes.mirror_drift import probe_mirror_drift
        ctx = ProbeContext(repo_path=str(tmp_path), changed_files=["module.py"])
        result = probe_mirror_drift(ctx)
        param_findings = [f for f in result.findings if "param" in f.title.lower()]
        assert len(param_findings) > 0
        assert any("callback" in str(f.evidence_refs) for f in param_findings)


# ─��� Hidden Dependency Probe tests ──

class TestHiddenDependencyProbe:

    def test_detects_undeclared_import(self, tmp_path):
        """Import a package not in requirements.txt."""
        (tmp_path / "requirements.txt").write_text("flask>=2.0\n")
        (tmp_path / "app.py").write_text("import pandas\nimport flask\n")

        from multifleet.code_scanner.probes.hidden_dependency import probe_hidden_dependency
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_hidden_dependency(ctx)
        assert result.status == "warn"
        assert any("pandas" in f.title.lower() for f in result.findings)

    def test_ok_when_all_declared(self, tmp_path):
        """All imports are either stdlib or declared."""
        (tmp_path / "requirements.txt").write_text("requests>=2.0\n")
        (tmp_path / "app.py").write_text("import os\nimport json\n")

        from multifleet.code_scanner.probes.hidden_dependency import probe_hidden_dependency
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_hidden_dependency(ctx)
        assert result.status == "ok"

    def test_skipped_when_no_py_files(self, tmp_path):
        from multifleet.code_scanner.probes.hidden_dependency import probe_hidden_dependency
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_hidden_dependency(ctx)
        assert result.status == "skipped"


# ── Blast Radius Probe tests ��─

class TestBlastRadiusProbe:

    def test_skipped_without_changed_files(self, tmp_path):
        from multifleet.code_scanner.probes.blast_radius import probe_blast_radius
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_blast_radius(ctx)
        assert result.status == "skipped"

    def test_detects_high_blast_radius(self, tmp_path):
        """A module imported by many files has high blast radius."""
        # Create a core module
        (tmp_path / "core.py").write_text("def important(): pass\n")
        # Create 12 files that import it
        for i in range(12):
            (tmp_path / f"user_{i}.py").write_text(f"from core import important\n")

        from multifleet.code_scanner.probes.blast_radius import probe_blast_radius
        ctx = ProbeContext(repo_path=str(tmp_path), changed_files=["core.py"])
        result = probe_blast_radius(ctx)
        assert result.status == "warn"
        assert any("high" in f.blast_radius for f in result.findings)

    def test_ok_for_isolated_file(self, tmp_path):
        """A file imported by nobody has low blast radius."""
        (tmp_path / "isolated.py").write_text("x = 1\n")
        (tmp_path / "other.py").write_text("y = 2\n")

        from multifleet.code_scanner.probes.blast_radius import probe_blast_radius
        ctx = ProbeContext(repo_path=str(tmp_path), changed_files=["isolated.py"])
        result = probe_blast_radius(ctx)
        assert result.status == "ok"


# ── Evidence Bridge tests ──

class TestProbeEvidenceBridge:

    def test_records_medium_high_findings(self):
        mock_ledger = MagicMock()
        mock_ledger.record.return_value = {"entry_id": "abc123"}
        mock_eventbus = MagicMock()
        mock_eventbus.emit.return_value = True

        bridge = ProbeEvidenceBridge(
            evidence_ledger=mock_ledger,
            eventbus=mock_eventbus,
        )

        results = [
            ProbeResult(
                probe_id="test", status="warn", confidence=0.8,
                findings=[
                    Finding(title="low sev", summary="x", severity="low", confidence=0.5),
                    Finding(title="med sev", summary="y", severity="medium", confidence=0.7),
                    Finding(title="high sev", summary="z", severity="high", confidence=0.9),
                ],
            ),
        ]

        summary = bridge.record_results(results, node_id="mac1")
        assert summary["findings_recorded"] == 2  # medium + high only
        assert summary["events_emitted"] == 2
        assert mock_ledger.record.call_count == 2
        assert mock_eventbus.emit.call_count == 2

    def test_dry_run_without_ledger(self):
        bridge = ProbeEvidenceBridge()  # No ledger, no eventbus
        results = [
            ProbeResult(
                probe_id="test", status="warn", confidence=0.8,
                findings=[
                    Finding(title="high", summary="x", severity="high", confidence=0.9),
                ],
            ),
        ]
        summary = bridge.record_results(results)
        assert summary["findings_recorded"] == 0
        assert summary["events_emitted"] == 0

    def test_stats_tracking(self):
        mock_ledger = MagicMock()
        mock_ledger.record.return_value = {"entry_id": "x"}
        bridge = ProbeEvidenceBridge(evidence_ledger=mock_ledger)

        results = [ProbeResult.ok("clean")]
        bridge.record_results(results)
        assert bridge.stats["results_processed"] == 1
        assert bridge.stats["findings_recorded"] == 0
