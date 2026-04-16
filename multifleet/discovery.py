"""
Fleet discovery, gossip, and heartbeat for fleet messaging.

Extracted from nats_client.py — contains:
- FleetDiscoveryManager: peer IP aggregation, gossip target selection,
  UDP gossip send/receive loops, NATS heartbeat publishing, heartbeat handling
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .nats_client import FleetNerveNATS

from .channels import GOSSIP_FANOUT, GOSSIP_TTL, UDP_GOSSIP_PORT

logger = logging.getLogger("fleet_nats")

# ── Redis peer heartbeat tracking (memory module wire) ──
# Records per-peer heartbeat timestamps in Redis for stall detection.
# Peers missing 3+ beats (30s at 10s interval) are flagged as stalled.
_REDIS_PEER_HB_PREFIX = "fleet:peer_heartbeat:"
_REDIS_PEER_HB_TTL = 60  # expire after 60s (6 missed beats)
_REDIS_STALLED_KEY = "fleet:stalled_peers"
_HEARTBEAT_STALL_THRESHOLD_S = 30  # 3 missed beats at 10s interval
_redis_hb_client = None


def _get_redis_hb():
    """Lazy-load Redis client for heartbeat tracking."""
    global _redis_hb_client
    if _redis_hb_client is not None:
        return _redis_hb_client
    try:
        from memory.redis_cache import get_redis_client
        _redis_hb_client = get_redis_client()
    except Exception:
        _redis_hb_client = False  # sentinel
    return _redis_hb_client if _redis_hb_client else None


def _record_peer_heartbeat(node: str, ts: float):
    """Record a peer heartbeat in Redis. Clears stalled flag if present."""
    r = _get_redis_hb()
    if not r:
        return
    try:
        import json as _json
        key = f"{_REDIS_PEER_HB_PREFIX}{node}"
        r.setex(key, _REDIS_PEER_HB_TTL, _json.dumps({"ts": ts, "node": node}))
        # Clear stalled flag for this peer
        r.hdel(_REDIS_STALLED_KEY, node)
    except Exception:
        pass


def get_stalled_fleet_peers() -> list:
    """Return list of peers whose heartbeat key has expired (stalled).

    Called by health checks or watchdog to detect unresponsive peers.
    A peer is stalled if its Redis heartbeat key is absent but was
    previously known (still in the stalled set or config).
    """
    r = _get_redis_hb()
    if not r:
        return []
    try:
        stalled = r.hgetall(_REDIS_STALLED_KEY)
        return list(stalled.keys()) if stalled else []
    except Exception:
        return []


def flag_stalled_peer(node: str):
    """Mark a peer as stalled in Redis (called when heartbeat key expires)."""
    r = _get_redis_hb()
    if not r:
        return
    try:
        import json as _json
        r.hset(_REDIS_STALLED_KEY, node, _json.dumps({"since": time.time()}))
    except Exception:
        pass


class FleetDiscoveryManager:
    """Manages peer discovery, gossip protocol, and heartbeat lifecycle."""

    def __init__(self, nerve: "FleetNerveNATS"):
        self._nerve = nerve

    # ── Heartbeat handling ──

    async def handle_heartbeat(self, msg):
        """Track peer heartbeats. Auto-adds unknown peers. Clears stale repair/channel state."""
        nerve = self._nerve
        try:
            data = json.loads(msg.data.decode())
            node = data.get("node", "unknown")
            if node != nerve.node_id:
                was_unknown = node not in nerve._peers and node not in nerve._peers_config
                # Protocol version negotiation
                from multifleet.protocol import check_peer_version
                peer_ver = data.get("protocol_version")
                compat, ver_status = check_peer_version(peer_ver)
                nerve._peers[node] = {**data, "_received_at": time.time(), "_version_status": ver_status}
                # Record heartbeat in Redis for cross-process stall detection
                _record_peer_heartbeat(node, time.time())
                if not compat:
                    logger.warning(f"Peer {node} has incompatible protocol: {peer_ver} ({ver_status})")
                # Fresh heartbeat = peer is alive on NATS. Clear any stale repair state.
                if hasattr(nerve, '_repair_in_progress'):
                    nerve._repair_in_progress.pop(node, None)
                if hasattr(nerve, 'protocol') and nerve.protocol:
                    nerve.protocol.channel_state.record_success(node, "P1_nats", 0)
                # Auto-add unknown peers to discovery (don't reject)
                if was_unknown:
                    logger.info(f"Auto-discovered new peer via NATS heartbeat: {node}")
                if hasattr(nerve, '_discovery') and nerve._discovery:
                    peer_ip = data.get("ip", "")
                    peer_port = data.get("port", 8855)
                    nerve._discovery.add_nats_peer(node, ip=peer_ip, port=peer_port)
                # Wire 5: Update node overlay from heartbeat health data
                try:
                    from multifleet.node_overlay import NodeOverlayManager
                    if hasattr(nerve, '_http_handler_cls') and hasattr(nerve._http_handler_cls, '_overlay_manager'):
                        mgr = nerve._http_handler_cls._overlay_manager
                        if mgr is not None:
                            mgr.update_from_health(node, data)
                except Exception:
                    pass  # overlay update is best-effort
        except Exception:
            pass

    # ── Heartbeat loop ──

    async def heartbeat_loop(self):
        """Publish heartbeat every 10s."""
        nerve = self._nerve
        # Import here to avoid circular — module-level in nats_client.py
        try:
            from tools import fleet_historian_nats
        except Exception:
            fleet_historian_nats = None  # type: ignore[assignment]

        while nerve._running:
            try:
                detected = nerve._detect_sessions()
                from multifleet.protocol import PROTOCOL_VERSION
                hb = {
                    "node": nerve.node_id,
                    "timestamp": time.time(),
                    "ip": self.get_local_ip(),
                    "protocol_version": PROTOCOL_VERSION,
                    "sessions": len(detected),
                    "session_list": [
                        {"sessionId": s.get("sessionId", ""), "idle_s": s.get("idle_s", 0)}
                        for s in detected
                    ],
                    "vscode": nerve._detect_vscode_state(),
                    "git": nerve._get_git_context(),
                }
                await nerve.nc.publish("fleet.heartbeat", json.dumps(hb).encode())

                # Publish session historian data (change-driven, piggybacked on heartbeat)
                if fleet_historian_nats:
                    try:
                        await fleet_historian_nats.publish_all_session_data(nerve)
                    except Exception as e:
                        logger.debug(f"Session publish failed: {e}")

                # Ghost Pulse — tick every 30th heartbeat (~5 min at 10s interval)
                if getattr(nerve, '_ghost_pulse', None):
                    try:
                        nerve._ghost_pulse_counter += 1
                        if nerve._ghost_pulse_counter >= 30:
                            nerve._ghost_pulse_counter = 0
                            pulse_result = nerve._ghost_pulse.pulse()
                            if pulse_result:
                                logger.debug("GhostPulse: %s", pulse_result.get("status", "ok"))
                    except Exception as e:
                        logger.debug(f"GhostPulse tick failed: {e}")

                # Evidence Sync — tick every 30th heartbeat (~5 min)
                if getattr(nerve, '_evidence_sync', None):
                    try:
                        nerve._evidence_sync_counter += 1
                        if nerve._evidence_sync_counter >= 30:
                            nerve._evidence_sync_counter = 0
                            packet = nerve._evidence_sync.export_recent(since_hours=1)
                            if packet and nerve.nc and nerve.nc.is_connected:
                                await nerve.nc.publish(
                                    f"fleet.{nerve.node_id}.evidence",
                                    json.dumps(packet).encode(),
                                )
                                logger.debug("EvidenceSync: exported %d items", len(packet.get("items", [])))
                    except Exception as e:
                        logger.debug(f"EvidenceSync tick failed: {e}")

            except Exception as e:
                logger.debug(f"Heartbeat publish failed: {e}")
            await asyncio.sleep(10)

    # ── Local IP ──

    def get_local_ip(self) -> str:
        """Get LAN IP for heartbeat advertisement."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return ""

    # ── UDP Gossip ──

    def get_known_peer_ips(self) -> dict[str, str]:
        """Return {node_id: ip} for all known peers (from heartbeats + config)."""
        nerve = self._nerve
        peers = {}
        # From NATS heartbeat data (runtime-discovered)
        for node, data in nerve._peers.items():
            ip = data.get("ip")
            if ip and node != nerve.node_id:
                peers[node] = ip
        # From static peer config (fleet_nerve_config)
        for node, cfg in nerve._peers_config.items():
            if node != nerve.node_id and node not in peers:
                ip = cfg.get("ip") or cfg.get("host")
                if ip:
                    peers[node] = ip
        return peers

    def select_gossip_targets(self, exclude_nodes: set[str] | None = None) -> list[tuple[str, str]]:
        """Select up to GOSSIP_FANOUT random peers to gossip to.

        Returns list of (node_id, ip) tuples. Excludes self and any nodes in exclude_nodes.
        """
        peer_ips = self.get_known_peer_ips()
        if exclude_nodes:
            peer_ips = {n: ip for n, ip in peer_ips.items() if n not in exclude_nodes}
        candidates = list(peer_ips.items())
        if len(candidates) <= GOSSIP_FANOUT:
            return candidates
        return random.sample(candidates, GOSSIP_FANOUT)

    async def udp_gossip_loop(self):
        """Send UDP gossip heartbeats every 10s to GOSSIP_FANOUT random peers."""
        nerve = self._nerve
        # Wait for initial NATS heartbeats to populate peer list
        await asyncio.sleep(15)
        logger.info(f"UDP gossip heartbeat started (fanout={GOSSIP_FANOUT}, ttl={GOSSIP_TTL}, port={UDP_GOSSIP_PORT})")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        while nerve._running:
            try:
                targets = self.select_gossip_targets()
                if not targets:
                    await asyncio.sleep(10)
                    continue

                detected = nerve._detect_sessions()
                from multifleet.protocol import PROTOCOL_VERSION as _PV
                hb = {
                    "node": nerve.node_id,
                    "timestamp": time.time(),
                    "ip": self.get_local_ip(),
                    "protocol_version": _PV,
                    "sessions": len(detected),
                    "session_list": [
                        {"sessionId": s.get("sessionId", ""), "idle_s": s.get("idle_s", 0)}
                        for s in detected
                    ],
                    "vscode": nerve._detect_vscode_state(),
                    "git": nerve._get_git_context(),
                    # Gossip metadata
                    "_gossip_ttl": GOSSIP_TTL,
                    "_gossip_path": [nerve.node_id],
                    "_gossip_origin": nerve.node_id,
                }
                payload = json.dumps(hb).encode()

                for node_id, ip in targets:
                    try:
                        sock.sendto(payload, (ip, UDP_GOSSIP_PORT))
                        logger.debug(f"Gossip heartbeat sent to {node_id} ({ip}:{UDP_GOSSIP_PORT})")
                    except Exception as e:
                        logger.debug(f"Gossip send to {node_id} ({ip}) failed: {e}")
            except Exception as e:
                logger.debug(f"UDP gossip loop error: {e}")
            await asyncio.sleep(10)

    async def udp_gossip_listener(self):
        """Listen for UDP gossip heartbeats and forward to more peers (with TTL)."""
        nerve = self._nerve
        loop = asyncio.get_running_loop()

        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", UDP_GOSSIP_PORT))
        except OSError as e:
            logger.warning(f"UDP gossip listener bind failed on port {UDP_GOSSIP_PORT}: {e}")
            return
        sock.setblocking(False)

        logger.info(f"UDP gossip listener started on port {UDP_GOSSIP_PORT}")

        # Forward socket for gossip relay
        fwd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fwd_sock.setblocking(False)

        while nerve._running:
            try:
                # Non-blocking recv via asyncio
                data, addr = await loop.run_in_executor(None, lambda: sock.recvfrom(65535))
                hb = json.loads(data.decode())

                node = hb.get("node", "unknown")
                if node == nerve.node_id:
                    continue  # Ignore own heartbeats

                # Update peer state (same as NATS _handle_heartbeat)
                from multifleet.protocol import check_peer_version
                peer_ver = hb.get("protocol_version")
                compat, ver_status = check_peer_version(peer_ver)
                nerve._peers[node] = {**hb, "_received_at": time.time(), "_via": "udp_gossip", "_version_status": ver_status}
                if not compat:
                    logger.warning(f"Gossip peer {node} has incompatible protocol: {peer_ver} ({ver_status})")
                if hasattr(nerve, '_repair_in_progress'):
                    nerve._repair_in_progress.pop(node, None)
                if hasattr(nerve, 'protocol') and nerve.protocol:
                    nerve.protocol.channel_state.record_success(node, "P1_nats", 0)

                # Gossip forwarding: decrement TTL, forward to more peers
                ttl = hb.get("_gossip_ttl", 0)
                gossip_path = hb.get("_gossip_path", [])

                if ttl > 0:
                    # Add ourselves to the path
                    new_path = gossip_path + [nerve.node_id]
                    hb["_gossip_ttl"] = ttl - 1
                    hb["_gossip_path"] = new_path

                    # Don't forward back to sender, origin, or anyone in the path
                    exclude = set(new_path) | {hb.get("_gossip_origin", "")}
                    targets = self.select_gossip_targets(exclude_nodes=exclude)

                    if targets:
                        fwd_payload = json.dumps(hb).encode()
                        for fwd_node, fwd_ip in targets:
                            try:
                                fwd_sock.sendto(fwd_payload, (fwd_ip, UDP_GOSSIP_PORT))
                                logger.debug(f"Gossip forwarded {node}'s heartbeat to {fwd_node} (ttl={ttl - 1})")
                            except Exception as e:
                                logger.debug(f"Gossip forward to {fwd_node} failed: {e}")

            except json.JSONDecodeError:
                logger.debug("UDP gossip: invalid JSON received")
            except BlockingIOError:
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.debug(f"UDP gossip listener error: {e}")
                await asyncio.sleep(1)

# Backward compatibility alias (daemon imports FleetDiscovery)
FleetDiscovery = FleetDiscoveryManager
