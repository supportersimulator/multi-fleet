"""Tests for round 2 probes: stale_context, security_surface, injection_prioritizer.

Covers: stale context detection, security surface scanning, injection prioritization,
and 6-probe registration verification.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.code_scanner import ProbeEngine, create_engine
from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult


# ── Stale Context Probe tests ──


class TestStaleContextProbe:

    def test_detects_stale_claudemd(self, tmp_path):
        """CLAUDE.md hasn't been updated but code has."""
        (tmp_path / "CLAUDE.md").write_text("# Config\n")

        from multifleet.code_scanner.probes.stale_context import probe_stale_context

        with patch("multifleet.code_scanner.probes.stale_context._git_mtime") as mock_mtime, \
             patch("multifleet.code_scanner.probes.stale_context._recent_code_mtime") as mock_code:
            # CLAUDE.md is 14 days older than latest code
            mock_mtime.return_value = 1000000.0
            mock_code.return_value = 1000000.0 + 14 * 86400

            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_stale_context(ctx)
            assert result.status == "warn"
            assert any("stale" in f.title.lower() for f in result.findings)

    def test_ok_when_claudemd_fresh(self, tmp_path):
        """CLAUDE.md updated more recently than code."""
        (tmp_path / "CLAUDE.md").write_text("# Fresh\n")

        from multifleet.code_scanner.probes.stale_context import probe_stale_context

        with patch("multifleet.code_scanner.probes.stale_context._git_mtime") as mock_mtime, \
             patch("multifleet.code_scanner.probes.stale_context._recent_code_mtime") as mock_code:
            mock_mtime.return_value = 2000000.0
            mock_code.return_value = 1999000.0  # Code older

            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_stale_context(ctx)
            # Should not flag stale CLAUDE.md
            stale_findings = [f for f in result.findings if "stale" in f.title.lower()]
            assert len(stale_findings) == 0

    def test_detects_outdated_readme(self, tmp_path):
        """README references files that don't exist."""
        (tmp_path / "README.md").write_text(
            "See `src/old_module/handler.py` for details.\n"
            "Also check `scripts/deploy.sh`.\n"
        )
        # Create one of them so it's not all missing
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "deploy.sh").touch()

        from multifleet.code_scanner.probes.stale_context import probe_stale_context

        with patch("multifleet.code_scanner.probes.stale_context._git_mtime", return_value=0), \
             patch("multifleet.code_scanner.probes.stale_context._recent_code_mtime", return_value=0):
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_stale_context(ctx)
            dead_ref_findings = [f for f in result.findings if "dead reference" in f.title.lower()]
            assert len(dead_ref_findings) > 0
            assert any("old_module" in str(f.evidence_refs) for f in dead_ref_findings)

    def test_detects_unused_env_keys(self, tmp_path):
        """.env.example has keys not used in any code."""
        (tmp_path / ".env.example").write_text("DATABASE_URL=\nOBSOLETE_KEY=\n")
        (tmp_path / "app.py").write_text(
            'import os\ndb = os.environ.get("DATABASE_URL")\n'
        )

        from multifleet.code_scanner.probes.stale_context import probe_stale_context

        with patch("multifleet.code_scanner.probes.stale_context._git_mtime", return_value=0), \
             patch("multifleet.code_scanner.probes.stale_context._recent_code_mtime", return_value=0):
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_stale_context(ctx)
            env_findings = [f for f in result.findings if "env.example" in f.title.lower()]
            assert len(env_findings) > 0
            assert any("OBSOLETE_KEY" in str(f.evidence_refs) for f in env_findings)

    def test_detects_package_json_dead_scripts(self, tmp_path):
        """package.json scripts reference missing files."""
        pkg = {
            "scripts": {
                "build": "node build.js",
                "test": "node test_runner.js",
            }
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        # Neither file exists

        from multifleet.code_scanner.probes.stale_context import probe_stale_context

        with patch("multifleet.code_scanner.probes.stale_context._git_mtime", return_value=0), \
             patch("multifleet.code_scanner.probes.stale_context._recent_code_mtime", return_value=0):
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_stale_context(ctx)
            pkg_findings = [f for f in result.findings if "package.json" in f.title.lower()]
            assert len(pkg_findings) > 0


# ── Security Surface Probe tests ──


class TestSecuritySurfaceProbe:

    def test_detects_hardcoded_key(self, tmp_path):
        """Detects OpenAI API key pattern in source code."""
        (tmp_path / "config.py").write_text(
            'OPENAI_KEY = "sk-abc123456789012345678901234567890123456789"\n'
        )

        from multifleet.code_scanner.probes.security_surface import probe_security_surface
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_security_surface(ctx)
        assert result.status == "warn"
        secret_findings = [f for f in result.findings if "secret" in f.title.lower()]
        assert len(secret_findings) > 0
        assert any(f.severity == "high" for f in secret_findings)

    def test_detects_aws_key(self, tmp_path):
        """Detects AWS access key pattern."""
        (tmp_path / "deploy.py").write_text(
            'aws_key = "AKIAIOSFODNN7EXAMPLE"\n'
        )

        from multifleet.code_scanner.probes.security_surface import probe_security_surface
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_security_surface(ctx)
        secret_findings = [f for f in result.findings if "secret" in f.title.lower()]
        assert len(secret_findings) > 0

    def test_detects_shell_injection(self, tmp_path):
        """Detects os.system() and subprocess with shell=True."""
        (tmp_path / "runner.py").write_text(
            'import os\nimport subprocess\n'
            'os.system("rm -rf " + user_input)\n'
            'subprocess.call(cmd, shell=True)\n'
        )

        from multifleet.code_scanner.probes.security_surface import probe_security_surface
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_security_surface(ctx)
        shell_findings = [f for f in result.findings if "shell injection" in f.title.lower()]
        assert len(shell_findings) > 0

    def test_detects_env_in_git(self, tmp_path):
        """Detects .env files tracked in git."""
        from multifleet.code_scanner.probes.security_surface import probe_security_surface

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="src/app.py\n.env\n.env.production\nREADME.md\n",
            )
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_security_surface(ctx)
            env_findings = [f for f in result.findings if ".env" in f.title.lower()]
            assert len(env_findings) > 0
            assert any(f.severity == "high" for f in env_findings)

    def test_detects_private_key(self, tmp_path):
        """Detects PEM private key in repo."""
        (tmp_path / "server.key").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n"
        )

        from multifleet.code_scanner.probes.security_surface import probe_security_surface
        ctx = ProbeContext(repo_path=str(tmp_path))
        result = probe_security_surface(ctx)
        key_findings = [f for f in result.findings if "private key" in f.title.lower()]
        assert len(key_findings) > 0

    def test_ok_when_clean(self, tmp_path):
        """No security issues in clean code."""
        (tmp_path / "clean.py").write_text(
            'import os\nname = os.environ.get("APP_NAME", "default")\n'
        )

        from multifleet.code_scanner.probes.security_surface import probe_security_surface

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="clean.py\n")
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_security_surface(ctx)
            assert result.status == "ok"


