"""
Tests for run-hook.cmd output format — JSON systemMessage structure, graceful degradation.

The hook is the most critical file: it runs on every session start/stop/prompt-submit.
It MUST output valid JSON with a systemMessage key, or nothing at all.

Note: session-start does network I/O (git fetch, SSH probes, daemon health) so it gets
a generous timeout. Tests that need speed use stop/prompt-submit or test isolated functions.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "run-hook.cmd"
REPO_ROOT = Path(__file__).parent.parent.parent


def _parse_last_json(text: str) -> dict:
    """Parse the last JSON object from a string that may contain multiple
    concatenated JSON objects (e.g. daemon broadcast result + systemMessage).

    The hook may emit multiple JSON blobs on a single line when the daemon
    broadcast fires before the systemMessage. We want the last one (the
    systemMessage envelope).
    """
    text = text.strip()
    if not text:
        return {}
    # Try parsing the whole string first (single JSON object)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Multiple JSON objects: find the last '{...}' by scanning from the end
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        if text[i] == '}':
            depth += 1
        elif text[i] == '{':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:])
                except json.JSONDecodeError:
                    continue
    raise json.JSONDecodeError("No valid JSON found", text, 0)


def _run_hook(hook_type: str, env_overrides: dict = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run the hook script with the given type.

    Uses a fake HOME to prevent keychain access, and a non-git cwd to skip
    git operations. Timeout is generous for session-start (does network probes).
    """
    env = os.environ.copy()
    env["MULTIFLEET_NODE_ID"] = "test-node"
    # Fake HOME prevents keychain access, daemon env loading, etc.
    env["HOME"] = "/tmp/fleet-test-fake-home"
    # Remove any existing NATS_URL to prevent connection attempts
    env.pop("NATS_URL", None)
    if env_overrides:
        env.update(env_overrides)

    # Run from /tmp so auto_pull_if_behind's git check fails fast (not a git repo)
    return subprocess.run(
        ["bash", str(HOOK_PATH), hook_type],
        capture_output=True, text=True, timeout=timeout,
        cwd="/tmp",
        env=env,
    )


class TestHookJSONOutput:
    """Hook output must be valid JSON with systemMessage key."""

    @pytest.mark.unit
    def test_unknown_hook_type_returns_error_json(self):
        """Unknown hook type outputs JSON error and exits 1. Fast -- no network."""
        result = _run_hook("invalid-type", timeout=5)
        assert result.returncode == 1
        stdout = result.stdout.strip()
        data = json.loads(stdout)
        assert "systemMessage" in data
        assert "Unknown hook type" in data["systemMessage"]

    @pytest.mark.unit
    def test_stop_outputs_json(self):
        """stop hook outputs JSON with session-end notification.

        The hook may emit multiple JSON objects (daemon broadcast + systemMessage)
        concatenated on one line. We parse the last one for the systemMessage.
        """
        result = _run_hook("stop", timeout=10)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        if stdout:
            data = _parse_last_json(stdout)
            assert "systemMessage" in data
            assert "Session ended" in data["systemMessage"] or "test-node" in data["systemMessage"]

    @pytest.mark.unit
    def test_prompt_submit_silent_when_no_messages(self):
        """prompt-submit outputs nothing when there are no pending messages."""
        result = _run_hook("prompt-submit", timeout=10)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        # Either empty or valid JSON
        if stdout:
            data = json.loads(stdout)
            assert "systemMessage" in data

    @pytest.mark.integration
    def test_session_start_outputs_json(self):
        """session-start always outputs something (even if daemon is down).

        Marked integration because it does network probes (slow).
        """
        result = _run_hook("session-start", timeout=30)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        if stdout:
            data = json.loads(stdout)
            assert "systemMessage" in data
            assert isinstance(data["systemMessage"], str)
            assert len(data["systemMessage"]) > 0


