#!/usr/bin/env python3
"""
Fleet Nerve v2 -- NATS-based real-time fleet communication.

Replaces the HTTP daemon with NATS pub/sub. Zero port conflicts, zero polling,
instant message delivery, built-in offline queuing via JetStream.

Architecture:
  - mac1 runs nats-server (chief) -- all nodes connect to nats://mac1:4222
  - Each node subscribes to fleet.<node>.> and fleet.all.>
  - Messages publish instantly -- no HTTP, no seed files, no hooks
  - JetStream persists messages for offline nodes (they get them on reconnect)
  - Request/reply pattern for synchronous queries (health, status)

Subjects:
  fleet.<node>.message     -- direct message to a node
  fleet.all.message        -- broadcast to all nodes
  fleet.<node>.health      -- request/reply health check
  fleet.all.health         -- broadcast health request
  fleet.<node>.task        -- autonomous task assignment
  fleet.all.alert          -- fleet-wide alert
  fleet.heartbeat          -- periodic heartbeat (all nodes publish)
  fleet.<node>.ack         -- delivery acknowledgment back to sender

Usage:
  python3 tools/fleet_nerve_nats.py                    # start daemon
  python3 tools/fleet_nerve_nats.py send mac2 "hello"  # send message
  python3 tools/fleet_nerve_nats.py broadcast "hello"  # broadcast
  python3 tools/fleet_nerve_nats.py health              # fleet health
  python3 tools/fleet_nerve_nats.py monitor             # live monitor
"""

import asyncio
import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Handle import for both pip install locations
try:
    import nats
    from nats.errors import ConnectionClosedError, TimeoutError as NatsTimeout
except ImportError:
    print("ERROR: nats-py not installed. Run: pip3 install nats-py")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Session historian NATS (standalone module)
try:
    from tools import fleet_historian_nats
except ImportError:
    fleet_historian_nats = None  # type: ignore[assignment]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fleet-nats/%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fleet_nats")

# ── Config ──
NODE_ID = os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname().split(".")[0]
def _default_nats_url():
    """Resolve NATS URL from config or env.

    Priority: NATS_URL env > tunnel detection > config file > loopback.
    If an SSH tunnel is forwarding port 4222 locally, use 127.0.0.1 to avoid
    router-blocked direct connections.
    """
    # Check if a local tunnel is forwarding port 4222
    try:
        import subprocess as _sp
        out = _sp.check_output(["lsof", "-i", ":4222", "-sTCP:LISTEN"], text=True, timeout=3, stderr=_sp.DEVNULL)
        if "ssh" in out.lower():
            return "nats://127.0.0.1:4222"
    except Exception as e:
        logger.debug(f"SSH tunnel check for NATS failed: {e}")

    # Try config file for chief IP
    conf = REPO_ROOT / "scripts" / "3s-network.local.conf"
    if conf.exists():
        for line in conf.read_text().split("\n"):
            if line.startswith("PEER_mac1="):
                ip = line.split("@")[-1].strip('"')
                return f"nats://{ip}:4222"

    # Try multifleet config
    mf_conf = REPO_ROOT / ".multifleet" / "config.json"
    if mf_conf.exists():
        try:
            import json as _json
            c = _json.loads(mf_conf.read_text())
            chief_host = c.get("chief", {}).get("host", "")
            if chief_host:
                return f"nats://{chief_host}:4222"
        except Exception as e:
            logger.debug(f"Failed to read NATS URL from multifleet config: {e}")

    return "nats://localhost:4222"

NATS_URL = os.environ.get("NATS_URL") or _default_nats_url()
SEED_DIR = "/tmp"
STREAM_NAME = "FLEET"
ALL_CHANNELS = ("P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git")
HEAL_SCORE_THRESHOLD = 5  # Score below this triggers automatic repair escalation

# ── Channel Priority Constants ──
CHANNEL_P0_CLOUD = "P0_cloud"
CHANNEL_P1_NATS = "P1_nats"
CHANNEL_P2_HTTP = "P2_http"
CHANNEL_P3_CHIEF = "P3_chief"
CHANNEL_P4_SEED = "P4_seed"
CHANNEL_P5_SSH = "P5_ssh"
CHANNEL_P6_WOL = "P6_wol"
CHANNEL_P7_GIT = "P7_git"
CHANNEL_P8_KEYSTROKE = "P8_keystroke"

CHANNEL_ORDER = [
    CHANNEL_P1_NATS, CHANNEL_P2_HTTP, CHANNEL_P3_CHIEF,
    CHANNEL_P4_SEED, CHANNEL_P5_SSH, CHANNEL_P6_WOL,
    CHANNEL_P7_GIT, CHANNEL_P8_KEYSTROKE,
]

# Per-channel timeout (30s max each, but tuned per channel for efficiency)
CHANNEL_TIMEOUT_S = {
    CHANNEL_P0_CLOUD: 30,
    CHANNEL_P1_NATS: 30,
    CHANNEL_P2_HTTP: 30,
    CHANNEL_P3_CHIEF: 30,
    CHANNEL_P4_SEED: 30,
    CHANNEL_P5_SSH: 30,
    CHANNEL_P6_WOL: 30,
    CHANNEL_P7_GIT: 30,
    CHANNEL_P8_KEYSTROKE: 30,
}

# Self-heal loop interval
SELF_HEAL_INTERVAL_S = 60
# Max peers to probe per self-heal cycle — dynamic: max(10, sqrt(N))
# At 3 nodes: 10 (covers all). At 100: 10. At 1000: 32. (3-surgeon recommendation)
import math as _math
def _dynamic_probe_count(n_peers: int) -> int:
    return max(10, int(_math.sqrt(n_peers)))
SELF_HEAL_MAX_PROBES_PER_CYCLE = 10  # default, overridden dynamically in _self_heal_cycle
# Repair cooldown per action per peer
REPAIR_COOLDOWN_S = 300  # 5 min

# Broadcast scaling constants
BROADCAST_PARALLEL_LIMIT = 20  # Max concurrent HTTP broadcasts (prevents socket exhaustion at 100+)
BROADCAST_HTTP_TIMEOUT_S = 5   # Timeout per peer during HTTP broadcast fallback
GIT_BROADCAST_COOLDOWN_S = 3600  # Max 1 git broadcast commit per hour
# ── Gossip Protocol Constants (UDP direct heartbeats) ──
# At 3 nodes: fanout=3 covers everyone (same as full-mesh).
# At 100 nodes: 3 hops reaches ~27 nodes/cycle, full coverage in ~4 cycles (40s).
GOSSIP_FANOUT = 3   # Number of random peers to send each heartbeat to
GOSSIP_TTL = 3      # Max forwarding hops (decremented each hop, 0 = don't forward)
UDP_GOSSIP_PORT = 8866  # UDP port for gossip heartbeats


class ChannelState:
    """Tracks per-peer, per-channel health state."""

    def __init__(self):
        # {peer: {channel: {status, last_success, last_failure, latency_ms, fail_count}}}
        self._state: dict = {}

    def _ensure_peer(self, peer: str, channel: str):
        if peer not in self._state:
            self._state[peer] = {}
        if channel not in self._state[peer]:
            self._state[peer][channel] = {
                "status": "unknown",
                "last_success": 0.0,
                "last_failure": 0.0,
                "latency_ms": 0,
                "fail_count": 0,
            }

    def record_success(self, peer: str, channel: str, latency_ms: int):
        """Record a successful channel send."""
        self._ensure_peer(peer, channel)
        entry = self._state[peer][channel]
        entry["status"] = "ok"
        entry["last_success"] = time.time()
        entry["latency_ms"] = latency_ms
        entry["fail_count"] = 0
        prev = entry.get("_prev_status")
        if prev and prev != "ok":
            logger.info(f"Channel state change: {peer}/{channel} {prev} -> ok")
        entry["_prev_status"] = "ok"

    def record_failure(self, peer: str, channel: str, reason: str = ""):
        """Record a failed channel send."""
        self._ensure_peer(peer, channel)
        entry = self._state[peer][channel]
        prev_status = entry["status"]
        entry["status"] = "broken"
        entry["last_failure"] = time.time()
        entry["fail_count"] = entry.get("fail_count", 0) + 1
        if prev_status != "broken":
            logger.info(f"Channel state change: {peer}/{channel} {prev_status} -> broken ({reason})")
        entry["_prev_status"] = "broken"

    def is_known_broken(self, peer: str, channel: str, max_age_s: float = 120.0) -> bool:
        """Check if a channel is known broken (failed recently)."""
        if peer not in self._state or channel not in self._state[peer]:
            return False
        entry = self._state[peer][channel]
        if entry["status"] != "broken":
            return False
        # Stale broken state (>max_age_s) -- retry it
        if time.time() - entry["last_failure"] > max_age_s:
            return False
        return True

    def get_broken_channels(self, peer: str) -> list:
        """Return list of broken channel names for a peer."""
        if peer not in self._state:
            return []
        return [ch for ch, v in self._state[peer].items() if v["status"] == "broken"]

    def get_all(self) -> dict:
        """Return full state dict (safe for JSON serialization)."""
        result = {}
        for peer, channels in self._state.items():
            result[peer] = {}
            for ch, v in channels.items():
                # Omit internal keys
                result[peer][ch] = {k: v2 for k, v2 in v.items() if not k.startswith("_")}
        return result

    def get_peer_summary(self, peer: str) -> dict:
        """Summarized channel health for one peer."""
        if peer not in self._state:
            return {}
        return {ch: {"status": v["status"], "latency_ms": v["latency_ms"]}
                for ch, v in self._state[peer].items() if not ch.startswith("_")}

# ── Subjects ──
def _sub_direct(node: str) -> str:
    return f"fleet.{node}.>"

def _sub_all() -> str:
    return "fleet.all.>"

def _sub_heartbeat() -> str:
    return "fleet.heartbeat"

def _sub_ack(node: str) -> str:
    return f"fleet.{node}.ack"