# ── Injection Prioritizer Probe tests ──


class TestInjectionPrioritizerProbe:

    def test_ranks_recent_files(self, tmp_path):
        """Recently modified files should appear in ranked output."""
        (tmp_path / "active.py").write_text("x = 1\n")

        from multifleet.code_scanner.probes.injection_prioritizer import probe_injection_prioritizer

        with patch("multifleet.code_scanner.probes.injection_prioritizer._recently_modified") as mock_recent:
            mock_recent.return_value = [("active.py", 5), ("other.py", 2)]

            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_injection_prioritizer(ctx)
            assert len(result.findings) > 0
            # First finding should be the ranked list
            ranked = result.findings[0]
            assert "priority" in ranked.title.lower() or "ranked" in ranked.title.lower()
            assert "active.py" in ranked.summary or "active.py" in str(ranked.evidence_refs)

    def test_boosts_central_modules(self, tmp_path):
        """Highly imported modules get higher scores."""
        (tmp_path / "core.py").write_text("def core_fn(): pass\n")
        (tmp_path / "a.py").write_text("from core import core_fn\n")
        (tmp_path / "b.py").write_text("from core import core_fn\n")
        (tmp_path / "c.py").write_text("from core import core_fn\n")

        from multifleet.code_scanner.probes.injection_prioritizer import _most_imported
        central = _most_imported(tmp_path, list(tmp_path.glob("*.py")))
        # core should appear as imported
        modules = [m for m, _ in central]
        assert any("core" in m for m in modules)

    def test_detects_failing_tests(self, tmp_path):
        """Picks up failing tests from pytest cache."""
        cache_dir = tmp_path / ".pytest_cache" / "v" / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "lastfailed").write_text(
            json.dumps({"tests/test_foo.py::test_bar": True})
        )

        from multifleet.code_scanner.probes.injection_prioritizer import _failing_tests
        failing = _failing_tests(tmp_path)
        assert len(failing) > 0
        assert "test_foo" in failing[0] or "test_bar" in failing[0]

    def test_ok_when_nothing_active(self, tmp_path):
        """Empty repo returns ok with low confidence."""
        from multifleet.code_scanner.probes.injection_prioritizer import probe_injection_prioritizer

        with patch("multifleet.code_scanner.probes.injection_prioritizer._recently_modified", return_value=[]):
            ctx = ProbeContext(repo_path=str(tmp_path))
            result = probe_injection_prioritizer(ctx)
            assert result.status == "ok"


# ── Engine Registration ──


class TestAllProbesRegistered:

    def test_engine_has_6_probes(self):
        """All 6 probes auto-discovered by the engine."""
        engine = create_engine()
        probes = engine.list_probes()
        ids = {p["probe_id"] for p in probes}

        expected = {
            "mirror_drift",
            "hidden_dependency",
            "blast_radius",
            "stale_context",
            "security_surface",
            "injection_prioritizer",
        }
        assert expected.issubset(ids), f"Missing probes: {expected - ids}"
        assert len(ids) >= 6

    def test_probe_categories(self):
        """Verify each probe has correct category."""
        engine = create_engine()
        probes = {p["probe_id"]: p for p in engine.list_probes()}

        assert probes["stale_context"]["category"] == "context_quality"
        assert probes["security_surface"]["category"] == "security"
        assert probes["injection_prioritizer"]["category"] == "context_quality"

    def test_probe_costs(self):
        """All new probes are medium cost."""
        engine = create_engine()
        probes = {p["probe_id"]: p for p in engine.list_probes()}

        assert probes["stale_context"]["cost"] == "medium"
        assert probes["security_surface"]["cost"] == "medium"
        assert probes["injection_prioritizer"]["cost"] == "medium"
