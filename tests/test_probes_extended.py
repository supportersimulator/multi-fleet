"""Tests for the 4 extended probes: architectural_hotspots, branch_intent,
dependency_drift, agent_readiness.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import ProbeFinding, ProbeResult
from multifleet.probes.architectural_hotspots import (
    _blast_radius,
    _git_churn,
    probe_architectural_hotspots,
)
from multifleet.probes.branch_intent import (
    _classify_intent,
    _current_branch,
    _diff_stat,
    probe_branch_intent,
)
from multifleet.probes.dependency_drift import (
    _is_dep_file,
    _is_test_file,
    _recent_commits_with_files,
    probe_dependency_drift,
)
from multifleet.probes.agent_readiness import (
    _check_python_version,
    probe_agent_readiness,
)


# ── architectural_hotspots ──────────────────────────────────────────


def test_hotspots_returns_probe_result(tmp_path):
    result = probe_architectural_hotspots({"repo_root": str(tmp_path)})
    assert isinstance(result, ProbeResult)
    assert result.probe_id == "architectural_hotspots"
    assert result.category == "structure"


def test_hotspots_no_git_repo(tmp_path):
    """Non-git directory returns ok with no-churn finding."""
    result = probe_architectural_hotspots({"repo_root": str(tmp_path)})
    assert result.status == "ok"
    assert result.confidence == 0.5


@patch("multifleet.probes.architectural_hotspots._git_churn")
@patch("multifleet.probes.architectural_hotspots._blast_radius")
def test_hotspots_with_churn(mock_br, mock_churn, tmp_path):
    from collections import Counter
    mock_churn.return_value = Counter({"a.py": 10, "b.py": 5, "c.py": 2})
    mock_br.return_value = {"a.py": 3, "b.py": 1, "c.py": 1}
    result = probe_architectural_hotspots({"repo_root": str(tmp_path)})
    assert result.status == "warning"
    assert len(result.findings) == 1
    assert "hotspots" in result.findings[0].title.lower()


@patch("multifleet.probes.architectural_hotspots._git_churn")
@patch("multifleet.probes.architectural_hotspots._blast_radius")
def test_hotspots_high_severity(mock_br, mock_churn, tmp_path):
    from collections import Counter
    mock_churn.return_value = Counter({"core.py": 25})
    mock_br.return_value = {"core.py": 5}
    result = probe_architectural_hotspots({"repo_root": str(tmp_path)})
    assert result.severity == "high"


@patch("multifleet.probes.architectural_hotspots._git_churn")
def test_hotspots_empty_churn(mock_churn, tmp_path):
    from collections import Counter
    mock_churn.return_value = Counter()
    result = probe_architectural_hotspots({"repo_root": str(tmp_path)})
    assert result.status == "ok"


def test_git_churn_nonexistent_dir(tmp_path):
    """_git_churn on non-git dir returns empty Counter."""
    churn = _git_churn(tmp_path / "nope")
    assert len(churn) == 0


def test_blast_radius_no_py_files(tmp_path):
    result = _blast_radius(tmp_path, ["readme.md"])
    assert result == {"readme.md": 1}


# ── branch_intent ───────────────────────────────────────────────────


def test_classify_fix():
    assert _classify_intent("fix/login-bug") == "fix"
    assert _classify_intent("bugfix/crash") == "fix"
    assert _classify_intent("hotfix/urgent") == "fix"


def test_classify_feature():
    assert _classify_intent("feature/new-dashboard") == "feature"
    assert _classify_intent("feat/probe-engine") == "feature"


def test_classify_refactor():
    assert _classify_intent("refactor/cleanup-imports") == "refactor"
    assert _classify_intent("chore/deps") == "refactor"


def test_classify_docs():
    assert _classify_intent("docs/update-readme") == "docs"


def test_classify_unknown():
    assert _classify_intent("main") == "unknown"
    assert _classify_intent("random-branch") == "unknown"


def test_branch_intent_main_branch(tmp_path):
    """Main/master branches return ok immediately."""
    result = probe_branch_intent({"repo_root": str(tmp_path), "branch": "main"})
    assert result.status == "ok"
    assert result.severity == "info"


@patch("multifleet.probes.branch_intent._diff_stat")
def test_branch_intent_fix_scope_creep(mock_diff, tmp_path):
    mock_diff.return_value = (15, [f"file{i}.py" for i in range(15)])
    result = probe_branch_intent({"repo_root": str(tmp_path), "branch": "fix/tiny-bug"})
    assert result.status == "warning"
    assert any("scope creep" in f.title.lower() for f in result.findings)


@patch("multifleet.probes.branch_intent._diff_stat")
def test_branch_intent_fix_no_creep(mock_diff, tmp_path):
    mock_diff.return_value = (2, ["a.py", "b.py"])
    result = probe_branch_intent({"repo_root": str(tmp_path), "branch": "fix/small"})
    assert result.status == "ok"


@patch("multifleet.probes.branch_intent._diff_stat")
def test_branch_intent_docs_with_code(mock_diff, tmp_path):
    mock_diff.return_value = (3, ["README.md", "src/main.py", "lib/util.py"])
    result = probe_branch_intent({"repo_root": str(tmp_path), "branch": "docs/update"})
    assert result.status == "warning"
    assert any("non-doc" in f.title.lower() for f in result.findings)


@patch("multifleet.probes.branch_intent._diff_stat")
def test_branch_intent_docs_clean(mock_diff, tmp_path):
    mock_diff.return_value = (2, ["README.md", "docs/guide.md"])
    result = probe_branch_intent({"repo_root": str(tmp_path), "branch": "docs/fix-typo"})
    assert result.status == "ok"


# ── dependency_drift ────────────────────────────────────────────────


def test_is_dep_file():
    assert _is_dep_file("pyproject.toml") is True
    assert _is_dep_file("package.json") is True
    assert _is_dep_file("requirements.txt") is True
    assert _is_dep_file("main.py") is False
    assert _is_dep_file("nested/pyproject.toml") is True


def test_is_test_file():
    assert _is_test_file("tests/test_foo.py") is True
    assert _is_test_file("test_bar.py") is True
    assert _is_test_file("src/main.py") is False
    assert _is_test_file("spec/widget.spec.js") is True


def test_dep_drift_no_git(tmp_path):
    result = probe_dependency_drift({"repo_root": str(tmp_path)})
    assert isinstance(result, ProbeResult)
    assert result.probe_id == "dependency_drift"
    assert result.status == "ok"


@patch("multifleet.probes.dependency_drift._recent_commits_with_files")
def test_dep_drift_untested_changes(mock_commits, tmp_path):
    mock_commits.return_value = [
        ("a" * 40, ["pyproject.toml", "src/main.py"]),
        ("b" * 40, ["package.json"]),
    ]
    result = probe_dependency_drift({"repo_root": str(tmp_path)})
    assert result.status == "warning"
    assert len(result.findings) == 1
    assert "without tests" in result.findings[0].title


@patch("multifleet.probes.dependency_drift._recent_commits_with_files")
def test_dep_drift_tested_changes(mock_commits, tmp_path):
    mock_commits.return_value = [
        ("a" * 40, ["pyproject.toml", "tests/test_deps.py"]),
    ]
    result = probe_dependency_drift({"repo_root": str(tmp_path)})
    assert result.status == "ok"


@patch("multifleet.probes.dependency_drift._recent_commits_with_files")
def test_dep_drift_no_dep_changes(mock_commits, tmp_path):
    mock_commits.return_value = [
        ("a" * 40, ["src/main.py", "src/util.py"]),
    ]
    result = probe_dependency_drift({"repo_root": str(tmp_path)})
    assert result.status == "ok"


@patch("multifleet.probes.dependency_drift._recent_commits_with_files")
def test_dep_drift_severity_escalation(mock_commits, tmp_path):
    # 5 untested dep commits -> medium severity
    mock_commits.return_value = [
        (f"{chr(97+i)}" * 40, ["pyproject.toml"]) for i in range(5)
    ]
    result = probe_dependency_drift({"repo_root": str(tmp_path)})
    assert result.severity == "medium"


# ── agent_readiness ─────────────────────────────────────────────────


def test_python_version_check():
    ok, info = _check_python_version()
    assert ok is True  # We're running on 3.10+
    assert "Python" in info


def test_readiness_returns_probe_result(tmp_path):
    result = probe_agent_readiness({"repo_root": str(tmp_path)})
    assert isinstance(result, ProbeResult)
    assert result.probe_id == "agent_readiness"
    assert result.category == "agent_readiness"


@patch("multifleet.probes.agent_readiness._check_daemon")
@patch("multifleet.probes.agent_readiness._check_keychain")
@patch("multifleet.probes.agent_readiness._check_superset_db")
@patch("multifleet.probes.agent_readiness._check_imports")
@patch("multifleet.probes.agent_readiness._check_optional_imports")
def test_readiness_all_ok(mock_opt, mock_imp, mock_ss, mock_kc, mock_daemon):
    mock_daemon.return_value = (True, "daemon running")
    mock_kc.return_value = (["ANTHROPIC_API_KEY", "OPENAI_API_KEY"], [])
    mock_ss.return_value = (True, "superset db exists")
    mock_imp.return_value = (["nats", "jsonschema"], [])
    mock_opt.return_value = (["mcp"], [])
    result = probe_agent_readiness({})
    assert result.status == "ok"
    assert result.severity == "info"


@patch("multifleet.probes.agent_readiness._check_daemon")
@patch("multifleet.probes.agent_readiness._check_keychain")
@patch("multifleet.probes.agent_readiness._check_superset_db")
@patch("multifleet.probes.agent_readiness._check_imports")
@patch("multifleet.probes.agent_readiness._check_optional_imports")
def test_readiness_daemon_down(mock_opt, mock_imp, mock_ss, mock_kc, mock_daemon):
    mock_daemon.return_value = (False, "daemon unreachable: ConnectionError")
    mock_kc.return_value = (["ANTHROPIC_API_KEY", "OPENAI_API_KEY"], [])
    mock_ss.return_value = (True, "ok")
    mock_imp.return_value = (["nats", "jsonschema"], [])
    mock_opt.return_value = ([], ["mcp"])
    result = probe_agent_readiness({})
    assert result.status == "warning"
    assert any("blocker" in f.title.lower() for f in result.findings)


@patch("multifleet.probes.agent_readiness._check_daemon")
@patch("multifleet.probes.agent_readiness._check_keychain")
@patch("multifleet.probes.agent_readiness._check_superset_db")
@patch("multifleet.probes.agent_readiness._check_imports")
@patch("multifleet.probes.agent_readiness._check_optional_imports")
def test_readiness_missing_keys(mock_opt, mock_imp, mock_ss, mock_kc, mock_daemon):
    mock_daemon.return_value = (True, "ok")
    mock_kc.return_value = ([], ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"])
    mock_ss.return_value = (True, "ok")
    mock_imp.return_value = (["nats", "jsonschema"], [])
    mock_opt.return_value = ([], [])
    result = probe_agent_readiness({})
    assert result.status == "warning"


@patch("multifleet.probes.agent_readiness._check_daemon")
@patch("multifleet.probes.agent_readiness._check_keychain")
@patch("multifleet.probes.agent_readiness._check_superset_db")
@patch("multifleet.probes.agent_readiness._check_imports")
@patch("multifleet.probes.agent_readiness._check_optional_imports")
def test_readiness_many_blockers_high_severity(mock_opt, mock_imp, mock_ss, mock_kc, mock_daemon):
    mock_daemon.return_value = (False, "down")
    mock_kc.return_value = ([], ["ANTHROPIC_API_KEY"])
    mock_ss.return_value = (False, "not found")
    mock_imp.return_value = ([], ["nats"])
    mock_opt.return_value = ([], [])
    result = probe_agent_readiness({})
    assert result.severity == "high"


# ── Registration ────────────────────────────────────────────────────


def test_all_probes_registered():
    from multifleet.probes import create_engine
    engine = create_engine()
    probes = engine.registry.list_probes()
    ids = {p["probe_id"] for p in probes}
    assert "architectural_hotspots" in ids
    assert "branch_intent" in ids
    assert "dependency_drift" in ids
    assert "agent_readiness" in ids


def test_probe_categories():
    from multifleet.probes import create_engine
    engine = create_engine()
    probes = {p["probe_id"]: p for p in engine.registry.list_probes()}
    assert probes["architectural_hotspots"]["category"] == "structure"
    assert probes["branch_intent"]["category"] == "change"
    assert probes["dependency_drift"]["category"] == "change"
    assert probes["agent_readiness"]["category"] == "agent_readiness"
