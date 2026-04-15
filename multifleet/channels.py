"""
Channel state tracking and communication protocol for fleet messaging.

Extracted from nats_client.py — contains:
- Channel priority constants (P0-P8)
- ChannelState: per-peer, per-channel health tracking
- CommunicationProtocol: channel cascade with self-healing
"""

from __future__ import annotations

import asyncio
import json
import logging
import math as _math
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .nats_client import FleetNerveNATS

logger = logging.getLogger("fleet_nats")

SEED_DIR = "/tmp"

# ── Channel Priority Constants ──
ALL_CHANNELS = ("P0_discord", "P1_nats", "P2_http", "P2t_tailscale", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git")
HEAL_SCORE_THRESHOLD = 5  # Score below this triggers automatic repair escalation

CHANNEL_P0_DISCORD = "P0_discord"
CHANNEL_P0_CLOUD = "P0_cloud"
CHANNEL_P1_NATS = "P1_nats"
CHANNEL_P2_HTTP = "P2_http"
CHANNEL_P2T_TAILSCALE = "P2t_tailscale"
CHANNEL_P3_CHIEF = "P3_chief"
CHANNEL_P4_SEED = "P4_seed"
CHANNEL_P5_SSH = "P5_ssh"
CHANNEL_P6_WOL = "P6_wol"
CHANNEL_P7_GIT = "P7_git"
CHANNEL_P8_KEYSTROKE = "P8_keystroke"

CHANNEL_ORDER = [
    CHANNEL_P0_DISCORD, CHANNEL_P1_NATS, CHANNEL_P2_HTTP,
    CHANNEL_P2T_TAILSCALE, CHANNEL_P3_CHIEF, CHANNEL_P4_SEED,
    CHANNEL_P5_SSH, CHANNEL_P6_WOL, CHANNEL_P7_GIT,
    CHANNEL_P8_KEYSTROKE,
]

# Per-channel timeout (30s max each, but tuned per channel for efficiency)
CHANNEL_TIMEOUT_S = {
    CHANNEL_P0_DISCORD: 10,
    CHANNEL_P0_CLOUD: 30,
    CHANNEL_P1_NATS: 30,
    CHANNEL_P2_HTTP: 30,
    CHANNEL_P2T_TAILSCALE: 30,
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


class CommunicationProtocol:
    """Invariant self-healing communication protocol.

    Enforces:
    - Channel cascade P0-P8 with 30s timeout per channel
    - Standing self-heal loop (every 60s) that probes peers and auto-repairs
    - Channel state tracking per peer, exposed via get_channel_state()
    - Secrets protection: never logs message content, API keys, or auth tokens
    - P8 (direct user text input) as absolute last resort, rate-limited 1/30s
    """

    def __init__(self, nerve: FleetNerveNATS):
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
        channels.append((CHANNEL_P0_DISCORD, self._nerve._send_p0_discord))
        if use_cloud:
            channels.append((CHANNEL_P0_CLOUD, self._nerve._send_p0_cloud))
        channels.extend([
            (CHANNEL_P1_NATS, self._nerve._send_p1_nats),
            (CHANNEL_P2_HTTP, self._nerve._send_p2_http),
            (CHANNEL_P2T_TAILSCALE, self._nerve._send_p2t_tailscale),
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
                    if name not in (CHANNEL_P0_DISCORD, CHANNEL_P0_CLOUD, CHANNEL_P1_NATS, CHANNEL_P2_HTTP, CHANNEL_P2T_TAILSCALE):
                        broken = [e.split(":")[0] for e in errors if "skipped" not in e]
                        if broken:
                            asyncio.ensure_future(self._trigger_heal(target, name, broken))
                    return {
                        "delivered": True, "channel": name,
                        "elapsed_ms": elapsed_ms, **result,
                    }
                if result.get("wol_sent"):
                    # WoL is fire-and-forget — sending the packet IS success.
                    # Do NOT record_failure here — it poisons channel state and
                    # causes P6 to be skipped as "known broken" on future attempts.
                    logger.info(f"[{name}] WoL packet sent to {target} ({elapsed_ms}ms)")
                    errors.append(f"{name}: WoL sent (fire-and-forget)")
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
        - Skips vacation nodes and backed-off idle nodes (flood prevention)
        """
        all_peers = set(self._nerve._peers_config.keys()) - {self._nerve.node_id}
        if not all_peers:
            return

        # Filter out vacation/backed-off nodes to prevent flood
        healer = getattr(self._nerve, '_healer', None)
        if healer:
            all_peers = {p for p in all_peers
                         if not healer.is_on_vacation(p)
                         and not healer._should_skip_repair(p)}

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
        except Exception:
            elapsed_ms = int((time.time() - t0) * 1000)
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
                CHANNEL_P0_DISCORD: self._nerve._send_p0_discord,
                CHANNEL_P1_NATS: self._nerve._send_p1_nats,
                CHANNEL_P2_HTTP: self._nerve._send_p2_http,
                CHANNEL_P2T_TAILSCALE: self._nerve._send_p2t_tailscale,
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
            except Exception:
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
