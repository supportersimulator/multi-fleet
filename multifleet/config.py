"""
Fleet Nerve Config — unified peer discovery.

Priority: .multifleet/config.json (scales to 100+) → 3s-network.local.conf (legacy)
Discovered peers (mDNS/NATS) merged in; config takes priority for known nodes.
No hardcoded IPs or node names. Open-source ready.
"""

import json
import logging
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from ._paths import REPO_ROOT, get_superrepo_root

logger = logging.getLogger("fleet.config")

# Config lives at superrepo root when running inside one, otherwise package root
_CONFIG_ROOT = get_superrepo_root()
JSON_CONF = _CONFIG_ROOT / ".multifleet" / "config.json"
SHELL_CONF = _CONFIG_ROOT / "scripts" / "3s-network.local.conf"
DISCOVERY_CACHE = _CONFIG_ROOT / ".multifleet" / "discovered_peers.json"


def detect_node_id() -> str:
    """Detect node ID from env var, config IP match, or hostname fallback."""
    env = os.environ.get("MULTIFLEET_NODE_ID")
    if env:
        return env

    # Try matching our IP to a node in config.json
    my_ip = _get_local_ip()
    if my_ip and JSON_CONF.exists():
        try:
            cfg = json.loads(JSON_CONF.read_text())
            for nid, node in cfg.get("nodes", {}).items():
                if node.get("host") == my_ip:
                    return nid
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Config JSON parse failed during node ID detection: %s", e)
            pass

    return socket.gethostname().split(".")[0].lower()


def _get_local_ip() -> Optional[str]:
    """Get the machine's LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.debug("Local IP detection failed: %s", e)
        return None


def load_peers(conf_path=None, include_discovered: bool = True) -> Tuple[dict, Optional[str]]:
    """Load peer registry. JSON config first (scales to 100+), then shell conf.
    Merges in discovered peers (mDNS/NATS) — config takes priority for known nodes.

    Returns:
        (peers, chief) where peers = {"node1": {"user": "...", "ip": "...", "port": 8855}, ...}
    """
    if conf_path is None and JSON_CONF.exists():
        try:
            peers, chief = _load_from_json(JSON_CONF)
            if include_discovered:
                peers = _merge_discovered(peers)
            return peers, chief
        except Exception as e:
            logger.debug("JSON config load failed, falling through to shell conf: %s", e)
            pass

    shell_path = Path(conf_path) if conf_path else SHELL_CONF
    if shell_path.exists():
        peers, chief = _load_from_shell(shell_path)
        if include_discovered:
            peers = _merge_discovered(peers)
        return peers, chief

    # No config at all — return only discovered peers if available
    if include_discovered:
        discovered = _load_discovered_cache()
        return discovered, None

    return {}, None


def _merge_discovered(config_peers: dict) -> dict:
    """Merge discovered peers into config peers. Config takes priority."""
    discovered = _load_discovered_cache()
    merged = dict(config_peers)
    for node_id, info in discovered.items():
        if node_id not in merged:
            merged[node_id] = {
                "user": info.get("user", ""),
                "ip": info.get("ip", ""),
                "port": info.get("port", 8855),
                "role": info.get("role", "worker"),
                "mac_address": info.get("mac_address", ""),
                "_source": info.get("source", "discovered"),
            }
    return merged


def _load_discovered_cache() -> dict:
    """Load cached discovered peers from disk."""
    if not DISCOVERY_CACHE.exists():
        return {}
    try:
        import time
        cached = json.loads(DISCOVERY_CACHE.read_text())
        now = time.time()
        peers = {}
        for node_id, info in cached.items():
            last_seen = info.get("last_seen", 0)
            # Only include peers seen within eviction window (30 min)
            if now - last_seen <= 1800:
                peers[node_id] = info
        return peers
    except Exception as e:
        logger.warning(f"Failed to load discovery cache: {e}")
        return {}


def save_discovered_peers(peers: dict):
    """Write newly discovered peers to cache file.

    Args:
        peers: dict of {node_id: {ip, port, last_seen, source, ...}}
               Only non-config peers should be passed.
    """
    try:
        DISCOVERY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        # Filter out config-sourced peers (they reload from their own source)
        cacheable = {
            nid: info for nid, info in peers.items()
            if info.get("source") != "config"
        }
        DISCOVERY_CACHE.write_text(json.dumps(cacheable, indent=2))
        logger.debug(f"Saved {len(cacheable)} discovered peers to cache")
    except Exception as e:
        logger.warning(f"Failed to save discovery cache: {e}")


def _load_from_json(path: Path) -> Tuple[dict, Optional[str]]:
    """Load from .multifleet/config.json — scales to 100+ nodes."""
    cfg = json.loads(path.read_text())
    peers = {}
    chief = cfg.get("chief", {}).get("nodeId")

    for nid, node in cfg.get("nodes", {}).items():
        peers[nid] = {
            "user": node.get("user", os.environ.get("USER", "")),
            "ip": node.get("host", ""),
            "port": node.get("port", 8855),
            "role": node.get("role", "worker"),
            "mac_address": node.get("mac_address", ""),
            "tailscale_ip": node.get("tailscale_ip", ""),
        }

    return peers, chief


def _load_from_shell(path: Path) -> Tuple[dict, Optional[str]]:
    """Load from 3s-network.local.conf — legacy format."""
    text = path.read_text()
    peers = {}
    chief = None

    names_match = re.search(r'PEER_NAMES="([^"]+)"', text)
    if not names_match:
        return {}, None
    names = names_match.group(1).split()

    for name in names:
        m = re.search(rf'PEER_{name}="([^@]+)@([^"]+)"', text)
        if m:
            peers[name] = {"user": m.group(1), "ip": m.group(2), "port": 8855}
            mac_m = re.search(rf'MAC_{name}="([^"]+)"', text)
            if mac_m:
                peers[name]["mac_address"] = mac_m.group(1)

    chief_match = re.search(r'CHIEF="([^"]+)"', text)
    if chief_match:
        chief = chief_match.group(1)

    return peers, chief


def is_tailscale_available() -> bool:
    """Check if Tailscale is running on this machine."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return data.get("BackendState") == "Running"
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.debug("Tailscale availability check failed: %s", e)
        return False


def resolve_tailscale_ip(peer_config: dict, peer_name: str = "") -> Optional[str]:
    """Resolve Tailscale IP for a peer.

    Priority:
    1. Static config field 'tailscale_ip' (from .multifleet/config.json)
    2. Dynamic lookup via 'tailscale status --json' matching peer hostname

    Returns the Tailscale IP string, or None if unavailable.
    """
    # 1. Static config
    static_ip = peer_config.get("tailscale_ip", "")
    if static_ip:
        return static_ip

    # 2. Dynamic fallback — search tailscale status for peer hostname
    if not peer_name:
        return None
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        for _node_key, node_info in data.get("Peer", {}).items():
            # Match by hostname (case-insensitive, strip trailing dot)
            ts_hostname = node_info.get("HostName", "").lower().rstrip(".")
            if ts_hostname == peer_name.lower():
                addrs = node_info.get("TailscaleIPs", [])
                if addrs:
                    return addrs[0]  # prefer first (IPv4 typically)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        logger.debug("Tailscale IP resolution failed for %s: %s", peer_name, e)

    return None
