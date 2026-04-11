"""
Tests for session ID normalization and display.

Verifies that:
- Full UUIDs pass through unchanged
- Truncated IDs get resolved from active session lists
- Ambiguous/no-match returns original (graceful degradation)
- display_session_id always truncates for human readability
- _detect_sessions returns full session IDs
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multifleet.sessions import (
    SessionManager,
    normalize_session_id,
    display_session_id,
)


FULL_UUID_1 = "fab27887-e376-46b9-8a0c-38d0aef602ec"
FULL_UUID_2 = "abc12345-d678-4e90-abcd-1234567890ab"
FULL_UUID_3 = "fab27887-ffff-4444-9999-000000000000"  # shares prefix with UUID_1
TRUNCATED_12 = "fab27887-e37"  # first 12 chars of FULL_UUID_1
TRUNCATED_8 = "fab27887"  # first 8 chars — ambiguous (matches UUID_1 and UUID_3)


class TestNormalizeSessionId:
    """normalize_session_id: resolve truncated IDs to full UUIDs."""

    def test_full_uuid_passes_through(self):
        """Full UUID (36 chars) is returned unchanged."""
        result = normalize_session_id(FULL_UUID_1)
        assert result == FULL_UUID_1

    def test_full_uuid_with_sessions_list(self):
        """Full UUID passes through even when active_sessions is provided."""
        sessions = [{"sessionId": FULL_UUID_2}]
        result = normalize_session_id(FULL_UUID_1, active_sessions=sessions)
        assert result == FULL_UUID_1

    def test_truncated_resolves_from_sessions(self):
        """Truncated ID resolves to the matching full UUID from session list."""
        sessions = [
            {"sessionId": FULL_UUID_1},
            {"sessionId": FULL_UUID_2},
        ]
        result = normalize_session_id(TRUNCATED_12, active_sessions=sessions)
        assert result == FULL_UUID_1

    def test_truncated_no_match_returns_original(self):
        """Truncated ID with no match returns the original string."""
        sessions = [{"sessionId": FULL_UUID_2}]
        result = normalize_session_id(TRUNCATED_12, active_sessions=sessions)
        assert result == TRUNCATED_12

    def test_truncated_ambiguous_returns_original(self):
        """Truncated ID matching multiple sessions returns the original (ambiguous)."""
        sessions = [
            {"sessionId": FULL_UUID_1},
            {"sessionId": FULL_UUID_3},  # same 8-char prefix as UUID_1
        ]
        result = normalize_session_id(TRUNCATED_8, active_sessions=sessions)
        assert result == TRUNCATED_8

    def test_empty_string_returns_empty(self):
        """Empty session ID returns empty string."""
        result = normalize_session_id("")
        assert result == ""

    def test_none_returns_none(self):
        """None session ID returns None (no crash)."""
        result = normalize_session_id(None)
        assert result is None

    def test_longer_than_uuid_passes_through(self):
        """String longer than 36 chars passes through."""
        long_id = FULL_UUID_1 + "-extra-suffix"
        result = normalize_session_id(long_id)
        assert result == long_id

    def test_empty_sessions_list(self):
        """Truncated ID with empty sessions list returns original."""
        result = normalize_session_id(TRUNCATED_12, active_sessions=[])
        assert result == TRUNCATED_12


class TestDisplaySessionId:
    """display_session_id: truncate for human-readable display."""

    def test_full_uuid_truncates_to_12(self):
        result = display_session_id(FULL_UUID_1)
        assert result == "fab27887-e37"
        assert len(result) == 12

    def test_short_id_unchanged(self):
        result = display_session_id("abc")
        assert result == "abc"

    def test_empty_returns_question_mark(self):
        result = display_session_id("")
        assert result == "?"


class TestDetectSessionsFullIds:
    """_detect_sessions must return full session IDs, not truncated."""

    def test_pid_sessions_return_full_id(self, tmp_path):
        """PID file sessions should have full sessionId."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create a fake PID file with the current process PID (so os.kill works)
        pid = os.getpid()
        pid_file = sessions_dir / f"{pid}.json"
        pid_file.write_text(json.dumps({
            "sessionId": FULL_UUID_1,
            "kind": "interactive",
            "entrypoint": "vscode",
        }))

        nerve = MagicMock()
        mgr = SessionManager(nerve)

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        assert len(sessions) >= 1
        found = [s for s in sessions if s["sessionId"].startswith("fab27887")]
        assert len(found) == 1
        assert found[0]["sessionId"] == FULL_UUID_1
        assert len(found[0]["sessionId"]) == 36

    def test_jsonl_sessions_return_full_id(self, tmp_path):
        """JSONL file sessions should have full sessionId (from filename stem)."""
        projects_dir = tmp_path / ".claude" / "projects"
        proj_dir = projects_dir / "test-project"
        proj_dir.mkdir(parents=True)

        # Create a fake JSONL file with full UUID as filename
        jsonl_file = proj_dir / f"{FULL_UUID_2}.jsonl"
        jsonl_file.write_text('{"type":"test"}\n')
        # Touch to ensure recent mtime
        os.utime(jsonl_file, (time.time(), time.time()))

        nerve = MagicMock()
        mgr = SessionManager(nerve)

        with patch.object(Path, "home", return_value=tmp_path):
            sessions = mgr._detect_sessions()

        found = [s for s in sessions if s["sessionId"].startswith("abc12345")]
        assert len(found) == 1
        assert found[0]["sessionId"] == FULL_UUID_2
        assert len(found[0]["sessionId"]) == 36
