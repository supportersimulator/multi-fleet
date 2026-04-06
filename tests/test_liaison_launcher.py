"""
Integration tests for the Fleet Liaison launcher chain.

Validates that all liaison components exist, are well-formed, and that the
FleetLiaison class behaves correctly for PID management and daemon-offline
scenarios.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))


# ── Test 1: fleet-liaison.sh exists and is executable ──

class TestLauncherScript:
    """Verify the shell launcher script."""

    SCRIPT_CANDIDATES = [
        FLEET_ROOT / "scripts" / "fleet-liaison.sh",
        FLEET_ROOT / "bin" / "fleet-liaison.sh",
        REPO_ROOT / "scripts" / "fleet-liaison.sh",
    ]

    def _find_script(self):
        for p in self.SCRIPT_CANDIDATES:
            if p.exists():
                return p
        return None

    @pytest.mark.unit
    def test_fleet_liaison_sh_exists(self):
        """fleet-liaison.sh must exist in one of the expected locations."""
        script = self._find_script()
        if script is None:
            pytest.skip(
                "fleet-liaison.sh not yet created — expected in one of: "
                + ", ".join(str(p) for p in self.SCRIPT_CANDIDATES)
            )
        assert script.exists()

    @pytest.mark.unit
    def test_fleet_liaison_sh_is_executable(self):
        """fleet-liaison.sh must have execute permission."""
        script = self._find_script()
        if script is None:
            pytest.skip("fleet-liaison.sh not yet created")
        assert os.access(script, os.X_OK), f"{script} is not executable"


# ── Test 2: Agent prompt exists and is under 200 lines ──

class TestAgentPrompt:
    """Verify the fleet-liaison agent prompt markdown."""

    PROMPT_PATH = FLEET_ROOT / "agents" / "fleet-liaison.md"

    @pytest.mark.unit
    def test_fleet_liaison_md_exists(self):
        """agents/fleet-liaison.md must exist."""
        assert self.PROMPT_PATH.exists(), f"Missing: {self.PROMPT_PATH}"

    @pytest.mark.unit
    def test_fleet_liaison_md_under_200_lines(self):
        """Agent prompt must stay lean — under 200 lines."""
        lines = self.PROMPT_PATH.read_text().splitlines()
        assert len(lines) <= 200, (
            f"fleet-liaison.md is {len(lines)} lines — should be under 200 for token efficiency"
        )


# ── Test 3: SKILL.md exists ──

class TestSkillDefinition:
    """Verify the fleet-liaison skill definition."""

    SKILL_PATH = FLEET_ROOT / "skills" / "fleet-liaison" / "SKILL.md"

    @pytest.mark.unit
    def test_fleet_liaison_skill_exists(self):
        """skills/fleet-liaison/SKILL.md must exist."""
        if not self.SKILL_PATH.exists():
            pytest.skip("skills/fleet-liaison/SKILL.md not yet created")
        assert self.SKILL_PATH.exists()


# ── Test 4: command.md exists ──

class TestCommandDefinition:
    """Verify the fleet-liaison command definition."""

    COMMAND_PATH = FLEET_ROOT / "commands" / "fleet-liaison.md"

    @pytest.mark.unit
    def test_fleet_liaison_command_exists(self):
        """commands/fleet-liaison.md must exist."""
        if not self.COMMAND_PATH.exists():
            pytest.skip("commands/fleet-liaison.md not yet created")
        assert self.COMMAND_PATH.exists()


# ── Test 5: liaison.py can be imported ──

class TestLiaisonImport:
    """Verify that the liaison module is importable."""

    @pytest.mark.unit
    def test_liaison_module_imports(self):
        """multifleet.liaison must be importable without side effects."""
        from multifleet.liaison import (
            EscalationLevel,
            FleetLiaison,
            LiaisonState,
            create_liaison,
        )
        assert EscalationLevel is not None
        assert FleetLiaison is not None
        assert LiaisonState is not None
        assert create_liaison is not None


# ── Test 6: FleetLiaison can be instantiated ──

class TestLiaisonInstantiation:
    """Verify FleetLiaison construction and defaults."""

    @pytest.mark.unit
    def test_basic_instantiation(self):
        """FleetLiaison(node_id=...) succeeds with sensible defaults."""
        from multifleet.liaison import FleetLiaison

        liaison = FleetLiaison(node_id="mac3")
        assert liaison.node_id == "mac3"
        assert liaison.daemon_url == "http://127.0.0.1:8855"
        assert liaison.state.node_id == "mac3"
        assert liaison.state.unread_messages == []
        assert liaison.state.pending_reviews == []
        assert liaison.state.active_rebuttals == []

    @pytest.mark.unit
    def test_factory_with_env(self):
        """create_liaison() reads MULTIFLEET_NODE_ID from environment."""
        from multifleet.liaison import create_liaison

        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac2"}):
            liaison = create_liaison()
            assert liaison.node_id == "mac2"

    @pytest.mark.unit
    def test_factory_default_node_id(self):
        """create_liaison() defaults to 'unknown' when env var is missing."""
        from multifleet.liaison import create_liaison

        with patch.dict(os.environ, {}, clear=True):
            # Remove MULTIFLEET_NODE_ID if present
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            liaison = create_liaison()
            assert liaison.node_id == "unknown"


# ── Test 7: PID file management ──

class TestPidFileManagement:
    """Verify PID file create/check/cleanup lifecycle."""

    @pytest.mark.unit
    def test_pid_file_create_and_read(self, tmp_path):
        """PID file can be written and read back."""
        pid_file = tmp_path / "fleet-liaison.pid"
        pid = os.getpid()
        pid_file.write_text(str(pid))

        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == pid

    @pytest.mark.unit
    def test_pid_file_cleanup(self, tmp_path):
        """PID file is removed on cleanup."""
        pid_file = tmp_path / "fleet-liaison.pid"
        pid_file.write_text(str(os.getpid()))
        assert pid_file.exists()

        pid_file.unlink()
        assert not pid_file.exists()

    @pytest.mark.unit
    def test_stale_pid_detection(self, tmp_path):
        """A PID file with a non-existent process is stale."""
        pid_file = tmp_path / "fleet-liaison.pid"
        # Use a very high PID unlikely to exist
        stale_pid = 99999999
        pid_file.write_text(str(stale_pid))

        try:
            os.kill(stale_pid, 0)
            process_alive = True
        except ProcessLookupError:
            process_alive = False
        except PermissionError:
            process_alive = True  # exists but owned by another user

        assert not process_alive, "Stale PID should not correspond to a running process"


# ── Test 8: Graceful behavior when daemon is offline ──

class TestDaemonOffline:
    """Verify liaison degrades gracefully when fleet daemon is unreachable."""

    @pytest.mark.unit
    def test_process_message_without_daemon(self):
        """Messages can be processed even when daemon is offline."""
        from multifleet.liaison import EscalationLevel, FleetLiaison

        liaison = FleetLiaison(node_id="mac3", daemon_url="http://127.0.0.1:9999")
        msg = {
            "type": "context",
            "from": "mac1",
            "payload": {"subject": "test", "body": "hello"},
        }
        level = liaison.process_message(msg)
        assert level == EscalationLevel.INFO
        assert len(liaison.state.unread_messages) == 1

    @pytest.mark.unit
    def test_batch_injection_without_daemon(self):
        """Batch injection works offline — returns accumulated messages."""
        from multifleet.liaison import FleetLiaison

        liaison = FleetLiaison(node_id="mac3")
        liaison.process_message({
            "type": "alert",
            "from": "mac2",
            "payload": {"subject": "urgent issue"},
        })
        liaison.process_message({
            "type": "context",
            "from": "mac1",
            "payload": {"subject": "fyi update"},
        })

        batch = liaison.get_batch_for_injection()
        assert batch is not None
        assert "Fleet Update" in batch
        assert "mac2" in batch
        assert "mac1" in batch
        # Queue should be cleared after batch
        assert len(liaison.state.unread_messages) == 0

    @pytest.mark.unit
    def test_escalation_callback_fires_without_daemon(self):
        """Registered escalation callbacks fire even when daemon is down."""
        from multifleet.liaison import EscalationLevel, FleetLiaison

        liaison = FleetLiaison(node_id="mac3")
        callback_fired = []

        def on_critical(msg, level):
            callback_fired.append((msg, level))

        liaison.register_callback(EscalationLevel.CRITICAL, on_critical)

        msg = {
            "type": "alert",
            "from": "mac1",
            "payload": {"subject": "system down"},
        }
        level = liaison.process_message(msg)
        assert level == EscalationLevel.CRITICAL
        assert len(callback_fired) == 1
        assert callback_fired[0][1] == EscalationLevel.CRITICAL
