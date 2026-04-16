"""
Tests for sessions.py — session tracking, state management, cleanup logic.

Covers:
- SessionManager formatting and resume injection
- Seed file resolution and injection
- Context injection routing (urgent vs passive)
- Keystroke throttling and rate limiting
- Task handling and worker dispatch
- VS Code / session detection
- Notification delivery
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from multifleet.sessions import (
    SessionManager,
    normalize_session_id,
    display_session_id,
)


FULL_UUID_1 = "fab27887-e376-46b9-8a0c-38d0aef602ec"
FULL_UUID_2 = "abc12345-d678-4e90-abcd-1234567890ab"


@pytest.fixture
def nerve():
    """Mock FleetNerveNATS for SessionManager."""
    n = MagicMock()
    n.node_id = "mac1"
    n._loop = MagicMock()
    return n


@pytest.fixture
def mgr(nerve):
    """SessionManager with mocked nerve."""
    return SessionManager(nerve)


# ── Format resume message ──────────────────────────────────────────────


class TestFormatResumeMessage:
    def test_basic_format(self, mgr):
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Build complete", "body": "All tests pass."},
        }
        result = mgr._format_resume_message(data)
        assert "[FLEET CONTEXT] from mac2: Build complete" in result
        assert "All tests pass." in result

    def test_missing_fields_graceful(self, mgr):
        data = {}
        result = mgr._format_resume_message(data)
        assert "[FLEET CONTEXT] from unknown: Fleet message" in result

    def test_alert_type_uppercase(self, mgr):
        data = {"type": "alert", "from": "mac3", "payload": {"subject": "Disk full"}}
        result = mgr._format_resume_message(data)
        assert "[FLEET ALERT]" in result


# ── Inject via resume ──────────────────────────────────────────────────


class TestInjectViaResume:
    @patch("multifleet.sessions.subprocess")
    def test_success(self, mock_sp, mgr):
        mock_sp.run.return_value = MagicMock(returncode=0)
        result = mgr._inject_via_resume(FULL_UUID_1, "test message")
        assert result is True
        args = mock_sp.run.call_args
        assert "--resume" in args[0][0]
        assert FULL_UUID_1 in args[0][0]

    @patch("multifleet.sessions.subprocess")
    def test_failure_returns_false(self, mock_sp, mgr):
        mock_sp.run.return_value = MagicMock(returncode=1)
        result = mgr._inject_via_resume(FULL_UUID_1, "test message")
        assert result is False

    @patch("multifleet.sessions.subprocess")
    def test_exception_returns_false(self, mock_sp, mgr):
        mock_sp.run.side_effect = OSError("command not found")
        result = mgr._inject_via_resume(FULL_UUID_1, "test message")
        assert result is False


# ── Seed path resolution ───────────────────────────────────────────────


class TestResolveSeedPath:
    def test_explicit_target_session(self, mgr):
        data = {"_target_session": FULL_UUID_1}
        path = mgr._resolve_seed_path(data)
        assert f"fleet-seed-{FULL_UUID_1}" in path

    def test_target_session_sanitized(self, mgr):
        """Special characters stripped from session ID in path."""
        data = {"_target_session": "ab/../../../etc/passwd"}
        path = mgr._resolve_seed_path(data)
        # Only alnum and -_ survive
        assert ".." not in path
        assert "/" not in Path(path).name.replace("fleet-seed-", "").replace(".md", "")

    @patch("urllib.request.urlopen")
    def test_health_endpoint_session(self, mock_url, mgr):
        """Resolves from daemon /health when no explicit target."""
        health_data = json.dumps({
            "sessions": [
                {"sessionId": FULL_UUID_2, "idle_s": 5},
                {"sessionId": FULL_UUID_1, "idle_s": 300},
            ]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = health_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_url.return_value = mock_resp
        data = {}
        path = mgr._resolve_seed_path(data)
        # Should pick the least-idle session (FULL_UUID_2, idle_s=5)
        assert FULL_UUID_2.replace("-", "") in path.replace("-", "")

    @patch("urllib.request.urlopen", side_effect=Exception("no daemon"))
    def test_fallback_to_node_id(self, mock_url, mgr):
        """Falls back to node_id when daemon unreachable."""
        data = {}
        path = mgr._resolve_seed_path(data)
        assert "fleet-seed-mac1" in path


# ── Inject seed (file write) ──────────────────────────────────────────


class TestInjectSeed:
    def test_writes_seed_file(self, mgr, tmp_path):
        """Seed injection creates/appends to seed file."""
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Update", "body": "New branch pushed."},
            "_target_session": "test-session-id",
        }
        # Patch SEED_DIR to use tmp_path
        with patch("multifleet.sessions.SEED_DIR", str(tmp_path)):
            mgr._inject_seed(data)

        seed_files = list(tmp_path.glob("fleet-seed-*"))
        assert len(seed_files) == 1
        content = seed_files[0].read_text()
        assert "[CONTEXT] from mac2: Update" in content
        assert "New branch pushed." in content

    def test_appends_not_overwrites(self, mgr, tmp_path):
        """Multiple injections append to same seed file."""
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Msg1", "body": "First"},
            "_target_session": "fixed-session",
        }
        with patch("multifleet.sessions.SEED_DIR", str(tmp_path)):
            mgr._inject_seed(data)
            data["payload"]["subject"] = "Msg2"
            data["payload"]["body"] = "Second"
            mgr._inject_seed(data)

        seed_files = list(tmp_path.glob("fleet-seed-*"))
        content = seed_files[0].read_text()
        assert "Msg1" in content
        assert "Msg2" in content


# ── Context injection routing ──────────────────────────────────────────


class TestInjectContext:
    def test_non_urgent_skips_keystroke(self, mgr):
        """Non-urgent context messages go to seed only, no keystroke."""
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Info", "body": "FYI"},
        }
        with patch.object(mgr, "_inject_seed") as mock_seed, \
             patch.object(mgr, "_keystroke_trigger") as mock_key:
            mgr._inject_context(data)
            mock_seed.assert_called_once()
            mock_key.assert_not_called()

    def test_alert_triggers_keystroke(self, mgr):
        """Alert type triggers seed + keystroke."""
        data = {
            "type": "alert",
            "from": "mac2",
            "payload": {"subject": "Disk full", "body": "90% used"},
        }
        with patch.object(mgr, "_inject_seed") as mock_seed, \
             patch.object(mgr, "_keystroke_trigger") as mock_key, \
             patch.object(mgr, "_notify"), \
             patch.object(mgr, "_get_active_session_full_id", return_value=None):
            mgr._inject_context(data)
            mock_seed.assert_called_once()
            mock_key.assert_called_once()

    def test_critical_urgency_triggers_keystroke(self, mgr):
        """Payload urgency=critical also treated as urgent."""
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Build", "body": "Failed", "urgency": "critical"},
        }
        with patch.object(mgr, "_inject_seed") as mock_seed, \
             patch.object(mgr, "_keystroke_trigger") as mock_key, \
             patch.object(mgr, "_notify"), \
             patch.object(mgr, "_get_active_session_full_id", return_value=None):
            mgr._inject_context(data)
            mock_seed.assert_called_once()
            mock_key.assert_called_once()

    def test_idle_gated_skips_keystroke(self, mgr):
        """_idle_gated flag suppresses keystroke trigger even for urgent messages."""
        data = {
            "type": "alert",
            "from": "mac2",
            "payload": {"subject": "Alert", "body": "Something"},
            "_idle_gated": True,
        }
        with patch.object(mgr, "_inject_seed"), \
             patch.object(mgr, "_keystroke_trigger") as mock_key, \
             patch.object(mgr, "_notify"), \
             patch.object(mgr, "_get_active_session_full_id", return_value=None):
            mgr._inject_context(data)
            mock_key.assert_not_called()

    def test_resume_preferred_for_context(self, mgr):
        """context type prefers --resume injection when session available."""
        data = {
            "type": "context",
            "from": "mac2",
            "payload": {"subject": "Update", "body": "Done", "urgency": "critical"},
        }
        with patch.object(mgr, "_get_active_session_full_id", return_value=FULL_UUID_1), \
             patch.object(mgr, "_inject_via_resume", return_value=True) as mock_resume, \
             patch.object(mgr, "_inject_seed") as mock_seed, \
             patch.object(mgr, "_notify"):
            mgr._inject_context(data)
            mock_resume.assert_called_once()
            # Seed should NOT be called if resume succeeded
            mock_seed.assert_not_called()


# ── Keystroke throttle ─────────────────────────────────────────────────


class TestKeystrokeThrottle:
    def setup_method(self):
        SessionManager._last_keystroke = 0.0

    @patch("multifleet.sessions.subprocess")
    def test_first_keystroke_fires(self, mock_sp, mgr):
        """First keystroke in 30s window fires."""
        # VS Code check returns true
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="true", stderr="")
        mgr._keystroke_trigger("test")
        assert mock_sp.run.call_count >= 1

    @patch("multifleet.sessions.subprocess")
    def test_throttled_within_30s(self, mock_sp, mgr):
        """Second keystroke within 30s is throttled."""
        SessionManager._last_keystroke = time.time()
        mgr._keystroke_trigger("test")
        mock_sp.run.assert_not_called()

    @patch("multifleet.sessions.subprocess")
    def test_fires_after_30s(self, mock_sp, mgr):
        """Keystroke fires after 30s cooldown."""
        SessionManager._last_keystroke = time.time() - 31
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="true", stderr="")
        mgr._keystroke_trigger("test")
        assert mock_sp.run.call_count >= 1


# ── Task injection ─────────────────────────────────────────────────────


class TestInjectTask:
    def test_worker_unavailable_falls_to_seed(self, mgr):
        """When worker manager fails, falls back to seed file."""
        data = {
            "type": "task",
            "from": "mac2",
            "payload": {"subject": "Run tests", "body": "pytest -v"},
        }
        with patch.object(mgr, "_inject_context") as mock_ctx, \
             patch.dict("sys.modules", {"multifleet.worker": None}):
            # Worker module unavailable => falls to seed fallback
            mgr._inject_task(data)
            mock_ctx.assert_called_once()


# ── Notification ───────────────────────────────────────────────────────


class TestNotify:
    @patch("multifleet.sessions.subprocess")
    def test_notify_calls_osascript(self, mock_sp, mgr):
        mock_sp.run.return_value = MagicMock(returncode=0)
        mgr._notify("Hello fleet")
        assert mock_sp.run.called
        cmd = mock_sp.run.call_args[0][0]
        assert "osascript" in cmd
        assert "Hello fleet" in " ".join(cmd)

    @patch("multifleet.sessions.subprocess")
    def test_notify_swallows_errors(self, mock_sp, mgr):
        """Notification failure doesn't raise."""
        mock_sp.run.side_effect = Exception("osascript not found")
        mgr._notify("Hello")  # should not raise