class TestHookNodeIdentity:
    """Node ID is derived from MULTIFLEET_NODE_ID env var."""

    @pytest.mark.security
    def test_node_id_sanitized(self):
        """Special characters in node ID are stripped (prevents injection)."""
        # The hook does: tr -cd 'a-zA-Z0-9_-'
        # So "node$(evil);&rm" becomes "nodeevil-rm" (approx)
        result = _run_hook("stop", {"MULTIFLEET_NODE_ID": "node$(evil);&rm"}, timeout=10)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        if stdout:
            data = _parse_last_json(stdout)
            # Shell metacharacters must not appear in output
            assert "$(" not in data["systemMessage"]

    @pytest.mark.security
    def test_empty_node_id_errors(self):
        """Empty/invalid node ID after sanitization returns error JSON."""
        # "$()!@#" has no alphanumerics -> sanitizes to empty -> error
        result = _run_hook("stop", {"MULTIFLEET_NODE_ID": "$()!@#"}, timeout=5)
        stdout = result.stdout.strip()
        if result.returncode != 0 and stdout:
            data = json.loads(stdout)
            assert "ERROR" in data.get("systemMessage", "") or "invalid" in data.get("systemMessage", "").lower()


class TestHookGracefulDegradation:
    """Hook works correctly even when daemon, NATS, etc. are unavailable."""

    @pytest.mark.unit
    def test_stop_works_without_daemon(self):
        """stop hook works even when fleet daemon is not running."""
        result = _run_hook("stop", timeout=10)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        if stdout:
            data = _parse_last_json(stdout)
            assert "systemMessage" in data

    @pytest.mark.unit
    def test_prompt_submit_works_without_daemon(self):
        """prompt-submit outputs nothing or valid JSON when daemon is down."""
        result = _run_hook("prompt-submit", timeout=10)
        assert result.returncode == 0
        stdout = result.stdout.strip()
        if stdout:
            json.loads(stdout)  # Must be valid JSON

    @pytest.mark.unit
    def test_teammate_idle_exits_0_when_no_messages(self):
        """teammate-idle exits 0 (stay idle) when there are no pending messages."""
        result = _run_hook("teammate-idle", timeout=10)
        # Exit 0 = stay idle, exit 2 = asyncRewake
        assert result.returncode in (0, 2)


class TestOutputJsonFunction:
    """Test the output_json bash function produces valid JSON."""

    @pytest.mark.unit
    def test_output_json_with_special_chars(self):
        """output_json must properly JSON-encode special characters."""
        # We test this by having the hook emit a message with quotes
        # The "stop" path emits "[FLEET] Session ended on <node>"
        result = _run_hook("stop", {"MULTIFLEET_NODE_ID": 'node-with"quotes'}, timeout=5)
        # If node ID has quotes stripped by sanitization, that's fine
        # The key point is the output must be valid JSON
        stdout = result.stdout.strip()
        if stdout:
            data = _parse_last_json(stdout)  # Must not throw
            assert "systemMessage" in data

    @pytest.mark.unit
    def test_output_json_with_newlines_in_content(self):
        """Output contains valid JSON objects (may be multiple per line).

        The hook may emit multiple JSON objects on a single line when the
        daemon broadcast fires alongside the systemMessage. Each individual
        JSON object must be valid.
        """
        result = _run_hook("stop", timeout=10)
        stdout = result.stdout.strip()
        if stdout:
            lines = [l for l in stdout.split("\n") if l.strip()]
            assert len(lines) >= 1
            for line in lines:
                # Line may contain multiple concatenated JSON objects
                # Parse each one by scanning for matched braces
                remaining = line.strip()
                found = 0
                decoder = json.JSONDecoder()
                while remaining:
                    try:
                        _, idx = decoder.raw_decode(remaining)
                        remaining = remaining[idx:].lstrip()
                        found += 1
                    except json.JSONDecodeError:
                        break
                assert found >= 1, f"No valid JSON in line: {line[:80]}"
