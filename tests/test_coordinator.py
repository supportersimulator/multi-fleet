"""Tests for multifleet.coordinator — surgeon status, heartbeat, fleet summary, git branch, error handling."""

import json
import os
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from multifleet import coordinator


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """Isolate coordinator from real filesystem and network."""
    monkeypatch.setattr(coordinator, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(coordinator, "CONFIG_PATH", tmp_path / ".multifleet" / "config.json")
    monkeypatch.setattr(coordinator, "LOCAL_DIR", tmp_path / ".contextdna" / "local")
    monkeypatch.setattr(coordinator, "QUEUE_DIR", tmp_path / ".contextdna" / "packets")
    monkeypatch.setattr(coordinator, "NODE_STATE_PATH", tmp_path / ".contextdna" / "local" / "node_state.json")
    monkeypatch.setenv("MULTIFLEET_NODE_ID", "mac1")


@pytest.fixture
def sample_config():
    return {
        "fleetId": "test-fleet",
        "chief": {"nodeId": "mac1", "host": "192.168.1.1", "ingestUrl": "http://192.168.1.1:8855/ingest"},
        "nodes": {"mac1": {}, "mac2": {}},
    }


# ── surgeon_status ──


class TestSurgeonStatus:
    def test_cardiologist_true_with_openai_key(self, monkeypatch):
        monkeypatch.setenv("Context_DNA_OPENAI", "sk-test")
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("urllib.request.urlopen", side_effect=Exception("no ollama")):
            status = coordinator.surgeon_status()
        assert status["cardiologist"] is True

    def test_cardiologist_true_with_deepseek_key(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.setenv("Context_DNA_Deepseek", "sk-test")
        with patch("urllib.request.urlopen", side_effect=Exception("no ollama")):
            status = coordinator.surgeon_status()
        assert status["cardiologist"] is True

    def test_cardiologist_false_no_keys(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("urllib.request.urlopen", side_effect=Exception("no ollama")):
            status = coordinator.surgeon_status()
        assert status["cardiologist"] is False

    def test_neurologist_true_ollama(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status = coordinator.surgeon_status()
        assert status["neurologist"] is True

    def test_neurologist_true_mlx_fallback(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("ollama down")
            mock_resp = MagicMock()
            mock_resp.status = 200
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=side_effect):
            status = coordinator.surgeon_status()
        assert status["neurologist"] is True

    def test_neurologist_false_both_fail(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("urllib.request.urlopen", side_effect=Exception("all down")):
            status = coordinator.surgeon_status()
        assert status["neurologist"] is False


# ── get_node_id ──


class TestGetNodeId:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MULTIFLEET_NODE_ID", "custom-node")
        assert coordinator.get_node_id() == "custom-node"

    def test_hostname_mapping(self, monkeypatch):
        monkeypatch.delenv("MULTIFLEET_NODE_ID", raising=False)
        with patch("socket.gethostname", return_value="Mac2-Studio.local"):
            assert coordinator.get_node_id() == "mac2"

    def test_unknown_hostname_passthrough(self, monkeypatch):
        monkeypatch.delenv("MULTIFLEET_NODE_ID", raising=False)
        with patch("socket.gethostname", return_value="random-host"):
            assert coordinator.get_node_id() == "random-host"


# ── build_heartbeat ──


class TestBuildHeartbeat:
    def test_heartbeat_structure(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("subprocess.run") as mock_run, \
             patch("urllib.request.urlopen", side_effect=Exception("no")):
            mock_run.return_value = MagicMock(returncode=0, stdout="main\n")
            hb = coordinator.build_heartbeat({"fleetId": "test"})

        assert hb["type"] == "heartbeat"
        assert hb["nodeId"] == "mac1"
        assert hb["fleetId"] == "test"
        assert hb["status"] == "active"
        assert hb["activeBranch"] == "main"
        assert "surgeonStatus" in hb
        assert "timestamp" in hb

    def test_heartbeat_git_failure_defaults_unknown(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("subprocess.run", side_effect=Exception("git not found")), \
             patch("urllib.request.urlopen", side_effect=Exception("no")):
            hb = coordinator.build_heartbeat({})
        assert hb["activeBranch"] == "unknown"

    def test_heartbeat_git_nonzero_defaults_unknown(self, monkeypatch):
        monkeypatch.delenv("Context_DNA_OPENAI", raising=False)
        monkeypatch.delenv("Context_DNA_Deepseek", raising=False)
        with patch("subprocess.run") as mock_run, \
             patch("urllib.request.urlopen", side_effect=Exception("no")):
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            hb = coordinator.build_heartbeat({})
        assert hb["activeBranch"] == "unknown"


# ── send_to_chief ──


class TestSendToChief:
    def test_no_ingest_url_returns_false(self):
        assert coordinator.send_to_chief({}, {}) is False
        assert coordinator.send_to_chief({}, {"chief": {}}) is False

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        config = {"chief": {"ingestUrl": "http://localhost:8855/ingest"}}
        with patch("urllib.request.urlopen", return_value=mock_resp):
            assert coordinator.send_to_chief({"type": "heartbeat"}, config) is True

    def test_network_error_returns_false(self):
        config = {"chief": {"ingestUrl": "http://localhost:8855/ingest"}}
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            assert coordinator.send_to_chief({"type": "heartbeat"}, config) is False


# ── queue_packet ──


class TestQueuePacket:
    def test_creates_queue_file(self, tmp_path):
        coordinator.QUEUE_DIR = tmp_path / "packets"
        path = coordinator.queue_packet({"type": "heartbeat", "nodeId": "mac1"})
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["type"] == "heartbeat"

    def test_filename_contains_type(self, tmp_path):
        coordinator.QUEUE_DIR = tmp_path / "packets"
        path = coordinator.queue_packet({"type": "status", "nodeId": "mac1"})
        assert "status" in path.name


# ── flush_queue ──


class TestFlushQueue:
    def test_flush_empty_queue(self, tmp_path, capsys):
        coordinator.QUEUE_DIR = tmp_path / "packets"
        coordinator.flush_queue({})
        out = capsys.readouterr().out
        assert "No queued" in out

    def test_flush_sends_and_removes(self, tmp_path):
        coordinator.QUEUE_DIR = tmp_path / "packets"
        coordinator.queue_packet({"type": "test", "nodeId": "mac1"})
        config = {"chief": {"ingestUrl": "http://localhost:8855/ingest"}}

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            coordinator.flush_queue(config)
        remaining = list((tmp_path / "packets").glob("*.json"))
        assert len(remaining) == 0


# ── load_config ──


class TestLoadConfig:
    def test_missing_config_returns_empty(self):
        assert coordinator.load_config() == {}

    def test_valid_config(self, tmp_path):
        conf_dir = tmp_path / ".multifleet"
        conf_dir.mkdir()
        conf_file = conf_dir / "config.json"
        conf_file.write_text('{"fleetId":"test"}')
        coordinator.CONFIG_PATH = conf_file
        assert coordinator.load_config()["fleetId"] == "test"


# ── save_node_state ──


class TestSaveNodeState:
    def test_creates_state_file(self, tmp_path):
        coordinator.LOCAL_DIR = tmp_path / "local"
        coordinator.NODE_STATE_PATH = tmp_path / "local" / "node_state.json"
        coordinator.save_node_state({"nodeId": "mac1", "status": "active"})
        data = json.loads(coordinator.NODE_STATE_PATH.read_text())
        assert data["nodeId"] == "mac1"
