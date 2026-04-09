"""Tests for demo-multifleet.sh script."""

import os
import stat
import subprocess

SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "demo-multifleet.sh"
)


def test_demo_script_exists():
    """Demo script file exists at expected path."""
    assert os.path.isfile(SCRIPT_PATH), f"Script not found at {SCRIPT_PATH}"


def test_demo_script_is_executable():
    """Demo script has executable permission."""
    st = os.stat(SCRIPT_PATH)
    assert st.st_mode & stat.S_IXUSR, "Script is not executable by owner"


def test_demo_script_has_shebang():
    """Demo script starts with proper bash shebang."""
    with open(SCRIPT_PATH) as f:
        first_line = f.readline().strip()
    assert first_line == "#!/usr/bin/env bash", f"Bad shebang: {first_line}"


def test_demo_script_has_all_sections():
    """Demo script contains all 6 demo sections."""
    with open(SCRIPT_PATH) as f:
        content = f.read()
    required_sections = [
        "Fleet Health",
        "Probe Scan",
        "Rebuttal",
        "Human Arbiter",
        "Evidence Ledger",
        "Fleet Dashboard",
    ]
    for section in required_sections:
        assert section in content, f"Missing section: {section}"


def test_demo_script_runs_without_error():
    """Demo script executes successfully (exit code 0)."""
    result = subprocess.run(
        ["bash", SCRIPT_PATH],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "TERM": "dumb"},
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"
    # Verify it produced output
    assert len(result.stdout) > 100, "Script produced too little output"
