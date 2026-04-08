"""Tests for NATS cluster configuration and management."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.nats_cluster import (
    generate_config,
    client_urls,
    server_status,
    _detect_node_id,
    _load_config,
    _minimal_config,
    CLIENT_PORT,
    CLUSTER_PORT,
    MONITOR_PORT,
)


@pytest.fixture
def fleet_config(tmp_path):
    """Create a temporary fleet config."""
    config = {
        "fleetId": "test-fleet",
        "chief": {"nodeId": "mac1", "host": "10.0.0.1"},
        "nodes": {
            "mac1": {"host": "10.0.0.1", "role": "chief", "user": "test"},
            "mac2": {"host": "10.0.0.2", "role": "worker", "user": "test"},
            "mac3": {"host": "10.0.0.3", "role": "worker", "user": "test"},
        },
        "protocol": {"version": "multifleet-v2"},
    }
    conf_path = tmp_path / ".multifleet" / "config.json"
    conf_path.parent.mkdir(parents=True)
    conf_path.write_text(json.dumps(config))
    return config, conf_path


class TestGenerateConfig:

    def test_generates_valid_config(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac1")

        assert "server_name: mac1" in content
        assert "contextdna-fleet" in content
        assert f"listen: 0.0.0.0:{CLIENT_PORT}" in content
        assert f"listen: 0.0.0.0:{CLUSTER_PORT}" in content

    def test_cluster_routes_exclude_self(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac1")

        # mac1's config should route to mac2 and mac3, NOT to itself
        assert "10.0.0.2" in content
        assert "10.0.0.3" in content
        # Should not have self-route
        lines = [l.strip() for l in content.split("\n") if "nats-route://" in l]
        for line in lines:
            assert "10.0.0.1" not in line

    def test_worker_routes_to_all_peers(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac2")

        assert "server_name: mac2" in content
        assert "10.0.0.1" in content  # routes to chief
        assert "10.0.0.3" in content  # routes to mac3
        # No self-route
        lines = [l.strip() for l in content.split("\n") if "nats-route://" in l]
        for line in lines:
            assert "10.0.0.2" not in line

    def test_jetstream_enabled(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac1")

        assert "jetstream" in content
        assert "store_dir" in content

    def test_monitoring_port(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac1")

        assert f"http_port: {MONITOR_PORT}" in content

    def test_minimal_config_no_fleet(self):
        with patch("multifleet.nats_cluster.CONFIG_PATH", Path("/nonexistent")):
            content = generate_config()

        # Should fall back to minimal standalone config
        assert "jetstream" in content
        assert f"listen: 0.0.0.0:{CLIENT_PORT}" in content

    def test_chief_role_detected(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac1")

        assert "Role: chief" in content

    def test_worker_role_detected(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path):
            content = generate_config("mac2")

        assert "Role: worker" in content


class TestClientUrls:

    def test_returns_all_nodes(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path), \
             patch("multifleet.nats_cluster._detect_node_id", return_value="mac1"):
            urls = client_urls()

        # Should have 3 URLs (self via loopback + 2 peers)
        assert len(urls) == 3
        assert f"nats://127.0.0.1:{CLIENT_PORT}" in urls[0]  # self is first
        assert any("10.0.0.2" in u for u in urls)
        assert any("10.0.0.3" in u for u in urls)

    def test_self_first_for_latency(self, fleet_config):
        config, conf_path = fleet_config
        with patch("multifleet.nats_cluster.CONFIG_PATH", conf_path), \
             patch("multifleet.nats_cluster._detect_node_id", return_value="mac2"):
            urls = client_urls()

        # Self (127.0.0.1) should always be first for lowest latency
        assert urls[0] == f"nats://127.0.0.1:{CLIENT_PORT}"

    def test_fallback_no_config(self):
        with patch("multifleet.nats_cluster.CONFIG_PATH", Path("/nonexistent")):
            urls = client_urls()

        assert urls == [f"nats://127.0.0.1:{CLIENT_PORT}"]


class TestDetectNodeId:

    def test_env_override(self, fleet_config):
        config, _ = fleet_config
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac99"}):
            assert _detect_node_id(config) == "mac99"

    def test_ip_match(self, fleet_config):
        config, _ = fleet_config
        with patch("multifleet.nats_cluster._get_local_ip", return_value="10.0.0.2"), \
             patch.dict(os.environ, {}, clear=True):
            # Remove MULTIFLEET_NODE_ID if set
            env = dict(os.environ)
            env.pop("MULTIFLEET_NODE_ID", None)
            with patch.dict(os.environ, env, clear=True):
                assert _detect_node_id(config) == "mac2"


class TestServerStatus:

    def test_not_running(self):
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            status = server_status()

        assert status["running"] is False

    def test_running_with_routes(self):
        varz = json.dumps({
            "server_name": "mac1",
            "version": "2.12.6",
            "cluster": {"name": "contextdna-fleet"},
            "jetstream": {"config": {"store_dir": "/tmp/js"}},
            "connections": 5,
        }).encode()
        routez = json.dumps({
            "num_routes": 2,
            "routes": [
                {"remote_id": "abc12345", "ip": "10.0.0.2", "port": 6222},
                {"remote_id": "def67890", "ip": "10.0.0.3", "port": 6222},
            ],
        }).encode()

        def mock_urlopen(url, **kwargs):
            m = MagicMock()
            if "varz" in url:
                m.read.return_value = varz
            else:
                m.read.return_value = routez
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            return m

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            status = server_status()

        assert status["running"] is True
        assert status["server_name"] == "mac1"
        assert status["routes"] == 2
        assert len(status["route_details"]) == 2