class CommunicationProtocol:
    """Invariant self-healing communication protocol.

    Enforces:
    - Channel cascade P0-P8 with 30s timeout per channel
    - Standing self-heal loop (every 60s) that probes peers and auto-repairs
    - Channel state tracking per peer, exposed via get_channel_state()
    - Secrets protection: never logs message content, API keys, or auth tokens
    - P8 (direct user text input) as absolute last resort, rate-limited 1/30s
    """

    def __init__(self, nerve: "FleetNerveNATS"):
        self._nerve = nerve
        self.channel_state = ChannelState()
        self._heal_cooldowns: dict = {}  # "peer:action" -> last_attempt_time
        self._last_probed: dict = {}  # peer_id -> timestamp of last probe
        self._self_heal_task: Optional[asyncio.Task] = None

    def start(self):
        """Start the standing self-heal background loop."""
        if self._self_heal_task is None or self._self_heal_task.done():
            self._self_heal_task = asyncio.create_task(self._self_heal_loop())
            logger.info("CommunicationProtocol: self-heal loop started (60s interval)")

    def stop(self):
        """Stop the self-heal loop."""
        if self._self_heal_task and not self._self_heal_task.done():
            self._self_heal_task.cancel()
            logger.info("CommunicationProtocol: self-heal loop stopped")

    # ── Channel Cascade (30s per channel) ──

    async def send(self, target: str, message: dict, use_cloud: bool = False) -> dict:
        """Send message via channel cascade. Each channel gets max 30s.

        P0 Cloud only when use_cloud=True or all local channels fail.
        P8 Keystroke only as absolute last resort after all others fail.
        Never logs message content, API keys, or auth tokens.
        """
        channels = []
        if use_cloud:
            channels.append((CHANNEL_P0_CLOUD, self._nerve._send_p0_cloud))
        channels.extend([
            (CHANNEL_P1_NATS, self._nerve._send_p1_nats),
            (CHANNEL_P2_HTTP, self._nerve._send_p2_http),
            (CHANNEL_P3_CHIEF, self._nerve._send_p3_chief),
            (CHANNEL_P4_SEED, self._nerve._send_p4_seed),
            (CHANNEL_P5_SSH, self._nerve._send_p5_ssh),
            (CHANNEL_P6_WOL, self._nerve._send_p6_wol),
            (CHANNEL_P7_GIT, self._nerve._send_p7_git),
        ])

        # Ensure message envelope
        if "id" not in message:
            message["id"] = str(uuid.uuid4())
        if "from" not in message:
            message["from"] = self._nerve.node_id
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

        errors = []
        for name, sender in channels:
            # Skip known-broken channels (saves time, retried by self-heal loop)
            if self.channel_state.is_known_broken(target, name):
                errors.append(f"{name}: skipped (known broken)")
                logger.debug(f"[{name}] Skipped for {target} -- known broken")
                continue

            timeout = CHANNEL_TIMEOUT_S.get(name, 30)
            t0 = time.time()
            try:
                result = await asyncio.wait_for(sender(target, message), timeout=timeout)
                elapsed_ms = int((time.time() - t0) * 1000)
                if result.get("delivered"):
                    self.channel_state.record_success(target, name, elapsed_ms)
                    logger.info(f"[{name}] Delivered to {target} ({elapsed_ms}ms)")
                    # If we fell through to P3+, earlier channels are broken -- trigger heal
                    if name not in (CHANNEL_P0_CLOUD, CHANNEL_P1_NATS, CHANNEL_P2_HTTP):
                        broken = [e.split(":")[0] for e in errors if "skipped" not in e]
                        if broken:
                            asyncio.ensure_future(self._trigger_heal(target, name, broken))
                    return {
                        "delivered": True, "channel": name,
                        "elapsed_ms": elapsed_ms, **result,
                    }
                if result.get("wol_sent"):
                    self.channel_state.record_failure(target, name, "wol_only")
                    errors.append(f"{name}: WoL sent, not delivered")
                    continue
                # Not delivered -- record failure
                reason = result.get("error", "delivered=false")
                self.channel_state.record_failure(target, name, reason)
                errors.append(f"{name}: {reason}")
            except asyncio.TimeoutError:
                elapsed_ms = int((time.time() - t0) * 1000)
                self.channel_state.record_failure(target, name, f"timeout ({timeout}s)")
                errors.append(f"{name}: timeout ({timeout}s)")
                logger.debug(f"[{name}] Timed out for {target} after {timeout}s")
            except Exception as e:
                elapsed_ms = int((time.time() - t0) * 1000)
                self.channel_state.record_failure(target, name, str(e))
                errors.append(f"{name}: {e}")
                logger.debug(f"[{name}] Failed for {target}: {e}")

        # P0 cloud fallback if not already tried
        if not use_cloud:
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    self._nerve._send_p0_cloud(target, message),
                    timeout=CHANNEL_TIMEOUT_S[CHANNEL_P0_CLOUD])
                elapsed_ms = int((time.time() - t0) * 1000)
                if result.get("delivered"):
                    self.channel_state.record_success(target, CHANNEL_P0_CLOUD, elapsed_ms)
                    logger.info(f"[{CHANNEL_P0_CLOUD}] Delivered to {target} (fallback, {elapsed_ms}ms)")
                    return {"delivered": True, "channel": CHANNEL_P0_CLOUD, **result}
                self.channel_state.record_failure(target, CHANNEL_P0_CLOUD, "delivered=false")
            except Exception as e:
                self.channel_state.record_failure(target, CHANNEL_P0_CLOUD, str(e))
                errors.append(f"{CHANNEL_P0_CLOUD}: {e}")

        # P8: Direct user text input -- ABSOLUTE LAST RESORT
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                self._send_p8_keystroke(target, message),
                timeout=CHANNEL_TIMEOUT_S[CHANNEL_P8_KEYSTROKE])
            elapsed_ms = int((time.time() - t0) * 1000)
            if result.get("delivered"):
                self.channel_state.record_success(target, CHANNEL_P8_KEYSTROKE, elapsed_ms)
                return {"delivered": True, "channel": CHANNEL_P8_KEYSTROKE, **result}
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, "not_delivered")
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: not delivered")
        except asyncio.TimeoutError:
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, "timeout")
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: timeout")
        except Exception as e:
            self.channel_state.record_failure(target, CHANNEL_P8_KEYSTROKE, str(e))
            errors.append(f"{CHANNEL_P8_KEYSTROKE}: {e}")

        logger.warning(f"All channels (P0-P8) failed for {target}: {[e.split(':')[0] for e in errors]}")
        return {"delivered": False, "errors": errors}

    # ── P8: Direct User Text Input (LAST RESORT) ──

    _last_p8_time = 0.0  # Class-level rate limit: 1 per 30s

    async def _send_p8_keystroke(self, target: str, message: dict) -> dict:
        """P8: Direct user text input via osascript keystroke.

        LAST RESORT ONLY. Rate-limited to 1 per 30s. Logs a warning when used.
        Only works when target is the local machine (can't keystroke remote machines).
        Never includes message content in the keystroke -- only a trigger command.
        """
        # Only works for local node
        if target != self._nerve.node_id:
            return {"delivered": False, "error": "P8 only works for local node"}

        # Rate limit: max 1 keystroke per 30s
        now = time.time()
        if now - CommunicationProtocol._last_p8_time < 30:
            remaining = int(30 - (now - CommunicationProtocol._last_p8_time))
            return {"delivered": False, "error": f"P8 rate limited ({remaining}s remaining)"}

        logger.warning(
            f"P8 LAST RESORT: all channels P0-P7 failed for {target}. "
            f"Using direct keystroke input. This should be investigated."
        )
        CommunicationProtocol._last_p8_time = now

        # Write message to seed file first (keystroke just triggers reading it)
        payload = message.get("payload", {})
        seed_path = os.path.join(SEED_DIR, f"fleet-seed-{target}.md")
        sender = message.get("from", "unknown")
        subject = payload.get("subject", "fleet message")
        with open(seed_path, "a") as f:
            f.write(f"\n## [P8-LASTRESORT] from {sender}: {subject}\n\n")
            f.write("(Delivered via P8 keystroke -- all other channels failed)\n\n---\n")

        # Trigger VS Code keystroke
        self._nerve._keystroke_trigger(f"P8 last resort from {sender}: {subject}")
        return {"delivered": True, "method": "p8_keystroke"}

    # ── Standing Self-Heal Loop ──

    async def _self_heal_loop(self):
        """Background loop: every 60s, probe all peers and auto-repair broken channels.

        For each peer:
        1. HTTP health check (P2) to detect alive/dead
        2. Record which channels work vs broken
        3. If P1/P2 broken, spawn repair via best working channel
        4. Cooldown: 5min per action per peer
        """
        # Wait for startup stabilization — need heartbeats before judging health
        await asyncio.sleep(120)
        logger.info("Self-heal loop active -- probing peers every 60s")

        while True:
            try:
                await self._self_heal_cycle()
            except asyncio.CancelledError:
                logger.info("Self-heal loop cancelled")
                return
            except Exception as e:
                logger.error(f"Self-heal cycle error: {e}")
            await asyncio.sleep(SELF_HEAL_INTERVAL_S)

    async def _self_heal_cycle(self):
        """One cycle of the self-heal loop.

        Scales to 100+ nodes by probing a random subset per cycle:
        - Always includes peers with known broken channels (immediate attention)
        - Prioritizes peers not probed in the last 5 minutes
        - Caps at SELF_HEAL_MAX_PROBES_PER_CYCLE (default 10) per cycle
        - Full fleet coverage every ~N/10 cycles (10 min at 100 nodes)
        """
        all_peers = set(self._nerve._peers_config.keys()) - {self._nerve.node_id}
        if not all_peers:
            return

        now = time.time()
        max_probes = _dynamic_probe_count(len(all_peers))

        # 1. Always include peers with known broken channels
        broken_peers = set()
        for peer_id in all_peers:
            if self.channel_state.get_broken_channels(peer_id):
                broken_peers.add(peer_id)

        # 2. From remaining peers, prioritize those not probed in last 5 min
        remaining = all_peers - broken_peers
        stale_peers = [p for p in remaining
                       if now - self._last_probed.get(p, 0) > 300]
        fresh_peers = [p for p in remaining if p not in stale_peers]

        # 3. Build probe list: broken (all) + stale (random) + fresh (random)
        budget = max(0, max_probes - len(broken_peers))
        selected: list = list(broken_peers)

        if stale_peers:
            pick = min(budget, len(stale_peers))
            selected.extend(random.sample(stale_peers, pick))
            budget -= pick

        if budget > 0 and fresh_peers:
            pick = min(budget, len(fresh_peers))
            selected.extend(random.sample(fresh_peers, pick))

        logger.info(
            f"Self-heal cycle: probing {len(selected)}/{len(all_peers)} peers "
            f"(broken: {len(broken_peers)})"
        )

        for peer_id in selected:
            try:
                await self._probe_and_heal_peer(peer_id)
                self._last_probed[peer_id] = time.time()
            except Exception as e:
                logger.debug(f"Self-heal probe failed for {peer_id}: {e}")

    async def _probe_and_heal_peer(self, peer_id: str):
        """Probe a single peer's channels and trigger repair if needed."""
        peer = self._nerve._resolve_peer(peer_id)
        ip = peer.get("ip")
        if not ip:
            return

        # Probe P2 HTTP health
        p2_ok = False
        t0 = time.time()
        try:
            import urllib.request
            req = urllib.request.Request(f"http://{ip}:8855/health", method="GET")
            resp = urllib.request.urlopen(req, timeout=5)
            elapsed_ms = int((time.time() - t0) * 1000)
            if resp.status == 200:
                p2_ok = True
                self.channel_state.record_success(peer_id, CHANNEL_P2_HTTP, elapsed_ms)
        except Exception as e:
            elapsed_ms = int((time.time() - t0) * 1000)
            logger.debug(f"P2 health probe failed for {peer_id}: {e}")
            self.channel_state.record_failure(peer_id, CHANNEL_P2_HTTP, "health probe failed")

        # Probe P1 NATS (check heartbeat recency)
        p1_ok = False
        hb = self._nerve._peers.get(peer_id, {})
        if hb:
            last_seen = time.time() - hb.get("_received_at", 0)
            if last_seen < 45:  # Heartbeat within 45s = healthy
                p1_ok = True
                self.channel_state.record_success(peer_id, CHANNEL_P1_NATS, int(last_seen * 1000))
            else:
                self.channel_state.record_failure(
                    peer_id, CHANNEL_P1_NATS, f"stale heartbeat ({int(last_seen)}s)")
        else:
            self.channel_state.record_failure(peer_id, CHANNEL_P1_NATS, "no heartbeat received")

        # If P1 NATS is healthy, skip repair -- NATS is the primary channel.
        # P2 HTTP may legitimately fail behind routers (no direct route).
        # Only trigger repair when P1 is ALSO broken.
        if p1_ok:
            return

        # Both P1 and P2 are broken -- check cooldown before repair
        broken = self.channel_state.get_broken_channels(peer_id)
        if not broken:
            return

        # Determine repair action
        action = "restart_nats" if CHANNEL_P1_NATS in broken else "check_connectivity"
        cooldown_key = f"{peer_id}:{action}"
        now = time.time()
        last_attempt = self._heal_cooldowns.get(cooldown_key, 0)
        if now - last_attempt < REPAIR_COOLDOWN_S:
            logger.debug(
                f"Self-heal cooldown for {cooldown_key}: "
                f"{int(now - last_attempt)}s < {REPAIR_COOLDOWN_S}s"
            )
            return

        self._heal_cooldowns[cooldown_key] = now
        logger.info(
            f"Self-heal: {peer_id} has broken channels {broken}. "
            f"Sending repair via best working channel."
        )

        # Send repair request via best working channel
        heal_msg = {
            "id": str(uuid.uuid4()),
            "type": "repair",
            "from": self._nerve.node_id,
            "to": peer_id,
            "payload": {
                "subject": "self-heal-request",
                "body": json.dumps({
                    "reporter": self._nerve.node_id,
                    "broken_channels": broken,
                    "action": action,
                    "level": 3,  # ASSIST -- auto-exec
                }),
            },
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }

        # Try each channel in order (use cascade), skip known-broken for this peer
        for ch_name in CHANNEL_ORDER:
            if self.channel_state.is_known_broken(peer_id, ch_name):
                continue
            # NEVER use P7_git for self-heal — it creates git commits that flood the repo
            sender_map = {
                CHANNEL_P1_NATS: self._nerve._send_p1_nats,
                CHANNEL_P2_HTTP: self._nerve._send_p2_http,
                CHANNEL_P3_CHIEF: self._nerve._send_p3_chief,
                CHANNEL_P4_SEED: self._nerve._send_p4_seed,
                CHANNEL_P5_SSH: self._nerve._send_p5_ssh,
            }
            sender = sender_map.get(ch_name)
            if not sender:
                continue
            try:
                result = await asyncio.wait_for(sender(peer_id, heal_msg), timeout=10)
                if result.get("delivered"):
                    logger.info(f"Self-heal repair sent to {peer_id} via {ch_name}")
                    return
            except Exception as e:
                logger.debug(f"Self-heal repair channel {ch_name} failed for {peer_id}: {e}")
                continue

        logger.warning(f"Self-heal: could not deliver repair to {peer_id} -- all channels failed")

    async def _trigger_heal(self, target: str, working_channel: str, broken: list):
        """Lightweight heal trigger when send_with_fallback falls through."""
        if not broken:
            return
        action = "restart_nats" if CHANNEL_P1_NATS in broken else "check_connectivity"
        cooldown_key = f"{target}:{action}"
        now = time.time()
        if now - self._heal_cooldowns.get(cooldown_key, 0) < REPAIR_COOLDOWN_S:
            return
        self._heal_cooldowns[cooldown_key] = now
        logger.info(f"Cascade heal trigger: {target} reached via {working_channel}, broken: {broken}")
        # Delegate to the existing repair_with_escalation
        asyncio.ensure_future(
            self._nerve.repair_with_escalation(target, broken, action))

    def get_channel_state(self) -> dict:
        """Return full channel state for all peers (for /channels endpoint)."""
        return self.channel_state.get_all()


