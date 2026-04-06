"""Tests for fleet CLI."""

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

CLI_DIR = Path(__file__).resolve().parent.parent


def _run_cli(*args, timeout=15):
    """Run the CLI as a subprocess and return (returncode, stdout, stderr)."""
    cmd = [sys.executable, "-m", "multifleet.cli", *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(CLI_DIR), timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


class TestHelpOutput:
    def test_no_args_shows_help(self):
        rc, out, _err = _run_cli()
        assert rc == 0
        assert "Multi-Fleet CLI" in out or "fleet" in out

    def test_help_flag(self):
        rc, out, _err = _run_cli("--help")
        assert rc == 0
        assert "status" in out
        assert "race" in out
        assert "doctor" in out
        assert "send" in out

    def test_subcommand_help(self):
        rc, out, _err = _run_cli("send", "--help")
        assert rc == 0
        assert "target" in out.lower()


class TestStatusCommand:
    def test_status_daemon_down(self):
        """When daemon is unreachable, status should error gracefully."""
        from multifleet import cli

        with mock.patch.object(cli, "_get", return_value={"_error": "Daemon unreachable: Connection refused"}):
            with pytest.raises(SystemExit):
                cli.cmd_status(mock.MagicMock())

    def test_status_ok(self, capsys):
        """With a healthy daemon response, status should print node info."""
        from multifleet import cli

        fake_health = {
            "nodeId": "mac2",
            "uptime_s": 123,
            "nats_connected": True,
            "peers": {"mac1": {"last_seen": 0}},
        }
        with mock.patch.object(cli, "_get", return_value=fake_health):
            cli.cmd_status(mock.MagicMock())
        out = capsys.readouterr().out
        assert "mac2" in out
        assert "123" in out


class TestUnknownCommand:
    def test_unknown_subcommand(self):
        """Unknown subcommand should show help or error."""
        rc, out, err = _run_cli("nonexistent")
        assert rc != 0 or "usage" in out.lower() or "error" in err.lower()


class TestEvidenceCommand:
    def test_evidence_requires_action(self):
        """Evidence without action should error."""
        rc, _out, err = _run_cli("evidence")
        assert rc != 0
        assert "required" in err.lower() or "arguments" in err.lower()


class TestSendCommand:
    def test_send_delivered(self, capsys):
        from multifleet import cli

        with mock.patch.object(cli, "_post", return_value={"delivered": True, "method": "nats"}):
            args = mock.MagicMock()
            args.target = "mac1"
            args.message = "hello"
            cli.cmd_send(args)
        out = capsys.readouterr().out
        assert "mac1" in out