# ── VS Code state detection ───────────────────────────────────────────


class TestDetectVscodeState:
    @patch("multifleet.sessions.subprocess")
    def test_closed_when_no_process(self, mock_sp, mgr):
        mock_sp.run.return_value = MagicMock(returncode=1)
        assert mgr._detect_vscode_state() == "closed"

    @patch("multifleet.sessions.subprocess")
    def test_active_when_frontmost(self, mock_sp, mgr):
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "pgrep" in cmd:
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="Code")
        mock_sp.run.side_effect = side_effect
        assert mgr._detect_vscode_state() == "active"

    @patch("multifleet.sessions.subprocess")
    def test_open_when_not_frontmost(self, mock_sp, mgr):
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "pgrep" in cmd:
                return MagicMock(returncode=0)
            return MagicMock(returncode=0, stdout="Safari")
        mock_sp.run.side_effect = side_effect
        assert mgr._detect_vscode_state() == "open"

    @patch("multifleet.sessions.subprocess")
    def test_unknown_on_exception(self, mock_sp, mgr):
        mock_sp.run.side_effect = Exception("timeout")
        assert mgr._detect_vscode_state() == "unknown"


# ── Session detection ──────────────────────────────────────────────────


class TestDetectSessions:
    def test_pid_session_detected(self, tmp_path, mgr):
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        pid = os.getpid()
        pid_file = sessions_dir / f"{pid}.json"
        pid_file.write_text(json.dumps({
            "sessionId": FULL_UUID_1,
            "kind": "interactive",
            "entrypoint": "cli",
        }))

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        sids = [s["sessionId"] for s in sessions]
        assert FULL_UUID_1 in sids

    def test_dead_pid_ignored(self, tmp_path, mgr):
        """PID files for dead processes are skipped."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Use a very high PID unlikely to exist
        pid_file = sessions_dir / "9999999.json"
        pid_file.write_text(json.dumps({"sessionId": FULL_UUID_1, "kind": "interactive"}))

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        sids = [s["sessionId"] for s in sessions]
        assert FULL_UUID_1 not in sids

    def test_agent_jsonl_excluded(self, tmp_path, mgr):
        """JSONL files starting with 'agent-' are excluded."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-proj"
        projects_dir.mkdir(parents=True)

        agent_jsonl = projects_dir / f"agent-{FULL_UUID_2}.jsonl"
        agent_jsonl.write_text('{"type":"test"}\n')
        os.utime(agent_jsonl, (time.time(), time.time()))

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        sids = [s["sessionId"] for s in sessions]
        assert not any(s.startswith("agent-") for s in sids)

    def test_old_jsonl_excluded(self, tmp_path, mgr):
        """JSONL files older than 30 min are excluded."""
        projects_dir = tmp_path / ".claude" / "projects" / "test-proj"
        projects_dir.mkdir(parents=True)

        old_jsonl = projects_dir / f"{FULL_UUID_2}.jsonl"
        old_jsonl.write_text('{"type":"test"}\n')
        old_time = time.time() - 3600  # 1 hour ago
        os.utime(old_jsonl, (old_time, old_time))

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        sids = [s["sessionId"] for s in sessions]
        assert FULL_UUID_2 not in sids


# ── Active session full ID ─────────────────────────────────────────────


class TestGetActiveSessionFullId:
    def test_prefers_interactive_over_agent(self, tmp_path, mgr):
        """Agent sessions are skipped in favor of interactive ones."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        pid = os.getpid()
        # Agent session (should be skipped)
        agent_file = sessions_dir / f"{pid}.json"
        agent_file.write_text(json.dumps({
            "sessionId": FULL_UUID_2,
            "kind": "agent",
        }))

        with patch.object(Path, "home", return_value=tmp_path):
            result = mgr._get_active_session_full_id()

        # Agent was the only session, so should return None
        assert result is None

    def test_returns_most_recent(self, tmp_path, mgr):
        """Returns the session with the most recent PID file mtime."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        pid = os.getpid()
        pid_file = sessions_dir / f"{pid}.json"
        pid_file.write_text(json.dumps({
            "sessionId": FULL_UUID_1,
            "kind": "interactive",
        }))

        with patch.object(Path, "home", return_value=tmp_path):
            result = mgr._get_active_session_full_id()

        assert result == FULL_UUID_1