class FleetNerveNATS:
    """NATS-based fleet communication daemon."""

    def __init__(self, node_id: str = NODE_ID, nats_url: str = NATS_URL):
        self.node_id = node_id
        self.nats_url = nats_url
        self.nc: Optional[nats.NATS] = None
        self.js = None  # JetStream context
        self._loop = None  # Set in serve() for HTTP handler threads
        self._stats = {
            "received": 0, "sent": 0, "broadcasts": 0,
            "errors": 0, "started_at": time.time(),
        }
        self._peers = {}  # node_id -> last heartbeat data
        self._running = True
        # ACK tracking store
        try:
            from .store import FleetNerveStore
            self._store = FleetNerveStore()
        except Exception as e:
            logger.warning(f"Store init failed: {e} -- ACK tracking disabled")
            self._store = None
        self._channel_health = {}  # peer -> {channel: "ok"|"broken", ...}
        self._heal_in_flight = {}  # peer -> asyncio.Task (prevent duplicate heals)
        self._pending_acks = {}  # msg_id -> {sent_at, target, retries, acked}
        self._chains = {}  # chain_id -> {steps: [...], current: 0, status: "running"|"done"|"failed", results: []}
        self._dispatches = {}  # dispatch_id -> {target, prompt, status, sent_at, result}
        self._ack_timeout_s = int(os.environ.get("FLEET_ACK_TIMEOUT_S", "60"))
        self._ack_max_retries = int(os.environ.get("FLEET_ACK_MAX_RETRIES", "3"))
        # P7 Git rate limiting (prevent commit flood)
        self._p7_last_sent = {}       # {target: timestamp}
        self._p7_hourly_window = []   # [timestamp, ...] rolling 1h window
        self._p7_broadcast_last = 0.0
        # Load peer config for SSH/HTTP/WoL channels
        try:
            from .config import load_peers
            self._peers_config, self._chief = load_peers()
        except Exception as e:
            logger.warning(f"Failed to load peer config: {e}")
            self._peers_config, self._chief = {}, None
        # Broadcast scaling: track last git broadcast timestamp for rate limiting
        self._last_git_broadcast_ts = 0.0
        # Dynamic peer discovery (mDNS/DNS-SD + NATS heartbeats)
        self._discovery = None
        try:
            from .discovery import FleetDiscovery
            self._discovery = FleetDiscovery(node_id=self.node_id)
            # Seed discovery with config peers
            if self._peers_config:
                self._discovery.merge_config_peers(self._peers_config)
            self._discovery.start()
        except Exception as e:
            logger.warning(f"Discovery init failed: {e} — config-only mode")
            self._discovery = None
        # Communication protocol (self-healing, channel state tracking)
        self.protocol = CommunicationProtocol(self)

    async def connect(self):
        """Connect to NATS server with auto-reconnect.

        CV-001: Supports token auth via NATS_TOKEN env var or config.
        If set, all connections are authenticated. If not, connects without
        auth (backward-compatible for LAN-only fleets).
        """
        connect_kwargs = dict(
            servers=[self.nats_url],
            name=self.node_id,
            reconnected_cb=self._on_reconnect,
            disconnected_cb=self._on_disconnect,
            error_cb=self._on_error,
            max_reconnect_attempts=-1,  # infinite reconnect
            reconnect_time_wait=2,
        )
        # Token auth: NATS_TOKEN env var or .multifleet/config.json natsToken
        nats_token = os.environ.get("NATS_TOKEN")
        if not nats_token:
            try:
                cfg_path = REPO_ROOT / ".multifleet" / "config.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    nats_token = cfg.get("protocol", {}).get("natsToken")
            except Exception as e:
                logger.debug(f"Failed to read NATS token from config: {e}")
        if nats_token:
            connect_kwargs["token"] = nats_token
            logger.info("NATS auth: using token")
        else:
            logger.warning("NATS auth: no token configured (NATS_TOKEN env or config.protocol.natsToken) -- LAN-only mode")

        self.nc = await nats.connect(**connect_kwargs)
        logger.info(f"Connected to NATS at {self.nats_url} as {self.node_id}")

        # Set up JetStream for persistent messaging
        self.js = self.nc.jetstream()
        try:
            await self.js.find_stream_name_by_subject("fleet.>")
            logger.info("JetStream stream FLEET found")
        except Exception as e:
            logger.debug(f"JetStream stream lookup failed (will create): {e}")
            try:
                await self.js.add_stream(
                    name=STREAM_NAME,
                    subjects=["fleet.>"],
                    retention="limits",
                    max_age=24 * 3600,  # 24h in seconds (NATS Go expects int64)
                    storage="file",
                )
                logger.info("JetStream stream FLEET created (24h retention)")
            except Exception as e:
                logger.warning(f"JetStream setup failed (non-fatal, pub/sub still works): {e}")
                self.js = None

    async def _on_reconnect(self):
        logger.info(f"Reconnected to NATS")

    async def _on_disconnect(self):
        logger.warning(f"Disconnected from NATS -- will auto-reconnect")

    async def _on_error(self, e):
        logger.error(f"NATS error: {e}")
        self._stats["errors"] += 1

    # ── Message Handlers ──

    async def _handle_message(self, msg):
        """Route incoming messages by subject."""
        try:
            data = json.loads(msg.data.decode())
            subject_parts = msg.subject.split(".")
            msg_type = subject_parts[-1] if len(subject_parts) >= 3 else "message"

            # Don't process our own broadcasts or ACKs (handled by _handle_ack)
            if data.get("from") == self.node_id:
                return
            if msg_type == "ack":
                return

            self._stats["received"] += 1
            self._touch_activity()
            sender = data.get("from", "unknown")

            # ── ACK messages: update tracking, do NOT ack back (no infinite loop) ──
            if data.get("type") == "ack":
                ref_id = data.get("ref")
                ack_status = data.get("status", "delivered")
                if ref_id and self._store:
                    self._store.update_ack_status(ref_id, ack_status)
                    logger.info(f"ACK received for {ref_id[:8]}… status={ack_status} from {sender}")
                return

            if msg_type == "health":
                # Request/reply -- respond with our health
                if msg.reply:
                    health = self._build_health()
                    await self.nc.publish(msg.reply, json.dumps(health).encode())
                return

            # Log and inject
            payload = data.get("payload", {})
            subject = payload.get("subject", "no subject")
            body = payload.get("body", "")
            priority = data.get("type", "context")

            logger.info(f"[{priority}] from {sender}: {subject}")

            # Handle repair requests (local-first self-healing)
            # Protocol: execute silently, notify user ONCE, spawn agent per channel.
            # If peer hasn't fixed after 2min, we fix it for them via SSH.
            # NEVER inject repeated repair messages into user session.
            if priority in ("sync", "repair") and subject in ("self-heal-request", "repair"):
                try:
                    heal = json.loads(body) if body.startswith("{") else {"action": body}
                    action = heal.get("action", heal.get("suggested_action", ""))
                    reporter = heal.get("reporter", sender)
                    broken = heal.get("broken_channels", [])

                    # Init tracking dicts
                    if not hasattr(self, '_repair_cooldowns'):
                        self._repair_cooldowns = {}
                    if not hasattr(self, '_repair_notified'):
                        self._repair_notified = {}  # reporter:action -> first_seen_time
                    if not hasattr(self, '_repair_agents_spawned'):
                        self._repair_agents_spawned = {}  # channel -> pid

                    cooldown_key = f"{reporter}:{action}"
                    now = time.time()
                    last_attempt = self._repair_cooldowns.get(cooldown_key, 0)
                    if now - last_attempt < 300:  # 5 min cooldown
                        return  # Silent drop -- no log spam

                    # First time seeing this repair? Execute + notify ONCE
                    first_seen = self._repair_notified.get(cooldown_key, 0)
                    if first_seen == 0:
                        self._repair_notified[cooldown_key] = now
                        logger.info(f"Repair request from {reporter}: action={action}, broken={broken}")
                        self._notify(f"Fleet: {reporter} reports broken {broken}")

                        # Execute local repair action
                        SAFE_ACTIONS = {
                            "restart_nats": self._repair_restart_nats,
                            "restart_tunnel": self._repair_restart_tunnel,
                            "git_pull": self._repair_git_pull,
                            "kill_zombie": self._repair_kill_zombie,
                            "restart_daemon": self._repair_restart_daemon,
                        }
                        handler = SAFE_ACTIONS.get(action)
                        if handler:
                            self._repair_cooldowns[cooldown_key] = now
                            asyncio.create_task(handler(heal))

                        # Spawn 1 repair agent per broken channel immediately
                        claude_bin = self._find_claude()
                        if claude_bin:
                            for ch in broken:
                                if ch not in self._repair_agents_spawned:
                                    pid_file = f"/tmp/fleet-repair-{ch}.pid"
                                    if os.path.exists(pid_file):
                                        try:
                                            old_pid = int(open(pid_file).read().strip())
                                            os.kill(old_pid, 0)  # check alive
                                            continue  # agent already running
                                        except (OSError, ValueError) as e:
                                            logger.debug(f"Stale repair PID for {ch}: {e}")
                                    prompt = (f"Fleet channel {ch} is broken on {self.node_id}. "
                                              f"Diagnose and fix. Check daemon, NATS, SSH tunnel, config. "
                                              f"Report result to fleet when fixed.")
                                    proc = subprocess.Popen(
                                        [claude_bin, "-p", prompt],
                                        stdout=open(f"/tmp/fleet-repair-{ch}.log", "w"),
                                        stderr=subprocess.STDOUT)
                                    self._repair_agents_spawned[ch] = proc.pid
                                    with open(pid_file, "w") as f:
                                        f.write(str(proc.pid))
                                    logger.info(f"Spawned repair agent for {ch} (pid {proc.pid})")

                    elif now - first_seen > 120:
                        # 2 min passed, peer hasn't fixed it -- escalate to L3 (SSH fix)
                        logger.info(f"2min timeout: {reporter} still broken. Escalating to SSH repair.")
                        self._repair_cooldowns[cooldown_key] = now
                        self._repair_notified[cooldown_key] = 0  # reset for next cycle
                        # SSH repair attempt
                        peer = self._resolve_peer(reporter)
                        ip = peer.get("ip")
                        user = peer.get("user", "<user>")
                        if ip:
                            asyncio.create_task(self._ssh_repair_peer(ip, user, action))
                    # else: between first notification and 2min -- silent wait
                except Exception as e:
                    logger.warning(f"Repair handler error: {e}")
                return

            # Inject into session based on type
            if priority == "chain":
                self._handle_chain(data)
            elif priority == "chain_complete":
                # Remote node reports chain step done -- advance the chain
                cid = data.get("_chain_id") or payload.get("chain_id", "")
                ok = payload.get("success", True)
                res = payload.get("result", body)
                self._advance_chain(cid, ok, res)
            elif priority == "alert":
                self._inject_alert(data)
            elif priority == "task":
                self._inject_task(data)
            elif priority == "task_result":
                self._handle_task_result(data)
            else:
                self._inject_context(data)

            # Reply with ack if requested (request/reply pattern)
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps({
                    "status": "delivered", "node": self.node_id
                }).encode())

            # Publish ACK back to sender (fire-and-forget delivery confirmation)
            msg_id = data.get("id")
            if msg_id and sender != self.node_id:
                await self._send_ack(sender, msg_id, "delivered", "P1_nats")

        except Exception as e:
            logger.error(f"Message handler error: {e}")
            self._stats["errors"] += 1

    async def _handle_heartbeat(self, msg):
        """Track peer heartbeats. Auto-adds unknown peers. Clears stale repair/channel state."""
        try:
            data = json.loads(msg.data.decode())
            node = data.get("node", "unknown")
            if node != self.node_id:
                was_unknown = node not in self._peers and node not in self._peers_config
                self._peers[node] = {**data, "_received_at": time.time()}
                # Fresh heartbeat = peer is alive on NATS. Clear any stale repair state.
                if hasattr(self, '_repair_in_progress'):
                    self._repair_in_progress.pop(node, None)
                if hasattr(self, 'protocol') and self.protocol:
                    self.protocol.channel_state.record_success(node, "P1_nats", 0)
                # Auto-add unknown peers to discovery (don't reject)
                if was_unknown:
                    logger.info(f"Auto-discovered new peer via NATS heartbeat: {node}")
                if hasattr(self, '_discovery') and self._discovery:
                    peer_ip = data.get("ip", "")
                    peer_port = data.get("port", 8855)
                    self._discovery.add_nats_peer(node, ip=peer_ip, port=peer_port)
        except Exception as e:
            logger.warning(f"Heartbeat handler error: {e}")

    async def _ssh_repair_peer(self, ip: str, user: str, action: str):
        """L3 escalation: SSH into peer and fix their broken channel after 2min timeout."""
        import subprocess as _sp
        REPAIR_COMMANDS = {
            "restart_nats": "pkill -f fleet_nerve_nats; sleep 2; cd ~/dev/er-simulator-superrepo && nohup python3 tools/fleet_nerve_nats.py serve > /tmp/fleet-nerve.log 2>&1 &",
            "restart_tunnel": "pkill -f 'ssh.*4222'; sleep 1; autossh -M 0 -f -N -L 4222:127.0.0.1:4222 chief 2>/dev/null || true",
            "restart_daemon": "pkill -f fleet_nerve; sleep 2; cd ~/dev/er-simulator-superrepo && nohup python3 tools/fleet_nerve_nats.py serve > /tmp/fleet-nerve.log 2>&1 &",
        }
        cmd = REPAIR_COMMANDS.get(action)
        if not cmd:
            logger.warning(f"SSH repair: unknown action {action}")
            return
        try:
            logger.info(f"SSH repair: {user}@{ip} action={action}")
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _sp.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                     f"{user}@{ip}", cmd],
                    capture_output=True, text=True, timeout=30))
            if result.returncode == 0:
                logger.info(f"SSH repair success: {action} on {ip}")
            else:
                logger.warning(f"SSH repair failed: {action} on {ip} rc={result.returncode}")
        except Exception as e:
            logger.warning(f"SSH repair error: {action} on {ip}: {type(e).__name__}")

    async def _handle_ack(self, msg):
        """Process inbound ACK -- mark pending message as delivered."""
        try:
            data = json.loads(msg.data.decode())
            ref = data.get("ref")
            if ref in self._pending_acks:
                self._pending_acks[ref]["acked"] = True
                logger.info(f"ACK: {ref[:8]} from {data.get('node','?')} via {data.get('channel','?')}")
        except Exception as e:
            logger.warning(f"ACK handler error: {e}")

    async def _send_ack(self, sender: str, msg_id: str, status: str, channel: str):
        """Publish ACK back to the original sender."""
        if not self.nc or not self.nc.is_connected or sender == self.node_id:
            return
        ack = {"ref": msg_id, "status": status, "node": self.node_id, "channel": channel}
        try:
            await self.nc.publish(_sub_ack(sender), json.dumps(ack).encode())
        except Exception as e:
            logger.debug(f"Failed to send ACK to {sender}: {e}")

    # ── Injection (last mile) ──

    def _format_resume_message(self, data: dict) -> str:
        """Format a fleet message for --resume injection. Never includes raw body in logs."""
        msg_type = data.get("type", "context").upper()
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})
        subject = payload.get("subject", "Fleet message")
        body = payload.get("body", "")
        lines = [
            f"[FLEET {msg_type}] from {sender}: {subject}",
            "",
            body,
        ]
        return "\n".join(lines)

    def _get_active_session_full_id(self) -> Optional[str]:
        """Get the full session ID of the most recently active interactive session.

        Prefers sessions detected via PID files (have full IDs).
        Falls back to JSONL filename stems for full UUID.
        Returns None if no active session found.
        """
        best_sid = None
        best_mtime = 0.0

        # Method 1: PID session files -- have full session ID
        sessions_dir = Path.home() / ".claude" / "sessions"
        if sessions_dir.exists():
            for pid_file in sessions_dir.glob("*.json"):
                try:
                    pid = int(pid_file.stem)
                    os.kill(pid, 0)  # alive?
                    data = json.loads(pid_file.read_text())
                    kind = data.get("kind", "")
                    # Prefer interactive sessions over agents
                    if kind == "agent":
                        continue
                    sid = data.get("sessionId", "")
                    if sid:
                        mtime = pid_file.stat().st_mtime
                        if mtime > best_mtime:
                            best_mtime = mtime
                            best_sid = sid
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    logger.debug(f"Session PID file parse error: {e}")

        # Method 2: Recently modified JSONL (full UUID in filename)
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists() and not best_sid:
            cutoff = time.time() - 1800  # 30 min
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    if jsonl.stem.startswith("agent-"):
                        continue
                    try:
                        mtime = jsonl.stat().st_mtime
                        if mtime > cutoff and mtime > best_mtime:
                            best_mtime = mtime
                            best_sid = jsonl.stem  # full UUID
                    except OSError:
                        pass

        return best_sid

    def _inject_via_resume(self, session_id: str, message: str) -> bool:
        """Inject message into active Claude session via `claude --resume`.

        Args:
            session_id: Full session UUID
            message: Pre-formatted message text

        Returns:
            True if injection succeeded, False otherwise.
            Never logs message content -- only type and delivery status.
        """
        try:
            cmd = ["claude", "--resume", session_id, "--message", message]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"resume-inject: success session={session_id[:12]}")
                return True
            logger.warning(f"resume-inject: failed rc={result.returncode} session={session_id[:12]}")
            return False
        except Exception as e:
            logger.warning(f"resume-inject: error session={session_id[:12]}: {e}")
            return False

    def _resolve_seed_path(self, data: dict) -> str:
        """Resolve seed file path - per-session when possible, per-node fallback.

        Priority:
        1. Explicit _target_session in message -> /tmp/fleet-seed-<session_id>.md
        2. Primary active session (most recently active) -> /tmp/fleet-seed-<session_id>.md
        3. Fallback -> /tmp/fleet-seed-<node_id>.md
        """
        # Check explicit target
        target_session = data.get("_target_session")
        if target_session:
            safe_sid = "".join(c for c in target_session if c.isalnum() or c in "-_")
            if safe_sid:
                return os.path.join(SEED_DIR, f"fleet-seed-{safe_sid}.md")

        # Try to detect primary session from daemon health endpoint
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=2)
            health = json.loads(resp.read())
            sessions = health.get("sessions", [])
            if sessions:
                # Pick most recently active (lowest idle)
                best = min(sessions, key=lambda s: s.get("idle_s", s.get("lastModified", 0) or 9999))
                sid = best.get("sessionId", "")
                safe_sid = "".join(c for c in sid if c.isalnum() or c in "-_") if sid else ""
                if safe_sid:
                    return os.path.join(SEED_DIR, f"fleet-seed-{safe_sid}.md")
        except Exception:
            pass

        # Fallback: per-node
        return os.path.join(SEED_DIR, f"fleet-seed-{self.node_id}.md")

    def _inject_context(self, data: dict):
        """Write to seed file AND auto-trigger Claude session to read it.

        INJECTION IS P8 — LAST RESORT. Only inject for alert/critical types.
        Context/info messages are logged silently — the session reads them
        via the prompt-submit hook when the user naturally types next.

        Idle gating: if _idle_gated is set, skip keystroke trigger (seed only).
        """
        msg_type = data.get("type", "context")
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})

        # P8 injection only for urgent types — everything else is seed-only (passive)
        is_urgent = msg_type in ("alert", "sync_hold") or payload.get("urgency") == "critical"
        if not is_urgent:
            # Write seed file silently — hook reads it on next user prompt
            self._inject_seed(data)
            logger.info(f"Fleet msg from {sender} ({msg_type}) — seed only, no injection")
            return

        # Try --resume first for context and reply types
        if msg_type in ("context", "reply"):
            session_id = self._get_active_session_full_id()
            if session_id:
                message = self._format_resume_message(data)
                if self._inject_via_resume(session_id, message):
                    self._notify(f"Fleet: {payload.get('subject', 'message')} from {sender}")
                    return

        # Fallback: seed file + keystroke trigger
        self._inject_seed(data)
        self._notify(f"Fleet: {payload.get('subject', 'message')} from {sender}")
        if not data.get("_idle_gated"):
            self._keystroke_trigger(f"fleet message from {sender}: {payload.get('subject', '')}")
        else:
            logger.info(f"Idle-gated: skipping keystroke trigger for {sender} message")

    def _inject_seed(self, data: dict):
        """Write message to seed file (fallback injection method)."""
        sender = data.get("from", "unknown")
        payload = data.get("payload", {})
        seed_path = self._resolve_seed_path(data)
        with open(seed_path, "a") as f:
            f.write(f"\n## [{data.get('type', 'context').upper()}] from {sender}: {payload.get('subject', '')}\n\n")
            f.write(f"{payload.get('body', '')}\n\n---\n")
        logger.info(f"seed-inject: written to {seed_path}")

    _last_keystroke = 0.0  # Rate limit: max 1 keystroke per 30s

    def _keystroke_trigger(self, summary: str):
        """Focus VS Code and type a trigger prompt so the hook fires automatically.

        Uses osascript to simulate user input. Only fires if VS Code is running
        and no Claude agent is mid-response (checks for idle session).
        Rate-limited: max 1 keystroke per 30s to prevent spam when multiple messages arrive.
        """
        # Rate limit -- don't spam keystrokes
        now = time.time()
        if now - FleetNerveNATS._last_keystroke < 30:
            logger.debug(f"Keystroke throttled ({int(30 - (now - FleetNerveNATS._last_keystroke))}s remaining)")
            return
        FleetNerveNATS._last_keystroke = now
        try:
            # Check if VS Code is running
            check = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to (name of processes) contains "Code"'],
                capture_output=True, text=True, timeout=3)
            if "true" not in check.stdout.lower():
                logger.debug("VS Code not running -- skipping keystroke trigger")
                return

            # Trigger: focus VS Code, type the fleet check command, press Enter
            # The fleet-inbox-hook.sh will fire on UserPromptSubmit and read the seed file
            trigger_cmd = f"bash {REPO_ROOT}/scripts/fleet-check.sh"
            subprocess.run(
                ["osascript", "-e", f'''
                    tell application "Visual Studio Code 3" to activate
                    delay 0.5
                    tell application "System Events"
                        keystroke "{trigger_cmd}"
                        delay 0.2
                        keystroke return
                    end tell
                '''],
                capture_output=True, timeout=5)
            logger.info(f"Keystroke trigger sent to VS Code: {summary[:60]}")
        except Exception as e:
            logger.debug(f"Keystroke trigger failed: {e}")

    def _inject_alert(self, data: dict):
        """Alert: --resume (preferred) > seed file + auto-trigger + focus VS Code + notification."""
        self._inject_context(data)  # handles --resume with seed fallback
        # Additional focus for alerts (in case --resume didn't bring VS Code forward)
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to if exists (process "Code") then '
                 'tell application "Visual Studio Code 3" to activate'],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    def _inject_task(self, data: dict):
        """Task: spawn session-driver worker (invisible to user) or fallback to claude -p."""
        payload = data.get("payload", {})
        sender = data.get("from", "unknown")
        task_id = data.get("id", str(uuid.uuid4()))[:8]
        chain_id = data.get("_chain_id")
        dispatch_id = payload.get("dispatch_id", task_id)
        timeout_s = int(payload.get("timeout_s", 300))
        prompt = f"Fleet task from {sender}: {payload.get('subject', '')}. {payload.get('body', '')}"

        def _send_result(response: str, status: str = "completed"):
            """Send task_result back to the dispatching node."""
            if self._loop and sender != self.node_id:
                result_msg = {
                    "type": "task_result", "from": self.node_id, "to": sender,
                    "payload": {"dispatch_id": dispatch_id, "status": status, "response": response[:2000]},
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                }
                asyncio.run_coroutine_threadsafe(
                    self.send_with_fallback(sender, result_msg), self._loop)

        # Prefer session-driver worker (runs in tmux, never disrupts user)
        try:
            from .worker import FleetWorkerManager
            mgr = FleetWorkerManager()
            worker_name = f"fleet-task-{task_id}"
            worker = mgr.launch_worker(self.node_id, worker_name)
            if "error" not in worker:
                # Send task in background thread (don't block message handler)
                def _run_task():
                    try:
                        response = mgr.send_task(worker_name, prompt, timeout=timeout_s)
                        mgr.stop_worker(worker_name)
                        logger.info(f"Worker {worker_name} completed: {response[:100]}...")
                        # Chain advancement: report completion
                        if chain_id:
                            self._chain_step_done(chain_id, sender, True, response[:2000])
                        _send_result(response, "completed")
                    except Exception as e:
                        logger.warning(f"Worker {worker_name} failed: {e}")
                        if chain_id:
                            self._chain_step_done(chain_id, sender, False, str(e))
                        _send_result(str(e), "failed")
                from threading import Thread
                Thread(target=_run_task, daemon=True).start()
                logger.info(f"Launched worker {worker_name} for task from {sender}")
                return
        except Exception as e:
            logger.debug(f"Session-driver unavailable ({e}), falling back to claude -p")

        # Fallback: raw claude -p
        claude = self._find_claude()
        if claude:
            log_path = f"/tmp/fleet-task-{task_id}.log"
            subprocess.Popen(
                [claude, "-p", prompt, "--output-format", "json", "--max-turns", "20"],
                stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
            )
            logger.info(f"Spawned task agent {task_id} (claude -p fallback)")
        else:
            self._inject_context(data)  # fallback to seed file

    # ── Chain Orchestration ──

    def _handle_chain(self, data: dict):
        """Register a chain and dispatch its first step."""
        chain_id = data.get("chain_id") or str(uuid.uuid4())
        steps = data.get("steps", data.get("payload", {}).get("steps", []))
        if not steps:
            logger.warning(f"Chain {chain_id[:8]}: no steps provided")
            return
        self._chains[chain_id] = {
            "steps": steps,
            "current": 0,
            "status": "running",
            "results": [],
            "created_at": time.time(),
        }
        logger.info(f"Chain {chain_id[:8]}: registered {len(steps)} steps")
        self._dispatch_chain_step(chain_id)

    def _dispatch_chain_step(self, chain_id: str):
        """Dispatch the current step of a chain as a task message."""
        chain = self._chains.get(chain_id)
        if not chain or chain["status"] != "running":
            return
        idx = chain["current"]
        if idx >= len(chain["steps"]):
            chain["status"] = "done"
            logger.info(f"Chain {chain_id[:8]}: complete ({len(chain['steps'])} steps)")
            return
        step = chain["steps"][idx]
        target = step.get("node", self.node_id)
        task_desc = step.get("task", "chain step")
        timeout_s = step.get("timeout_s", 300)
        # Build a task message with chain metadata for tracking
        task_msg = {
            "id": str(uuid.uuid4()),
            "type": "task",
            "from": self.node_id,
            "to": target,
            "payload": {"subject": f"chain:{chain_id[:8]} step {idx}", "body": task_desc},
            "_chain_id": chain_id,
            "_chain_step": idx,
            "_chain_timeout_s": timeout_s,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }
        if target == self.node_id:
            # Local: inject directly
            self._inject_task(task_msg)
        elif self._loop:
            # Remote: send via NATS
            asyncio.run_coroutine_threadsafe(
                self.send(target, "task", f"chain:{chain_id[:8]} step {idx}", task_desc),
                self._loop,
            )
        logger.info(f"Chain {chain_id[:8]}: dispatched step {idx} -> {target}")

    def _advance_chain(self, chain_id: str, success: bool, result: str = ""):
        """Advance a chain after step completion (or fail it)."""
        chain = self._chains.get(chain_id)
        if not chain or chain["status"] != "running":
            return
        chain["results"].append({"step": chain["current"], "success": success, "result": result[:2000]})
        if not success:
            chain["status"] = "failed"
            logger.warning(f"Chain {chain_id[:8]}: failed at step {chain['current']}")
            return
        chain["current"] += 1
        if chain["current"] >= len(chain["steps"]):
            chain["status"] = "done"
            logger.info(f"Chain {chain_id[:8]}: all {len(chain['steps'])} steps complete")
        else:
            self._dispatch_chain_step(chain_id)

    def _get_chain_status(self, chain_id: str) -> dict:
        """Return status dict for a chain."""
        chain = self._chains.get(chain_id)
        if not chain:
            return {"error": "chain not found", "chain_id": chain_id}
        return {
            "chain_id": chain_id,
            "status": chain["status"],
            "current": chain["current"],
            "total": len(chain["steps"]),
            "steps": chain["steps"],
            "results": chain["results"],
            "created_at": chain.get("created_at"),
        }

    def _chain_step_done(self, chain_id: str, origin: str, success: bool, result: str):
        """Handle chain step completion -- local advance or notify remote originator."""
        if chain_id in self._chains:
            # We own this chain -- advance locally
            self._advance_chain(chain_id, success, result)
        elif self._loop and origin != self.node_id:
            # Remote chain -- send completion back to originator
            asyncio.run_coroutine_threadsafe(
                self.send(origin, "chain_complete", f"chain:{chain_id[:8]} done",
                          json.dumps({"chain_id": chain_id, "success": success, "result": result[:2000]})),
                self._loop,
            )

    # ── Remote Worker Dispatch ──

    def _handle_task_result(self, data: dict):
        """Handle incoming task_result -- update dispatch tracking."""
        payload = data.get("payload", {})
        dispatch_id = payload.get("dispatch_id")
        if dispatch_id and dispatch_id in self._dispatches:
            self._dispatches[dispatch_id]["status"] = payload.get("status", "completed")
            self._dispatches[dispatch_id]["result"] = payload.get("response", "")
            self._dispatches[dispatch_id]["completed_at"] = time.time()
            logger.info(f"Dispatch {dispatch_id[:8]} result: {payload.get('status')}")
        else:
            logger.info(f"Task result for unknown dispatch {dispatch_id} -- injecting as context")
            self._inject_context(data)

    def _notify(self, message: str):
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "Fleet Nerve" sound name "Glass"'],
                capture_output=True, timeout=3,
            )
        except Exception as e:
            logger.debug(f"macOS notification failed: {e}")

    def _find_claude(self) -> Optional[str]:
        for path in ["/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
            if os.path.exists(path):
                return path
        return None

    # ── Health ──

    def _build_health(self) -> dict:
        """Build health response with VS Code state and session idle times."""
        git = self._get_git_context()
        sessions = self._detect_sessions()
        return {
            "nodeId": self.node_id,
            "status": "ok",
            "transport": "nats",
            "natsUrl": self.nats_url,
            "vscode": self._detect_vscode_state(),
            "uptime_s": int(time.time() - self._stats["started_at"]),
            "activeSessions": len(sessions),
            "sessions": sessions[:5],
            "peers": {
                k: {
                    "lastSeen": int(time.time() - v["_received_at"]),
                    "sessions": v.get("sessions", 0),
                    "transport": "nats",
                    "vscode": v.get("vscode", "?"),
                }
                for k, v in self._peers.items()
            },
            "git": git,
            "stats": self._stats,
            "surgeons": self._probe_surgeons(),
            "channel_health": {
                peer: {
                    "score": f"{score}/{len(ALL_CHANNELS)}",
                    "broken": broken,
                }
                for peer in self._channel_health
                for score, broken in [self._get_peer_health_score(peer)]
            },
        }

    def _probe_surgeons(self) -> dict:
        """Quick probe of 3-surgeon backends for health display."""
        result = {}
        # Neurologist (local MLX/LM Studio)
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:5044/v1/models", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            result["neurologist"] = "ok"
        except Exception as e:
            logger.debug(f"Neurologist probe failed: {e}")
            result["neurologist"] = "down"
        # Cardiologist (OpenAI) -- check key availability (never log the key)
        # Priority: env var -> macOS Keychain -> AWS Secrets Manager
        key = ""
        key_source = "none"
        if os.environ.get("OPENAI_API_KEY", ""):
            key = os.environ["OPENAI_API_KEY"]
            key_source = "env"
        if not key:
            # Try macOS Keychain (most secure, no network call)
            try:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", "OPENAI_API_KEY", "-w"],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    key = r.stdout.strip()
                    key_source = "keychain"
            except Exception as e:
                logger.debug(f"Keychain lookup for OPENAI_API_KEY failed: {e}")
        if not key:
            # Try AWS Secrets Manager (fallback -- requires network + credentials)
            try:
                r = subprocess.run(
                    ["aws", "secretsmanager", "get-secret-value",
                     "--secret-id", "/ersim/prod/backend/OPENAI_API_KEY",
                     "--query", "SecretString", "--output", "text"],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    key = r.stdout.strip()
                    key_source = "aws"
            except Exception as e:
                logger.debug(f"AWS Secrets Manager lookup for OPENAI_API_KEY failed: {e}")
        has_key = bool(key and key.startswith("sk-"))
        result["cardiologist"] = "ok" if has_key else "no-key"
        result["cardiologist_key_source"] = key_source
        # Flag fallback usage -- recommend fleet collaboration for best practice
        if key_source in ("aws", "none"):
            result["cardiologist_recommendation"] = (
                "Key loaded from fallback. Recommend: security add-generic-password "
                "-s OPENAI_API_KEY -a $USER -w <key> to store in Keychain (most secure)."
            )
        return result

    def _get_git_context(self) -> dict:
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            commit = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            return {"branch": branch, "commit": commit}
        except Exception as e:
            logger.debug(f"Git context detection failed: {e}")
            return {"branch": "unknown", "commit": "unknown"}

    def _detect_vscode_state(self) -> str:
        """Returns: 'active' (frontmost), 'open' (running), 'closed'."""
        try:
            result = subprocess.run(["pgrep", "-f", "Visual Studio Code"], capture_output=True, timeout=2)
            if result.returncode != 0:
                return "closed"
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first application process whose frontmost is true'],
                capture_output=True, text=True, timeout=2)
            if "Code" in result.stdout:
                return "active"
            return "open"
        except Exception as e:
            logger.debug(f"VS Code state detection failed: {e}")
            return "unknown"

    def _detect_sessions(self) -> list:
        sessions = []
        # Method 1: PID files (most reliable)
        sessions_dir = Path.home() / ".claude" / "sessions"
        if sessions_dir.exists():
            for pid_file in sessions_dir.glob("*.json"):
                try:
                    pid = int(pid_file.stem)
                    os.kill(pid, 0)
                    data = json.loads(pid_file.read_text())
                    sessions.append({
                        "sessionId": data.get("sessionId", pid_file.stem)[:12],
                        "pid": pid,
                        "kind": data.get("kind", "?"),
                        "entrypoint": data.get("entrypoint", "?"),
                    })
                except (OSError, ValueError, json.JSONDecodeError) as e:
                    logger.debug(f"Session PID file parse error: {e}")
        # Method 2: JSONL mtime for idle_s
        projects_dir = Path.home() / ".claude" / "projects"
        if projects_dir.exists():
            cutoff = time.time() - 1800
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    if jsonl.stem.startswith("agent-"):
                        continue
                    try:
                        mtime = jsonl.stat().st_mtime
                        if mtime > cutoff:
                            sid = jsonl.stem[:12]
                            # Update idle_s on existing session or add new one
                            existing = next((s for s in sessions if s["sessionId"] == sid), None)
                            if existing:
                                existing["idle_s"] = int(time.time() - mtime)
                                existing["project"] = proj_dir.name
                            else:
                                sessions.append({
                                    "sessionId": sid,
                                    "project": proj_dir.name,
                                    "idle_s": int(time.time() - mtime),
                                    "kind": "interactive",
                                })
                    except OSError as e:
                        logger.debug(f"JSONL session stat error: {e}")
        return sessions

    # ── Publish ──

    async def send(self, to: str, msg_type: str, subject: str, body: str):
        """Send a message to a specific node or all."""
        msg = {
            "id": str(uuid.uuid4()),
            "type": msg_type,
            "from": self.node_id,
            "to": to,
            "payload": {"subject": subject, "body": body},
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }
        target = f"fleet.{to}.message" if to != "all" else "fleet.all.message"
        await self.nc.publish(target, json.dumps(msg).encode())
        self._stats["sent"] += 1
        self._touch_activity()
        if to == "all":
            self._stats["broadcasts"] += 1
        logger.info(f"Sent [{msg_type}] to {to}: {subject}")

    # ── 7-Priority Fallback Protocol ──
    # P1 NATS -> P2 HTTP -> P3 Chief -> P4 Seed -> P5 SSH -> P6 WoL -> P7 Git
    # Designed for 100+ nodes. Peers resolved from NATS heartbeats + config.

    def _resolve_peer(self, target: str) -> dict:
        """Resolve peer connection info from heartbeats (dynamic) or config (static)."""
        # Dynamic: from NATS heartbeat cache (scales to 100+)
        hb = self._peers.get(target, {})
        if hb:
            return {"ip": hb.get("ip", ""), "user": hb.get("user", ""), "mac_address": hb.get("mac", "")}
        # Static: from config file (bootstrap / fallback)
        return self._peers_config.get(target, {})

    # ── Local-first repair actions (Protocol: fix yourself before others SSH in) ──

    async def _subscribe_all(self):
        """Subscribe to all fleet NATS subjects. Called on connect and reconnect."""
        await self.nc.subscribe(_sub_direct(self.node_id), cb=self._handle_message)
        await self.nc.subscribe(_sub_all(), cb=self._handle_message)
        await self.nc.subscribe(_sub_heartbeat(), cb=self._handle_heartbeat)
        await self.nc.subscribe(_sub_ack(self.node_id), cb=self._handle_ack)
        # Session historian subscription (fleet.sessions.> wildcard)
        if fleet_historian_nats:
            sub, cb = fleet_historian_nats.subscribe_sessions(self)
            await self.nc.subscribe(sub, cb=cb)
            logger.info(
                f"NATS subscribed: fleet.{self.node_id}.> + fleet.all.> + "
                f"fleet.heartbeat + ack + fleet.sessions.>"
            )
        else:
            logger.info(f"NATS subscribed: fleet.{self.node_id}.> + fleet.all.> + fleet.heartbeat + ack")

    async def _repair_restart_nats(self, heal: dict):
        """Reconnect to NATS server without killing the daemon."""
        logger.info("Repair: reconnecting to NATS...")
        if self.nc:
            try:
                await self.nc.close()
            except Exception as e:
                logger.debug(f"NATS close during repair failed: {e}")
        await asyncio.sleep(1)
        await self.connect()
        await self._subscribe_all()
        logger.info("Repair: NATS reconnected + resubscribed")

    async def _repair_restart_tunnel(self, heal: dict):
        """Restart the autossh tunnel via launchctl."""
        logger.info("Repair: restarting SSH tunnel...")
        subprocess.run(["launchctl", "kickstart", "-k",
                        f"gui/{os.getuid()}/io.contextdna.fleet-tunnel"],
                       capture_output=True, timeout=10)
        logger.info("Repair: tunnel restart requested")

    async def _repair_git_pull(self, heal: dict):
        """Pull latest code from origin."""
        logger.info("Repair: git pull...")
        result = subprocess.run(["git", "pull", "origin", "main"],
                                cwd=str(REPO_ROOT), capture_output=True, timeout=30)
        logger.info(f"Repair: git pull {'OK' if result.returncode == 0 else 'FAILED'}")

    async def _repair_kill_zombie(self, heal: dict):
        """Kill a specific zombie/rogue process by PID."""
        pid = heal.get("pid")
        if not pid:
            logger.warning("Repair: kill_zombie requires 'pid' in heal payload")
            return
        try:
            os.kill(int(pid), 9)
            logger.info(f"Repair: killed PID {pid}")
        except (ProcessLookupError, PermissionError) as e:
            logger.warning(f"Repair: kill PID {pid} failed: {e}")

    async def _repair_restart_daemon(self, heal: dict):
        """Restart this daemon process (exec replaces current process)."""
        logger.info("Repair: daemon restart requested -- re-execing in 2s...")
        await asyncio.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def _send_p1_nats(self, target: str, message: dict) -> dict:
        """P1: NATS pub/sub -- fastest, scales to 100+ nodes."""
        if not self.nc or not self.nc.is_connected:
            return {"delivered": False}
        subject = f"fleet.{target}.message" if target != "all" else "fleet.all.message"
        await self.nc.publish(subject, json.dumps(message).encode())
        self._stats["sent"] += 1
        return {"delivered": True}

    async def _send_p2_http(self, target: str, message: dict) -> dict:
        """P2: HTTP direct to target daemon. Tries direct IP, then tunnel port."""
        peer = self._resolve_peer(target)
        ip = peer.get("ip")
        tunnel_port = peer.get("tunnel_port")
        if not ip:
            return {"delivered": False}
        import urllib.request
        payload = json.dumps(message).encode()
        # Try direct first, then tunnel
        urls = [f"http://{ip}:8855/message"]
        if tunnel_port:
            urls.append(f"http://127.0.0.1:{tunnel_port}/message")
        for url in urls:
            try:
                req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                resp = urllib.request.urlopen(req, timeout=3)
                return {"delivered": True, "url": url}
            except Exception as e:
                logger.debug(f"P2 HTTP send to {url} failed: {e}")
                continue
        return {"delivered": False}

    async def _send_p3_chief(self, target: str, message: dict) -> dict:
        """P3: Chief relay -- stores and forwards. Tries direct, then tunnel."""
        if not self._chief:
            return {"delivered": False}
        chief = self._resolve_peer(self._chief)
        chief_ip = chief.get("ip")
        if not chief_ip or self.node_id == self._chief:
            return {"delivered": False}
        import urllib.request
        payload = json.dumps({"from": self.node_id, "to": [target],
                              "subject": message.get("payload", {}).get("subject", ""),
                              "body": message.get("payload", {}).get("body", "")}).encode()
        # Try direct chief, then via tunnel (localhost:8844 if tunneled)
        urls = [f"http://{chief_ip}:8844/message", "http://127.0.0.1:8844/message"]
        for url in urls:
            try:
                req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3)
                return {"delivered": True, "url": url}
            except Exception as e:
                logger.debug(f"P3 chief relay to {url} failed: {e}")
                continue
        return {"delivered": False}

    async def _send_p4_seed(self, target: str, message: dict) -> dict:
        """P4: Seed file -- local (session-aware) or via SSH.

        Local: resolves to per-session seed file when sessions are detected.
        Remote: falls through to P5 SSH logic.
        """
        payload = message.get("payload", {})
        text = f"# Fleet: {payload.get('subject','')}\nFrom: {message.get('from','?')}\n\n{payload.get('body','')}\n"
        if target == self.node_id:
            seed_path = self._resolve_seed_path(message)
            Path(seed_path).write_text(text)
            return {"delivered": True, "seedPath": seed_path}
        # Remote via SSH (falls through to P5 logic)
        return {"delivered": False}

    async def _send_p5_ssh(self, target: str, message: dict) -> dict:
        """P5: SSH -- curl the target's daemon. Tries direct IP, then tunnel.

        CV-001 fixes:
        - Removed StrictHostKeyChecking=no (use known_hosts)
        - Payload sent via stdin (not shell interpolation) to prevent injection
        - Seed file write uses base64 to avoid shell metachar injection
        """
        peer = self._resolve_peer(target)
        user, ip = peer.get("user"), peer.get("ip")
        tunnel_port = peer.get("tunnel_port")
        if not user or not ip:
            return {"delivered": False}
        # Validate target is alphanumeric (prevent path traversal)
        if not target.replace("-", "").replace("_", "").isalnum():
            logger.warning(f"P5 SSH: rejected invalid target name: {target}")
            return {"delivered": False}
        payload_bytes = json.dumps(message).encode()
        # Try SSH to direct IP first -- pipe payload via stdin to avoid shell injection
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3",
                 f"{user}@{ip}",
                 "curl -sf 127.0.0.1:8855/message -H 'Content-Type: application/json' -d @-"],
                input=payload_bytes, capture_output=True, timeout=8)
            if result.returncode == 0:
                return {"delivered": True, "method": "ssh_direct"}
        except Exception as e:
            logger.debug(f"P5 SSH direct to {ip} failed: {e}")
        # Try via tunnel port (HTTP through SSH tunnel)
        if tunnel_port:
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"http://127.0.0.1:{tunnel_port}/message",
                    data=payload_bytes,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3)
                return {"delivered": True, "method": "ssh_tunnel"}
            except Exception as e:
                logger.debug(f"P5 SSH tunnel via port {tunnel_port} failed: {e}")
        # Fallback: write seed file via SSH using base64 to prevent injection
        # Session-aware: use _target_session if set, otherwise per-node fallback
        try:
            import base64
            payload = message.get("payload", {})
            seed_text = f"Fleet: {payload.get('subject','')}\n{payload.get('body','')}"
            b64 = base64.b64encode(seed_text.encode()).decode()
            # Determine seed file name -- per-session if targeted, per-node otherwise
            target_session = message.get("_target_session")
            if target_session:
                safe_sid = "".join(c for c in target_session if c.isalnum() or c in "-_")
                seed_name = f"fleet-seed-{safe_sid}.md" if safe_sid else f"fleet-seed-{target}.md"
            else:
                seed_name = f"fleet-seed-{target}.md"
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                 f"echo '{b64}' | base64 -d > /tmp/{seed_name}"],
                capture_output=True, timeout=8)
            return {"delivered": True, "method": "seed_via_ssh", "seedFile": seed_name}
        except Exception as e:
            logger.debug(f"P5 SSH seed file to {ip} failed: {e}")
            return {"delivered": False}

    async def _send_p6_wol(self, target: str, message: dict) -> dict:
        """P6: Wake-on-LAN -- wake sleeping machine."""
        peer = self._resolve_peer(target)
        mac_addr = peer.get("mac_address")
        if not mac_addr:
            return {"delivered": False}
        try:
            mac_bytes = bytes.fromhex(mac_addr.replace(":", ""))
            magic = b'\xff' * 6 + mac_bytes * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ('<broadcast>', 9))
            sock.close()
            logger.info(f"WoL sent to {target} ({mac_addr})")
            return {"delivered": False, "wol_sent": True}
        except Exception as e:
            logger.warning(f"P6 WoL failed for {target}: {e}")
            return {"delivered": False}

    # ── P7 Git rate limiting constants ──
    P7_PER_TARGET_COOLDOWN_S = 600   # 1 message per target per 10 min
    P7_HOURLY_MAX = 6                # max 6 commits/hour globally
    P7_BROADCAST_COOLDOWN_S = 3600   # max 1 broadcast/hour

    def _p7_rate_check(self, target: str) -> Optional[str]:
        """Check P7 rate limits. Returns reason string if limited, None if OK."""
        now = time.time()

        # Per-target cooldown
        last = self._p7_last_sent.get(target, 0)
        if now - last < self.P7_PER_TARGET_COOLDOWN_S:
            return f"per-target cooldown ({int(self.P7_PER_TARGET_COOLDOWN_S - (now - last))}s remaining)"

        # Broadcast cooldown
        if target == "all" and now - self._p7_broadcast_last < self.P7_BROADCAST_COOLDOWN_S:
            return f"broadcast cooldown ({int(self.P7_BROADCAST_COOLDOWN_S - (now - self._p7_broadcast_last))}s remaining)"

        # Global hourly cap — prune window first
        cutoff = now - 3600
        self._p7_hourly_window = [t for t in self._p7_hourly_window if t > cutoff]
        if len(self._p7_hourly_window) >= self.P7_HOURLY_MAX:
            return f"global hourly cap ({self.P7_HOURLY_MAX} commits/hr reached)"

        return None

    def _p7_record_sent(self, target: str):
        """Record a successful P7 send for rate tracking."""
        now = time.time()
        self._p7_last_sent[target] = now
        self._p7_hourly_window.append(now)
        if target == "all":
            self._p7_broadcast_last = now

    def _p7_queue_for_retry(self, target: str, message: dict, reason: str):
        """Queue rate-limited P7 message in SQLite outbox for later delivery."""
        logger.warning(f"P7 rate limited for {target} — queued for retry ({reason})")
        if self._store:
            # Ensure message is stored first
            existing = self._store.get_ack_status(message.get("id", ""))
            if not existing:
                self._store.store_message(message)
            self._store.queue_outbox(message["id"], target)

    async def _send_p7_git(self, target: str, message: dict) -> dict:
        """P7: Git push -- guaranteed delivery, works at any scale.

        Rate limited to prevent commit flood:
        - Max 1 commit per target per 10 minutes
        - Max 6 commits per hour globally (all targets)
        - Max 1 broadcast per hour
        Excess messages queued in SQLite outbox for retry via other channels.
        """
        # Check rate limits
        limit_reason = self._p7_rate_check(target)
        if limit_reason:
            self._p7_queue_for_retry(target, message, limit_reason)
            return {"delivered": False, "rate_limited": True, "reason": limit_reason}

        try:
            inbox_dir = REPO_ROOT / "fleet-inbox" / target
            inbox_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            msg_file = inbox_dir / f"{ts}-{message.get('id','msg')[:8]}.json"
            msg_file.write_text(json.dumps(message, indent=2))
            subprocess.run(["git", "add", str(msg_file)], cwd=str(REPO_ROOT), timeout=5)
            subprocess.run(["git", "commit", "-m", f"fleet: P7 message to {target}"],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=10)
            subprocess.run(["git", "push", "origin", "main"],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=15)
            self._p7_record_sent(target)
            return {"delivered": True}
        except Exception as e:
            logger.warning(f"P7 git send failed for {target}: {e}")
            return {"delivered": False}

    async def send_with_fallback(self, target: str, message: dict, use_cloud: bool = False) -> dict:
        """9-priority fallback: P0 Cloud -> P1 NATS -> P2 HTTP -> P3 Chief -> P4 Seed -> P5 SSH -> P6 WoL -> P7 Git -> P8 Keystroke.

        For broadcast (target="all" or message.to="all"):
        - NATS fleet.all.message is PRIMARY (O(1) pub/sub, scales to 100+)
        - HTTP loop is FALLBACK only if NATS is down, runs async parallel with semaphore

        Delegates to CommunicationProtocol.send() which enforces:
        - 30s timeout per channel (invariant)
        - Channel state tracking (skip known-broken, auto-retry via self-heal)
        - P8 keystroke as absolute last resort (rate-limited 1/30s)
        - Secrets protection (never logs message content)
        """
        # Ensure message has an ID
        if "id" not in message:
            message["id"] = str(uuid.uuid4())
        if "from" not in message:
            message["from"] = self.node_id
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

        # Store message and mark awaiting ACK (skip for ack messages and heartbeats)
        if self._store and message.get("type") not in ("ack", "heartbeat"):
            existing = self._store.get_ack_status(message["id"])
            if not existing:
                self._store.store_message(message)
                self._store.update_ack_status(message["id"], "awaiting")

        # ── Broadcast path: NATS primary, HTTP parallel fallback ──
        is_broadcast = target == "all" or message.get("to") == "all"
        if is_broadcast:
            return await self._send_broadcast(message, use_cloud=use_cloud)

        # ── Unicast path: sequential priority fallback ──
        channels = []
        if use_cloud:
            channels.append(("P0_cloud", self._send_p0_cloud))
        channels.extend([
            ("P1_nats", self._send_p1_nats),
            ("P2_http", self._send_p2_http),
            ("P3_chief", self._send_p3_chief),
            ("P4_seed", self._send_p4_seed),
            ("P5_ssh", self._send_p5_ssh),
            ("P6_wol", self._send_p6_wol),
        ])
        # P7_git creates commits — NEVER use for repair/self-heal (causes flood)
        msg_type = message.get("type", "")
        is_repair = msg_type == "repair" or "self-heal" in str(message.get("payload", {}).get("subject", ""))
        if not is_repair:
            channels.append(("P7_git", self._send_p7_git))

        # Per-channel timeout budget (total 30s max across all channels)
        CHANNEL_TIMEOUTS = {
            "P0_cloud": 5, "P1_nats": 2, "P2_http": 3, "P3_chief": 3,
            "P4_seed": 2, "P5_ssh": 8, "P6_wol": 3, "P7_git": 10,
        }
        TOTAL_BUDGET = 30.0
        budget_start = time.time()

        errors = []
        for name, sender in channels:
            # Check total budget
            elapsed = time.time() - budget_start
            if elapsed >= TOTAL_BUDGET:
                errors.append(f"{name}: skipped (30s budget exhausted)")
                logger.info(f"[{name}] Skipped -- 30s budget exhausted after {elapsed:.0f}s")
                continue

            try:
                channel_timeout = CHANNEL_TIMEOUTS.get(name, 5)
                result = await asyncio.wait_for(sender(target, message), timeout=channel_timeout)
                if result.get("delivered"):
                    logger.info(f"[{name}] Delivered to {target} ({time.time()-budget_start:.1f}s)")
                    self._update_channel_health(target, name, "ok")
                    # Track for ACK confirmation
                    msg_id = message.get("id")
                    if msg_id and target != "all":
                        existing = self._pending_acks.get(msg_id)
                        retries = existing["retries"] if existing else 0
                        self._pending_acks[msg_id] = {
                            "sent_at": time.time(), "target": target,
                            "retries": retries, "acked": False, "message": message,
                        }
                    # Self-heal: if we succeeded on P3+, earlier channels are broken
                    if name not in ("P1_nats", "P2_http"):
                        broken = [e.split(":")[0] for e in errors]
                        self._attempt_self_heal(target, name, broken)
                    return {"delivered": True, "channel": name, "elapsed_s": round(time.time()-budget_start, 1), **result}
                if result.get("wol_sent"):
                    logger.info(f"[{name}] WoL sent to {target}")
                    errors.append(f"{name}: WoL sent, queued for retry")
                    continue
                # ZSF: always log why a channel didn't deliver (no silent failures)
                reason = result.get("error", "delivered=false")
                errors.append(f"{name}: {reason}")
                self._update_channel_health(target, name, "broken")
                logger.debug(f"[{name}] not delivered to {target}: {reason}")
            except asyncio.TimeoutError:
                errors.append(f"{name}: timeout ({CHANNEL_TIMEOUTS.get(name, 5)}s)")
                self._update_channel_health(target, name, "broken")
                logger.debug(f"[{name}] timed out for {target} after {CHANNEL_TIMEOUTS.get(name, 5)}s")
            except Exception as e:
                errors.append(f"{name}: {e}")
                self._update_channel_health(target, name, "broken")
                logger.debug(f"[{name}] failed for {target}: {e}")

        # Last resort: if cloud wasn't already tried, try P0 cloud as final fallback
        if not use_cloud:
            try:
                result = await self._send_p0_cloud(target, message)
                if result.get("delivered"):
                    logger.info(f"[P0_cloud] Delivered to cloud (all local channels failed)")
                    return {"delivered": True, "channel": "P0_cloud", **result}
            except Exception as e:
                errors.append(f"P0_cloud: {e}")
                logger.debug(f"[P0_cloud] fallback also failed for {target}: {e}")

        logger.warning(f"All channels failed for {target}: {errors}")
        return {"delivered": False, "errors": errors}

    async def _send_broadcast(self, message: dict, use_cloud: bool = False) -> dict:
        """Broadcast optimized for 100+ nodes.

        Strategy:
        1. NATS fleet.all.message (O(1) pub/sub) -- PRIMARY
        2. If NATS down: async parallel HTTP to all known peers with semaphore
        3. P7 git broadcast rate-limited to 1/hour
        """
        t0 = time.time()
        message["to"] = "all"

        # ── Try NATS first (O(1) broadcast) ──
        nats_ok = False
        try:
            result = await asyncio.wait_for(self._send_p1_nats("all", message), timeout=2)
            if result.get("delivered"):
                nats_ok = True
                self._stats["broadcasts"] += 1
                logger.info(f"[broadcast] NATS pub fleet.all.message OK ({time.time()-t0:.2f}s)")
                return {"delivered": True, "channel": "P1_nats", "mode": "broadcast_nats",
                        "elapsed_s": round(time.time() - t0, 2)}
        except Exception as e:
            logger.warning(f"[broadcast] NATS failed, falling back to HTTP parallel: {e}")

        # ── NATS down: async parallel HTTP to all known peers ──
        peers = list(self._peers.keys()) + [
            p for p in self._peers_config.keys() if p not in self._peers
        ]
        # Remove self from broadcast targets
        peers = [p for p in peers if p != self.node_id]

        if not peers:
            logger.warning("[broadcast] No known peers for HTTP fallback")
            return {"delivered": False, "channel": "none", "mode": "broadcast_no_peers",
                    "errors": ["NATS down + no known peers"]}

        # Parallel HTTP with semaphore to prevent socket exhaustion
        semaphore = asyncio.Semaphore(BROADCAST_PARALLEL_LIMIT)
        errors = []
        delivered_count = 0

        async def _http_to_peer(peer_id: str) -> bool:
            nonlocal delivered_count
            async with semaphore:
                try:
                    result = await asyncio.wait_for(
                        self._send_p2_http(peer_id, message),
                        timeout=BROADCAST_HTTP_TIMEOUT_S
                    )
                    if result.get("delivered"):
                        delivered_count += 1
                        self._update_channel_health(peer_id, "P2_http", "ok")
                        return True
                    else:
                        errors.append(f"{peer_id}: http not delivered")
                        self._update_channel_health(peer_id, "P2_http", "broken")
                        return False
                except asyncio.TimeoutError:
                    errors.append(f"{peer_id}: http timeout ({BROADCAST_HTTP_TIMEOUT_S}s)")
                    self._update_channel_health(peer_id, "P2_http", "broken")
                    return False
                except Exception as e:
                    errors.append(f"{peer_id}: {e}")
                    self._update_channel_health(peer_id, "P2_http", "broken")
                    return False

        # Fire all HTTP sends in parallel
        await asyncio.gather(*[_http_to_peer(p) for p in peers], return_exceptions=True)
        self._stats["broadcasts"] += 1

        elapsed = round(time.time() - t0, 2)
        if delivered_count > 0:
            logger.info(f"[broadcast] HTTP parallel: {delivered_count}/{len(peers)} peers reached ({elapsed}s)")
            return {"delivered": True, "channel": "P2_http", "mode": "broadcast_http_parallel",
                    "peers_reached": delivered_count, "peers_total": len(peers),
                    "elapsed_s": elapsed}

        # ── All HTTP failed: try P7 git as last resort (rate-limited) ──
        now = time.time()
        if now - self._last_git_broadcast_ts >= GIT_BROADCAST_COOLDOWN_S:
            msg_type = message.get("type", "")
            is_repair = msg_type == "repair" or "self-heal" in str(message.get("payload", {}).get("subject", ""))
            if not is_repair:
                try:
                    git_result = await asyncio.wait_for(self._send_p7_git("all", message), timeout=15)
                    if git_result.get("delivered"):
                        self._last_git_broadcast_ts = now
                        logger.info(f"[broadcast] P7 git fallback delivered ({time.time()-t0:.1f}s)")
                        return {"delivered": True, "channel": "P7_git", "mode": "broadcast_git_fallback",
                                "elapsed_s": round(time.time() - t0, 2)}
                except Exception as e:
                    errors.append(f"P7_git: {e}")
                    logger.debug(f"[broadcast] P7 git fallback failed: {e}")
        else:
            remaining = GIT_BROADCAST_COOLDOWN_S - (now - self._last_git_broadcast_ts)
            errors.append(f"P7_git: rate-limited ({remaining:.0f}s remaining)")
            logger.debug(f"[broadcast] P7 git skipped: rate-limited, {remaining:.0f}s until next allowed")

        # ── Cloud as absolute last resort ──
        if not use_cloud:
            try:
                result = await self._send_p0_cloud("all", message)
                if result.get("delivered"):
                    logger.info("[broadcast] P0 cloud fallback delivered")
                    return {"delivered": True, "channel": "P0_cloud", "mode": "broadcast_cloud_fallback",
                            "elapsed_s": round(time.time() - t0, 2)}
            except Exception as e:
                errors.append(f"P0_cloud: {e}")

        logger.warning(f"[broadcast] All channels failed: {errors}")
        return {"delivered": False, "mode": "broadcast_all_failed", "errors": errors}

    async def _send_p0_cloud(self, target: str, message: dict) -> dict:
        """P0: Cloud dispatch via RemoteTrigger API. Spawns isolated Claude Code in Anthropic cloud."""
        try:
            from .cloud import send_cloud
        except ImportError:
            return {"delivered": False}

        prompt = message.get("payload", {}).get("body", "")
        if not prompt:
            prompt = json.dumps(message.get("payload", {}))

        return await send_cloud(
            prompt=prompt,
            payload=message,
        )


    # ── send_smart -- session-aware routing ──

    async def send_smart(self, target: str, message: dict) -> dict:
        """Session-aware message routing using NATS peer health cache.

        Routes by type:
          task    -> prefer idle session (idle > 5min)
          context/reply -> primary (most recently active) session
          alert   -> ALL sessions + macOS notification
        Falls back to send_with_fallback() for actual delivery.
        """
        t0 = time.time()
        msg_type = message.get('type', 'context')
        message.setdefault('id', str(uuid.uuid4()))
        message.setdefault('from', self.node_id)
        message.setdefault('to', target)
        message.setdefault('timestamp', datetime.now(timezone.utc).isoformat() + 'Z')

        # Probe target via NATS heartbeat cache
        peer = self._peers.get(target)
        if not peer or time.time() - peer.get('_received_at', 0) > 120:
            result = await self.send_with_fallback(target, message)
            result.update(routing='no_peer_health', session_target=None,
                          elapsed_s=round(time.time() - t0, 2))
            return result

        sessions = peer.get('sessions', [])
        routing = 'direct'
        session_target = None

        if msg_type == 'alert':
            routing = 'alert_all_sessions'
            session_target = f'all({len(sessions)})'

        elif msg_type == 'task':
            idle = [(s.get('idle_s', 0), s) for s in sessions
                    if s.get('idle_s', 0) > 300]
            idle.sort(key=lambda x: x[0], reverse=True)
            if idle:
                chosen = idle[0][1]
                session_target = chosen.get('sessionId', '?')[:12]
                message['_target_session'] = session_target
                routing = 'task_to_idle_session'
            else:
                routing = 'task_no_idle'

        elif msg_type in ('context', 'reply'):
            if sessions:
                primary = min(sessions, key=lambda s: s.get('idle_s', 9999))
                session_target = primary.get('sessionId', '?')[:12]
                message['_target_session'] = session_target
                routing = 'context_to_primary'

        result = await self.send_with_fallback(target, message)
        result.update(routing=routing, session_target=session_target,
                      elapsed_s=round(time.time() - t0, 2))
        logger.info(f'send_smart: {target} type={msg_type} routing={routing} '
                     f'session={session_target} delivered={result.get("delivered")}')
        return result

    def _update_channel_health(self, target: str, channel: str, status: str):
        """Track per-peer channel health. Status: 'ok' or 'broken'."""
        if target not in self._channel_health:
            self._channel_health[target] = {ch: "ok" for ch in ALL_CHANNELS}
        self._channel_health[target][channel] = status

    def _get_peer_health_score(self, target: str) -> tuple:
        """Returns (score, broken_list) for a peer. Max score = len(ALL_CHANNELS)."""
        ch = self._channel_health.get(target, {})
        broken = [name for name in ALL_CHANNELS if ch.get(name) == "broken"]
        score = len(ALL_CHANNELS) - len(broken)
        return score, broken

    def _attempt_self_heal(self, target: str, working_channel: str, broken: list):
        """Aggressive self-heal: immediately start repair_with_escalation for broken channels.

        Replaces the old L1-only notify. Now triggers full L1-L4 escalation,
        then re-probes each broken channel to verify the fix actually worked.
        Deduplicates: only one heal task per peer at a time.
        """
        if not broken:
            return
        # Don't stack heals -- one per peer
        existing = self._heal_in_flight.get(target)
        if existing and not existing.done():
            logger.debug(f"Self-heal already in flight for {target}, skipping")
            return
        score, _ = self._get_peer_health_score(target)
        action = "restart_nats" if "P1_nats" in broken else "check_connectivity"
        logger.info(f"Self-heal triggered for {target}: score={score}/{len(ALL_CHANNELS)}, broken={broken}")

        async def _heal_and_verify():
            try:
                result = await self.repair_with_escalation(target, broken, action)
                if result and result.get("repaired"):
                    # Re-probe each broken channel to verify repair
                    for ch in broken:
                        probe_ok = await self._verify_channel(target, ch)
                        self._update_channel_health(target, ch, "ok" if probe_ok else "broken")
                    new_score, still_broken = self._get_peer_health_score(target)
                    logger.info(f"Self-heal result for {target}: "
                                f"score {score}->{new_score}/{len(ALL_CHANNELS)}, "
                                f"still_broken={still_broken}")
                else:
                    logger.warning(f"Self-heal for {target} did not fully repair: {result}")
            except Exception as e:
                logger.warning(f"Self-heal task failed for {target}: {e}")
            finally:
                self._heal_in_flight.pop(target, None)

        self._heal_in_flight[target] = asyncio.ensure_future(_heal_and_verify())

    async def _verify_channel(self, target: str, channel: str) -> bool:
        """Probe a single channel to verify it works. Returns True if delivered."""
        probe_msg = {"type": "sync", "payload": {"subject": "ping"}, "from": self.node_id}
        sender_map = {
            "P1_nats": self._send_p1_nats, "P2_http": self._send_p2_http,
            "P3_chief": self._send_p3_chief, "P4_seed": self._send_p4_seed,
            "P5_ssh": self._send_p5_ssh, "P7_git": self._send_p7_git,
        }
        sender = sender_map.get(channel)
        if not sender:
            return False
        try:
            result = await asyncio.wait_for(sender(target, probe_msg), timeout=5)
            return bool(result.get("delivered"))
        except Exception as e:
            logger.debug(f"Channel probe {channel} for {target} failed: {e}")
            return False

    async def repair_with_escalation(self, target: str, broken_channels: list, action: str = "restart_nats"):
        """Multi-level repair escalation. Total time to L4: ~90s.

        Level 1 NOTIFY: Silent repair msg to target (30s wait)
        Level 2 GUIDE: Send specific repair command (30s wait)
        Level 3 ASSIST: Send auto-exec repair action (30s wait)
        Level 4 REMOTE: SSH direct intervention (~90s after detection)

        All repair messages use type:"repair" (NOT "context") to avoid
        seed file writes and keystroke injection spam on the target.
        Each level uses send_with_fallback() for delivery.
        """
        logger.info(f"Repair escalation for {target}: broken={broken_channels}, action={action}")

        # Level 1: NOTIFY -- silent repair msg (no seed/keystroke), 2min wait
        logger.info(f"Repair L1 NOTIFY -> {target}")
        notify_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": action,
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 1,
                }),
            },
        }
        result = await self.send_with_fallback(target, notify_msg)
        if result.get("delivered"):
            logger.info(f"Repair L1 delivered via {result.get('channel')}. Waiting 120s for local fix...")
            await asyncio.sleep(120)  # 2min -- give target time to self-fix
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L1 notify")
                return {"repaired": True, "level": 1}

        # Level 2: GUIDE -- auto-executable repair command (still silent, no seed/keystroke)
        logger.info(f"Repair L2 GUIDE -> {target}")
        guide_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": "restart_daemon",
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 2,
                }),
            },
        }
        result = await self.send_with_fallback(target, guide_msg)
        if result.get("delivered"):
            logger.info(f"Repair L2 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L2 guide")
                return {"repaired": True, "level": 2}

        # Level 3: ASSIST -- send auto-executable repair action (mac1's handlers)
        logger.info(f"Repair L3 ASSIST -> {target}")
        repair_msg = {
            "type": "repair", "from": self.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": action,
                    "reporter": self.node_id,
                    "broken_channels": broken_channels,
                    "level": 3,
                }),
            },
        }
        result = await self.send_with_fallback(target, repair_msg)
        if result.get("delivered"):
            logger.info(f"Repair L3 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L3 assist")
                return {"repaired": True, "level": 3}

        # Level 4: REMOTE -- SSH direct intervention (last resort, ~90s after first detection)
        logger.info(f"Repair L4 REMOTE -> {target} (~90s failure, all local attempts exhausted)")
        peer = self._resolve_peer(target)
        user, ip = peer.get("user"), peer.get("ip")
        if user and ip:
            try:
                # Check if target has active session -- don't interrupt
                check = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                     "pgrep -f 'claude.*native-binary' > /dev/null && echo ACTIVE || echo NONE"],
                    capture_output=True, text=True, timeout=8)
                if "ACTIVE" in check.stdout:
                    logger.warning(f"Repair L4: {target} has active session -- NOT restarting remotely")
                    # Notify the active session instead -- use base64 to prevent injection
                    import base64
                    notice = f"Fleet repair: your channels {broken_channels} are down for 3min+. Please restart daemon."
                    b64_notice = base64.b64encode(notice.encode()).decode()
                    # Validate target name before using in path
                    safe_target = "".join(c for c in target if c.isalnum() or c in "-_")
                    subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                         f"echo '{b64_notice}' | base64 -d > /tmp/fleet-seed-{safe_target}.md"],
                        capture_output=True, timeout=8)
                    return {"repaired": False, "level": 4, "reason": "active_session_detected"}
                else:
                    # No active session -- safe to remote restart
                    logger.info(f"Repair L4: no active session on {target} -- remote restart")
                    safe_target = "".join(c for c in target if c.isalnum() or c in "-_")
                    subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=3", f"{user}@{ip}",
                         f"pkill -f fleet_nerve_nats.py; sleep 2; cd ~/dev/er-simulator-superrepo && "
                         f"MULTIFLEET_NODE_ID={safe_target} nohup python3 tools/fleet_nerve_nats.py serve &>/tmp/fleet-nats-{safe_target}.log &"],
                        capture_output=True, timeout=15)
                    await asyncio.sleep(10)
                    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
                    if probe.get("delivered"):
                        logger.info(f"Repair SUCCESS: {target} back on P1 after L4 remote restart")
                        return {"repaired": True, "level": 4}
            except Exception as e:
                logger.warning(f"Repair L4 SSH failed: {e}")

        logger.warning(f"Repair FAILED: all 4 levels exhausted for {target}")
        return {"repaired": False, "level": 4, "reason": "all_levels_failed"}

    async def broadcast_health(self) -> list:
        """Request health from all nodes, collect replies."""
        results = []

        # Self
        results.append({"node": self.node_id, "status": 200, "data": self._build_health()})

        # Request/reply to each known peer + discovered peers
        inbox = self.nc.new_inbox()
        sub = await self.nc.subscribe(inbox)

        await self.nc.publish("fleet.all.health", json.dumps({
            "from": self.node_id, "type": "health_request"
        }).encode(), reply=inbox)

        # Collect replies for 3 seconds
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                msg = await asyncio.wait_for(sub.next_msg(), timeout=0.5)
                data = json.loads(msg.data.decode())
                if data.get("nodeId") != self.node_id:
                    results.append({"node": data["nodeId"], "status": 200, "data": data})
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug(f"Health collect reply parse error: {e}")
                break

        await sub.unsubscribe()
        results.sort(key=lambda r: r["node"])
        return results

    # ── Heartbeat ──

    async def _heartbeat_loop(self):
        """Publish heartbeat every 10s."""
        while self._running:
            try:
                detected = self._detect_sessions()
                hb = {
                    "node": self.node_id,
                    "timestamp": time.time(),
                    "ip": self._get_local_ip(),
                    "sessions": len(detected),
                    "session_list": [
                        {"sessionId": s.get("sessionId", ""), "idle_s": s.get("idle_s", 0)}
                        for s in detected
                    ],
                    "vscode": self._detect_vscode_state(),
                    "git": self._get_git_context(),
                }
                await self.nc.publish("fleet.heartbeat", json.dumps(hb).encode())

                # Publish session historian data (change-driven, piggybacked on heartbeat)
                if fleet_historian_nats:
                    try:
                        await fleet_historian_nats.publish_all_session_data(self)
                    except Exception as e:
                        logger.debug(f"Session publish failed: {e}")

            except Exception as e:
                logger.debug(f"Heartbeat publish failed: {e}")
            await asyncio.sleep(10)

    def _get_local_ip(self) -> str:
        """Get LAN IP for heartbeat advertisement."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return ""

    # ── UDP Gossip Heartbeat ──

    def _get_known_peer_ips(self) -> dict[str, str]:
        """Return {node_id: ip} for all known peers (from heartbeats + config)."""
        peers = {}
        # From NATS heartbeat data (runtime-discovered)
        for node, data in self._peers.items():
            ip = data.get("ip")
            if ip and node != self.node_id:
                peers[node] = ip
        # From static peer config (fleet_nerve_config)
        for node, cfg in self._peers_config.items():
            if node != self.node_id and node not in peers:
                ip = cfg.get("ip") or cfg.get("host")
                if ip:
                    peers[node] = ip
        return peers

    def _select_gossip_targets(self, exclude_nodes: set[str] | None = None) -> list[tuple[str, str]]:
        """Select up to GOSSIP_FANOUT random peers to gossip to.

        Returns list of (node_id, ip) tuples. Excludes self and any nodes in exclude_nodes.
        """
        peer_ips = self._get_known_peer_ips()
        if exclude_nodes:
            peer_ips = {n: ip for n, ip in peer_ips.items() if n not in exclude_nodes}
        candidates = list(peer_ips.items())
        if len(candidates) <= GOSSIP_FANOUT:
            return candidates
        return random.sample(candidates, GOSSIP_FANOUT)

    async def _udp_gossip_loop(self):
        """Send UDP gossip heartbeats every 10s to GOSSIP_FANOUT random peers."""
        # Wait for initial NATS heartbeats to populate peer list
        await asyncio.sleep(15)
        logger.info(f"UDP gossip heartbeat started (fanout={GOSSIP_FANOUT}, ttl={GOSSIP_TTL}, port={UDP_GOSSIP_PORT})")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        while self._running:
            try:
                targets = self._select_gossip_targets()
                if not targets:
                    await asyncio.sleep(10)
                    continue

                detected = self._detect_sessions()
                hb = {
                    "node": self.node_id,
                    "timestamp": time.time(),
                    "ip": self._get_local_ip(),
                    "sessions": len(detected),
                    "session_list": [
                        {"sessionId": s.get("sessionId", ""), "idle_s": s.get("idle_s", 0)}
                        for s in detected
                    ],
                    "vscode": self._detect_vscode_state(),
                    "git": self._get_git_context(),
                    # Gossip metadata
                    "_gossip_ttl": GOSSIP_TTL,
                    "_gossip_path": [self.node_id],
                    "_gossip_origin": self.node_id,
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

    async def _udp_gossip_listener(self):
        """Listen for UDP gossip heartbeats and forward to more peers (with TTL)."""
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

        while self._running:
            try:
                # Non-blocking recv via asyncio
                data, addr = await loop.run_in_executor(None, lambda: sock.recvfrom(65535))
                hb = json.loads(data.decode())

                node = hb.get("node", "unknown")
                if node == self.node_id:
                    continue  # Ignore own heartbeats

                # Update peer state (same as NATS _handle_heartbeat)
                self._peers[node] = {**hb, "_received_at": time.time(), "_via": "udp_gossip"}
                if hasattr(self, '_repair_in_progress'):
                    self._repair_in_progress.pop(node, None)
                if hasattr(self, 'protocol') and self.protocol:
                    self.protocol.channel_state.record_success(node, "P1_nats", 0)

                # Gossip forwarding: decrement TTL, forward to more peers
                ttl = hb.get("_gossip_ttl", 0)
                gossip_path = hb.get("_gossip_path", [])

                if ttl > 0:
                    # Add ourselves to the path
                    new_path = gossip_path + [self.node_id]
                    hb["_gossip_ttl"] = ttl - 1
                    hb["_gossip_path"] = new_path

                    # Don't forward back to sender, origin, or anyone in the path
                    exclude = set(new_path) | {hb.get("_gossip_origin", "")}
                    targets = self._select_gossip_targets(exclude_nodes=exclude)

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

    # ── Proactive Fleet Watchdog ──

    async def _watchdog_loop(self):
        """Proactive health monitor -- detects degraded peers and auto-repairs.

        Runs every 30s. Checks:
        1. Peer heartbeat staleness -> triggers repair_with_escalation()
        2. Local zombie processes -> auto-kills known runaways
        3. Git divergence -> auto-pulls if behind origin
        4. Tunnel health -> restarts dead tunnels
        5. Idle detection -> proactive work discovery for self and peers
        """
        # Wait 60s for fleet to stabilize on startup
        await asyncio.sleep(60)
        logger.info("Watchdog started -- proactive fleet health monitoring (30s interval)")

        # Track repair state to avoid re-triggering during escalation
        self._repair_in_progress = {}  # target -> {started_at, level}

        # Idle tracking state
        self._last_activity_at = time.time()  # updated on send/receive
        self._idle_wake_sent = {}  # peer_id -> timestamp of last idle-wake sent
        self._last_suggestion_scan = 0.0  # productive idle rate limit

        while self._running:
            try:
                await self._watchdog_check_peers()
                await self._watchdog_check_channel_health()
                await self._watchdog_check_zombies()
                await self._watchdog_check_git()
                await self._watchdog_check_tunnels()
                await self._watchdog_check_idle()
                await self._watchdog_check_pending_acks()
            except Exception as e:
                logger.debug(f"Watchdog cycle error: {e}")
            await asyncio.sleep(30)

    def _touch_activity(self):
        """Mark that NATS activity occurred (call on send/receive)."""
        if hasattr(self, "_last_activity_at"):
            self._last_activity_at = time.time()

    async def _watchdog_check_idle(self):
        """Detect idle state for this node and peers, trigger proactive work discovery.

        Self-idle (no NATS messages sent/received >5min):
          - Check fleet inbox (.fleet-messages/<node>/) for unprocessed messages
          - Check /tmp/fleet-seed-<node>.md for pending messages
          - Broadcast idle status to fleet
          - Trigger git pull if behind origin
          - Log for observability

        Peer-idle (session count = 0 or last heartbeat >10min):
          - Send gentle wake message (rate-limited: 1 per peer per 30min)
        """
        now = time.time()
        idle_threshold_self = 300   # 5 minutes
        idle_threshold_peer = 600   # 10 minutes
        wake_cooldown = 1800        # 30 minutes between idle-wakes per peer

        # ── Self idle check ──
        idle_seconds = now - self._last_activity_at
        if idle_seconds > idle_threshold_self:
            pending_items = []

            # Check fleet inbox for unprocessed messages
            inbox_dir = REPO_ROOT / ".fleet-messages" / self.node_id
            if inbox_dir.exists():
                unprocessed = [f.name for f in inbox_dir.iterdir()
                               if f.is_file() and f.suffix == ".md"]
                if unprocessed:
                    pending_items.append(f"{len(unprocessed)} inbox message(s)")

            # Check seed file for pending messages
            seed_path = Path(SEED_DIR) / f"fleet-seed-{self.node_id}.md"
            if seed_path.exists() and seed_path.stat().st_size > 0:
                pending_items.append("pending seed file")

            pending_note = f" -- found: {', '.join(pending_items)}" if pending_items else ""
            idle_msg = (
                f"Node {self.node_id} idle ({int(idle_seconds)}s)"
                f" -- checking plans and exploring opportunities{pending_note}"
            )
            logger.info(f"Watchdog idle: {idle_msg}")

            # Broadcast idle status to fleet
            try:
                if self.nc and self.nc.is_connected:
                    await self.send("all", "idle", "idle-check", idle_msg)
            except Exception as e:
                logger.debug(f"Watchdog idle broadcast failed: {e}")

            # Trigger git pull if behind (reuses existing method's cooldown)
            await self._watchdog_check_git()

            # Productive idle: scan for actionable work and broadcast suggestions
            await self._productive_idle_scan()

            # Reset activity timer so we don't spam every 30s
            self._last_activity_at = now

        # ── Peer idle check ──
        for peer_id, hb_data in self._peers.items():
            if peer_id == self.node_id:
                continue

            peer_last_seen = now - hb_data.get("_received_at", 0)
            peer_sessions = hb_data.get("sessions", -1)
            peer_is_idle = (
                peer_last_seen > idle_threshold_peer
                or peer_sessions == 0
            )

            if not peer_is_idle:
                continue

            # Rate limit: max 1 idle-wake per peer per 30min
            last_wake = self._idle_wake_sent.get(peer_id, 0)
            if now - last_wake < wake_cooldown:
                continue

            idle_min = int(peer_last_seen / 60)
            reason = (
                f"no heartbeat for {idle_min}min"
                if peer_last_seen > idle_threshold_peer
                else "0 active sessions"
            )
            wake_msg = (
                f"You've been idle for {idle_min}min ({reason})."
                f" Any pending fleet work? Check plans."
            )

            try:
                if self.nc and self.nc.is_connected:
                    await self.send(peer_id, "idle-wake", "idle-wake", wake_msg)
                    self._idle_wake_sent[peer_id] = now
                    logger.info(f"Watchdog idle-wake sent to {peer_id} ({reason})")
            except Exception as e:
                logger.debug(f"Watchdog idle-wake to {peer_id} failed: {e}")

    async def _productive_idle_scan(self):
        """Scan for productive work when idle. Rate-limited: 1 per 10min.

        Checks incomplete plan items, unanswered fleet messages, and recent
        commits lacking tests.  Writes to /tmp/fleet-watchdog-suggestions.json
        and broadcasts a summary to the fleet.
        """
        now = time.time()
        if now - self._last_suggestion_scan < 600:  # 10min cooldown
            return
        self._last_suggestion_scan = now
        suggestions = []

        # 1. Incomplete items in docs/plans/
        plans_dir = REPO_ROOT / "docs" / "plans"
        if plans_dir.exists():
            for p in sorted(plans_dir.iterdir()):
                if not p.is_file() or p.suffix != ".md":
                    continue
                try:
                    text = p.read_text(errors="replace")
                    todos = [ln.strip() for ln in text.splitlines()
                             if any(k in ln for k in ("[ ]", "TODO", "UNRESOLVED"))]
                    if todos:
                        suggestions.append({
                            "source": "plans",
                            "file": str(p.relative_to(REPO_ROOT)),
                            "items": todos[:5],
                        })
                except Exception:
                    pass

        # 2. Unanswered fleet messages (all nodes' inboxes)
        fleet_dir = REPO_ROOT / ".fleet-messages"
        if fleet_dir.exists():
            for node_dir in fleet_dir.iterdir():
                if not node_dir.is_dir() or node_dir.name in ("archive",):
                    continue
                msgs = [f.name for f in node_dir.iterdir()
                        if f.is_file() and f.suffix == ".md"]
                if msgs:
                    suggestions.append({
                        "source": "fleet-messages",
                        "node": node_dir.name,
                        "pending": msgs[:10],
                    })

        # 3. Recent commits without tests (last 24h)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(REPO_ROOT), "log",
                "--since=24 hours ago", "--pretty=format:%h %s",
                "--no-merges", "-20",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            commits = stdout.decode(errors="replace").strip().splitlines()
            untested = [c for c in commits
                        if c and "test" not in c.lower()
                        and "fleet-msg" not in c.lower()]
            if untested:
                suggestions.append({
                    "source": "untested-commits",
                    "commits": untested[:10],
                })
        except Exception:
            pass

        # Write to disk
        output = {
            "node": self.node_id,
            "scanned_at": datetime.now(timezone.utc).isoformat() + "Z",
            "suggestions": suggestions,
        }
        try:
            Path("/tmp/fleet-watchdog-suggestions.json").write_text(
                json.dumps(output, indent=2))
        except Exception as e:
            logger.debug(f"Productive idle: write suggestions failed: {e}")

        # Broadcast summary to fleet
        if suggestions and self.nc and self.nc.is_connected:
            lines = []
            for s in suggestions:
                src = s["source"]
                if src == "plans":
                    lines.append(
                        f"  plans/{Path(s['file']).name}: "
                        f"{len(s['items'])} TODO(s)")
                elif src == "fleet-messages":
                    lines.append(
                        f"  fleet-messages/{s['node']}: "
                        f"{len(s['pending'])} pending")
                elif src == "untested-commits":
                    lines.append(
                        f"  {len(s['commits'])} recent commit(s) "
                        f"may need tests")
            body = ("Productive idle scan found work:\n"
                    + "\n".join(lines))
            try:
                await self.send(
                    "all", "idle-suggestions", "productive-idle", body)
            except Exception:
                pass

        logger.info(f"Productive idle scan: "
                    f"{len(suggestions)} suggestion group(s)")

    async def _watchdog_check_peers(self):
        """Detect stale peers and trigger repair escalation."""
        now = time.time()
        all_peers = set(self._peers_config.keys()) - {self.node_id}
        seen_peers = set(self._peers.keys())

        for peer_id in all_peers:
            # Skip if repair already in progress
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:  # 5min cooldown
                continue

            if peer_id in seen_peers:
                last_seen = now - self._peers[peer_id].get("_received_at", 0)
                if last_seen > 90:  # 90s = 9 missed heartbeats
                    logger.warning(f"Watchdog: {peer_id} heartbeat stale ({int(last_seen)}s)")
                    self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
                    asyncio.create_task(
                        self.repair_with_escalation(peer_id, ["P1_nats"], "restart_nats"))
                elif last_seen > 45:  # Warning threshold
                    logger.info(f"Watchdog: {peer_id} heartbeat slow ({int(last_seen)}s)")
            else:
                # Never seen this peer -- they might not be online yet
                # Only escalate if we've been running >5min (past startup window)
                if now - self._stats["started_at"] > 300:
                    logger.warning(f"Watchdog: {peer_id} never seen -- attempting contact")
                    self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
                    asyncio.create_task(
                        self.repair_with_escalation(peer_id, ["P1_nats", "P2_http"], "restart_nats"))

    async def _watchdog_check_channel_health(self):
        """Check channel health scores and trigger repair for degraded peers."""
        now = time.time()
        for peer_id, channels in self._channel_health.items():
            score, broken = self._get_peer_health_score(peer_id)
            if score >= HEAL_SCORE_THRESHOLD or not broken:
                continue
            # Skip if repair already in progress (5min cooldown)
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:
                continue
            existing = self._heal_in_flight.get(peer_id)
            if existing and not existing.done():
                continue
            logger.warning(f"Watchdog: {peer_id} channel score {score}/{len(ALL_CHANNELS)} "
                           f"< {HEAL_SCORE_THRESHOLD} -- auto-repair: {broken}")
            self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
            action = "restart_nats" if "P1_nats" in broken else "check_connectivity"
            self._attempt_self_heal(peer_id, "watchdog", broken)

    async def _watchdog_check_pending_acks(self):
        """Retry messages with no ACK after timeout. Drop after max retries."""
        now = time.time()
        to_remove = []
        for msg_id, info in list(self._pending_acks.items()):
            if info["acked"]:
                to_remove.append(msg_id)
                continue
            age = now - info["sent_at"]
            if age < self._ack_timeout_s:
                continue
            if info["retries"] >= self._ack_max_retries:
                logger.warning(f"ACK failed after {self._ack_max_retries} retries: "
                               f"{msg_id[:8]} -> {info['target']}")
                to_remove.append(msg_id)
                continue
            info["retries"] += 1
            logger.info(f"ACK retry {info['retries']}/{self._ack_max_retries}: "
                        f"{msg_id[:8]} -> {info['target']}")
            asyncio.create_task(self.send_with_fallback(info["target"], info["message"]))
        for msg_id in to_remove:
            self._pending_acks.pop(msg_id, None)

    async def _watchdog_check_zombies(self):
        """Detect and kill known zombie patterns (runaway processes)."""
        try:
            result = subprocess.run(
                ["ps", "aux"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) < 11:
                    continue
                try:
                    pid = int(parts[1])
                    cpu = float(parts[2])
                    # Skip our own daemon process
                    if pid == os.getpid():
                        continue
                    # Flag: python process using >10% CPU for extended time
                    # (We can't tell duration from ps alone, so only kill known patterns)
                    cmd = " ".join(parts[10:])
                    # Known zombie: fleet_nerve_daemon.py fighting for our port
                    if "fleet_nerve_daemon.py" in cmd and "--port 8855" in cmd:
                        logger.warning(f"Watchdog: killing rogue fleet_nerve_daemon.py (PID {pid})")
                        os.kill(pid, 9)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except Exception:
            pass

    async def _watchdog_check_git(self):
        """Auto-pull if behind origin (check every cycle, pull at most every 5min)."""
        if not hasattr(self, "_last_git_pull"):
            self._last_git_pull = 0
        now = time.time()
        if now - self._last_git_pull < 300:  # 5min cooldown
            return
        try:
            # Check if behind
            subprocess.run(["git", "fetch", "origin", "main"],
                           cwd=str(REPO_ROOT), capture_output=True, timeout=15)
            result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..FETCH_HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=5)
            behind = int(result.stdout.strip()) if result.returncode == 0 else 0
            if behind > 0:
                logger.info(f"Watchdog: {behind} commits behind origin -- auto-pulling")
                subprocess.run(["git", "rebase", "FETCH_HEAD"],
                               cwd=str(REPO_ROOT), capture_output=True, timeout=30)
                self._last_git_pull = now
                logger.info("Watchdog: git pull complete")
        except Exception as e:
            logger.debug(f"Watchdog git check error: {e}")

    async def _watchdog_check_tunnels(self):
        """Verify SSH tunnels are alive, restart if dead."""
        tunnel_dir = Path.home() / ".fleet-tunnels"
        if not tunnel_dir.exists():
            return
        any_dead = False
        for pidfile in tunnel_dir.glob("tunnel-*.pid"):
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, 0)  # Check if alive (signal 0)
            except (ProcessLookupError, ValueError):
                logger.warning(f"Watchdog: tunnel {pidfile.stem} is dead -- restarting")
                any_dead = True
                pidfile.unlink(missing_ok=True)
        if any_dead:
            # Restart tunnels
            try:
                subprocess.run(
                    ["bash", str(REPO_ROOT / "scripts" / "fleet-tunnel.sh"), "start"],
                    capture_output=True, timeout=30,
                    env={**os.environ, "MULTIFLEET_NODE_ID": self.node_id})
                logger.info("Watchdog: tunnels restarted")
            except Exception as e:
                logger.debug(f"Watchdog tunnel restart failed: {e}")

    # ── ACK Retry Loop ──

    ACK_TIMEOUT_S = 60.0
    ACK_MAX_RETRIES = 3

    async def _ack_retry_loop(self):
        """Check for unacknowledged messages and retry via next fallback channel.

        Runs every 30s. Messages awaiting ACK past ACK_TIMEOUT_S get re-sent.
        After ACK_MAX_RETRIES, marked as failed and surfaced via _inject_alert.
        """
        await asyncio.sleep(30)  # Let daemon stabilize
        logger.info("ACK retry loop started (30s interval)")
        while self._running:
            try:
                if not self._store:
                    await asyncio.sleep(30)
                    continue
                pending = self._store.get_awaiting_ack(
                    max_age_s=self.ACK_TIMEOUT_S, max_retries=self.ACK_MAX_RETRIES
                )
                for row in pending:
                    msg_id = row["id"]
                    target = row["to_node"]
                    retries = row.get("retry_count", 0)

                    if retries >= self.ACK_MAX_RETRIES:
                        # Exhausted retries -- mark failed and alert
                        self._store.mark_ack_failed(msg_id)
                        logger.warning(f"ACK failed after {retries} retries: {msg_id[:8]}… -> {target}")
                        self._inject_alert({
                            "from": "fleet-ack",
                            "type": "alert",
                            "payload": {
                                "subject": f"Message delivery failed: {msg_id[:8]}…",
                                "body": f"Message to {target} failed after {retries} retries. ID: {msg_id}",
                            },
                        })
                        continue

                    # Rebuild message from stored body
                    try:
                        body_data = json.loads(row.get("body", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        body_data = {}

                    message = {
                        "id": msg_id,
                        "type": row.get("type", "context"),
                        "from": row.get("from_node", self.node_id),
                        "to": target,
                        "payload": body_data,
                        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    }

                    self._store.bump_retry(msg_id)
                    logger.info(f"ACK retry {retries + 1}/{self.ACK_MAX_RETRIES}: {msg_id[:8]}… -> {target}")

                    # Re-send (send_with_fallback will skip re-storing since it already exists)
                    try:
                        result = await self.send_with_fallback(target, message)
                        if result.get("delivered"):
                            logger.info(f"ACK retry delivered: {msg_id[:8]}… via {result.get('channel')}")
                    except Exception as e:
                        logger.warning(f"ACK retry send failed: {msg_id[:8]}…: {e}")

            except Exception as e:
                logger.error(f"ACK retry loop error: {e}")
            await asyncio.sleep(30)

    # ── Main Loop ──

    async def serve(self):
        """Start the NATS daemon -- HTTP first (always useful), then NATS (best-effort)."""
        self._loop = asyncio.get_running_loop()

        # Start HTTP compat FIRST -- useful even without NATS (fallback channels work)
        await self._start_http_compat()
        logger.info(f"Fleet Nerve | node={self.node_id} | nats={self.nats_url}")

        # Try NATS connection (non-blocking -- don't block HTTP if NATS is down)
        try:
            await self.connect()
            await self._subscribe_all()
            asyncio.create_task(self._heartbeat_loop())
            asyncio.create_task(self._watchdog_loop())
        except Exception as e:
            logger.warning(f"NATS connect failed: {e} -- HTTP-only mode, P2-P7 channels available")

        # Start UDP gossip heartbeat (works independently of NATS)
        asyncio.create_task(self._udp_gossip_listener())
        asyncio.create_task(self._udp_gossip_loop())

        # Start ACK retry loop (works with or without NATS -- uses send_with_fallback)
        asyncio.create_task(self._ack_retry_loop())
        # Start CommunicationProtocol self-heal loop (works with or without NATS)
        self.protocol.start()

        # Keep running
        try:
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.nc.close()

    async def _start_http_compat(self):
        """Minimal HTTP server on :8855 for backward compatibility with existing tools."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        daemon = self
        main_loop = asyncio.get_running_loop()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/health":
                    data = json.dumps(daemon._build_health()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/channels":
                    state = daemon.protocol.get_channel_state()
                    data = json.dumps({
                        "nodeId": daemon.node_id,
                        "channels": state,
                        "timestamp": time.time(),
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/peers/discovered":
                    if daemon._discovery:
                        all_peers = daemon._discovery.get_all_peers()
                        data = json.dumps({
                            "nodeId": daemon.node_id,
                            "peers": all_peers,
                            "count": len(all_peers),
                            "mdns_active": daemon._discovery.is_mdns_active,
                            "timestamp": time.time(),
                        }).encode()
                    else:
                        # Fallback: show config peers + NATS-observed peers
                        nats_peers = {
                            nid: {"last_seen": info.get("_received_at", 0), "source": "nats"}
                            for nid, info in daemon._peers.items()
                        }
                        config_peers = {
                            nid: {"source": "config"}
                            for nid in daemon._peers_config
                        }
                        merged = {**nats_peers, **config_peers}
                        data = json.dumps({
                            "nodeId": daemon.node_id,
                            "peers": merged,
                            "count": len(merged),
                            "mdns_active": False,
                            "timestamp": time.time(),
                        }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/dashboard":
                    try:
                        from .dashboard import render_dashboard
                        from .config import load_peers
                        peers, _chief = load_peers()
                        text = render_dashboard(daemon.node_id, peers, port=8855)
                        data = (text or "Fleet idle -- no activity").encode()
                    except Exception as e:
                        data = f"Dashboard error: {e}".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/broadcast/health":
                    # Run async broadcast in the event loop
                    future = asyncio.run_coroutine_threadsafe(daemon.broadcast_health(), main_loop)
                    results = future.result(timeout=10)
                    online = sum(1 for r in results if r["status"] == 200)
                    data = json.dumps({
                        "nodes": len(results), "online": online, "results": results,
                        "transport": "nats",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.startswith("/ack/"):
                    msg_id = self.path[5:]  # strip "/ack/"
                    if daemon._store:
                        info = daemon._store.get_ack_status(msg_id)
                        if info:
                            data = json.dumps(info).encode()
                            self.send_response(200)
                        else:
                            data = json.dumps({"error": "message not found", "id": msg_id}).encode()
                            self.send_response(404)
                    else:
                        data = json.dumps({"error": "store not available"}).encode()
                        self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/dispatches":
                    self._json_response(200, {
                        "dispatches": {k: {kk: vv for kk, vv in v.items() if kk != "result"}
                                       for k, v in daemon._dispatches.items()},
                        "count": len(daemon._dispatches),
                    })
                elif self.path.startswith("/dispatch/"):
                    did = self.path[10:]  # strip "/dispatch/"
                    info = daemon._dispatches.get(did)
                    if info:
                        self._json_response(200, {"id": did, **info})
                    else:
                        self._json_response(404, {"error": "dispatch not found", "id": did})
                elif self.path == "/chains":
                    chains = {cid: daemon._get_chain_status(cid) for cid in daemon._chains}
                    self._json_response(200, {"chains": chains, "count": len(chains)})
                elif self.path.startswith("/chain/"):
                    cid = self.path[7:]  # strip "/chain/"
                    status = daemon._get_chain_status(cid)
                    code = 404 if "error" in status else 200
                    self._json_response(code, status)
                elif self.path == "/fleet/sessions":
                    if fleet_historian_nats:
                        self._json_response(200, fleet_historian_nats.get_peer_sessions(daemon))
                    else:
                        self._json_response(503, {"error": "fleet_historian_nats module not loaded"})
                elif self.path == "/watchdog/suggestions":
                    sug_path = Path("/tmp/fleet-watchdog-suggestions.json")
                    if sug_path.exists():
                        try:
                            raw = sug_path.read_text()
                            json.loads(raw)  # validate JSON
                            data = raw.encode()
                            self.send_response(200)
                        except Exception:
                            data = json.dumps({"error": "corrupt suggestions file"}).encode()
                            self.send_response(500)
                    else:
                        data = json.dumps({
                            "node": daemon.node_id,
                            "scanned_at": None,
                            "suggestions": [],
                            "note": "no scan yet -- idle threshold not reached",
                        }).encode()
                        self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def _validate_peer_ip(self) -> bool:
                """CV-001: Validate request comes from known fleet peer or loopback."""
                client_ip = self.client_address[0]
                if client_ip in ("127.0.0.1", "::1"):
                    return True
                # Static config IPs
                allowed = {p.get("ip") for p in daemon._peers_config.values() if p.get("ip")}
                # Dynamic NATS peer IPs (from heartbeats)
                for peer_data in daemon._peers.values():
                    peer_ip = peer_data.get("ip")
                    if peer_ip:
                        allowed.add(peer_ip)
                if client_ip in allowed:
                    return True
                logger.warning(f"Rejected POST /message from unknown IP: {client_ip}")
                return False

            def _json_response(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                # CV-001: Peer IP validation on all POST endpoints
                if not self._validate_peer_ip():
                    self._json_response(403, {"error": "forbidden: unknown peer IP"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                if self.path == "/message":
                    try:
                        to = body.get("to", "all")
                        use_cloud = body.pop("use_cloud", False)
                        # Use 8-priority fallback protocol (P0 cloud optional)
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_with_fallback(to, body, use_cloud=use_cloud), main_loop)
                        result = future.result(timeout=20)  # Longer for fallback chain
                        data = json.dumps(result).encode()
                    except Exception as e:
                        logger.warning(f"POST /message error: {e}")
                        data = json.dumps({"delivered": False, "error": str(e)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/inject":
                    # Direct --resume injection for testing
                    # Accepts: {"session_id": "...", "message": "..."} or {"message": "..."}
                    # If session_id omitted, auto-detects active session
                    session_id = body.get("session_id") or daemon._get_active_session_full_id()
                    message = body.get("message", "")
                    if not message:
                        self._json_response(400, {"error": "missing 'message' field"})
                        return
                    if not session_id:
                        self._json_response(404, {
                            "delivered": False,
                            "method": "none",
                            "error": "no active session found",
                        })
                        return
                    ok = daemon._inject_via_resume(session_id, message)
                    if ok:
                        self._json_response(200, {
                            "delivered": True,
                            "method": "resume",
                            "session": session_id[:12],
                        })
                    else:
                        # Fallback to seed file
                        seed_path = os.path.join(SEED_DIR, f"fleet-seed-{daemon.node_id}.md")
                        with open(seed_path, "a") as f:
                            f.write(f"\n## [INJECT] Direct injection\n\n{message}\n\n---\n")
                        self._json_response(200, {
                            "delivered": True,
                            "method": "seed_fallback",
                            "session": session_id[:12],
                            "seed_path": seed_path,
                        })
                elif self.path == "/send-smart":
                    try:
                        to = body.get("to", body.get("target", "all"))
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_smart(to, body), main_loop)
                        result = future.result(timeout=35)
                        data = json.dumps(result).encode()
                    except Exception as e:
                        logger.warning(f"POST /send-smart error: {e}")
                        data = json.dumps({"delivered": False, "error": str(e)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/chain":
                    steps = body.get("steps", [])
                    chain_id = body.get("chain_id") or str(uuid.uuid4())
                    if not steps:
                        self._json_response(400, {"error": "missing 'steps' array"})
                        return
                    daemon._handle_chain({"chain_id": chain_id, "steps": steps})
                    self._json_response(200, {
                        "chain_id": chain_id,
                        "status": "running",
                        "steps": len(steps),
                    })
                elif self.path == "/dispatch":
                    target = body.get("target", "")
                    prompt = body.get("prompt", "")
                    timeout_s = int(body.get("timeout_s", 300))
                    priority = body.get("priority", "normal")
                    if not target or not prompt:
                        self._json_response(400, {"error": "missing target or prompt"})
                        return
                    dispatch_id = str(uuid.uuid4())[:12]
                    daemon._dispatches[dispatch_id] = {
                        "target": target, "prompt": prompt[:200], "status": "sent",
                        "sent_at": time.time(), "result": None, "priority": priority,
                    }
                    task_msg = {
                        "type": "task", "from": daemon.node_id, "to": target,
                        "payload": {
                            "subject": f"Dispatch {dispatch_id[:8]}",
                            "body": prompt,
                            "dispatch_id": dispatch_id,
                            "timeout_s": timeout_s,
                        },
                    }
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            daemon.send_with_fallback(target, task_msg), main_loop)
                        result = future.result(timeout=20)
                        if result.get("delivered"):
                            self._json_response(200, {"dispatch_id": dispatch_id, "status": "sent", "delivered": True})
                        else:
                            daemon._dispatches[dispatch_id]["status"] = "send_failed"
                            self._json_response(502, {"dispatch_id": dispatch_id, "status": "send_failed", "error": result.get("error", "delivery failed")})
                    except Exception as e:
                        daemon._dispatches[dispatch_id]["status"] = "send_failed"
                        self._json_response(500, {"dispatch_id": dispatch_id, "error": str(e)})
                else:
                    self.send_response(404)
                    self.end_headers()

        ThreadingHTTPServer.allow_reuse_address = True
        for attempt in range(5):
            try:
                server = ThreadingHTTPServer(("0.0.0.0", 8855), Handler)
                break
            except OSError:
                if attempt < 4:
                    logger.warning(f"HTTP port 8855 busy, retry {attempt+1}/5...")
                    import time as _t; _t.sleep(2)
                else:
                    logger.warning("HTTP compat server failed to start -- NATS still works")
                    return
        Thread(target=server.serve_forever, daemon=True).start()
        logger.info("HTTP compat server on :8855 (health + broadcast + message)")


# ── CLI ──

async def cli_send(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    await daemon.send(args[0], "context", "CLI message", " ".join(args[1:]))
    await daemon.nc.close()

async def cli_broadcast(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    await daemon.send("all", "context", "Broadcast", " ".join(args))
    await daemon.nc.close()

async def cli_health(args):
    daemon = FleetNerveNATS()
    await daemon.connect()
    results = await daemon.broadcast_health()
    online = sum(1 for r in results if r["status"] == 200)
    print(f"\nFleet Health: {online}/{len(results)} online (via NATS)\n")
    for r in results:
        n = r["node"]
        if r["status"] == 200:
            d = r["data"]
            print(f"  OK {n}: sessions={d.get('activeSessions', '?')} uptime={d.get('uptime_s', '?')}s")
            print(f"    git: {d.get('git', {}).get('commit', '?')[:50]}")
        else:
            print(f"  FAIL {n}: down")
    print()
    await daemon.nc.close()

async def cli_monitor(args):
    """Live fleet monitor -- prints events as they arrive."""
    nc = await nats.connect(NATS_URL, name=f"{NODE_ID}-monitor")
    print(f"Fleet Monitor -- connected to {NATS_URL}")
    print(f"Watching fleet.> ...\n")

    async def on_msg(msg):
        try:
            data = json.loads(msg.data.decode())
            ts = datetime.now().strftime("%H:%M:%S")
            node = data.get("from", data.get("node", "?"))
            if "heartbeat" in msg.subject:
                sessions = data.get("sessions", 0)
                print(f"  {ts} ♥ {node} (sessions={sessions})")
            else:
                payload = data.get("payload", {})
                subj = payload.get("subject", msg.subject)
                print(f"  {ts} ◉ [{msg.subject}] {node}: {subj}")
        except Exception:
            print(f"  {msg.subject}: {msg.data[:100]}")

    await nc.subscribe("fleet.>", cb=on_msg)
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        await nc.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "serve":
        daemon = FleetNerveNATS()
        asyncio.run(daemon.serve())
    elif sys.argv[1] == "send" and len(sys.argv) >= 4:
        asyncio.run(cli_send(sys.argv[2:]))
    elif sys.argv[1] == "broadcast" and len(sys.argv) >= 3:
        asyncio.run(cli_broadcast(sys.argv[2:]))
    elif sys.argv[1] == "health":
        asyncio.run(cli_health(sys.argv[2:]))
    elif sys.argv[1] == "monitor":
        asyncio.run(cli_monitor(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
