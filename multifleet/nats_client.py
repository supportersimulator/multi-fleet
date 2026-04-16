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
import logging.handlers
import os
import random
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Handle import for both pip install locations
try:
    import nats
    from nats.errors import ConnectionClosedError, TimeoutError as NatsTimeout
except ImportError:
    print("ERROR: nats-py not installed. Run: pip3 install nats-py")
    sys.exit(1)

from ._paths import REPO_ROOT  # noqa: E402 — must be before tools import

# Session historian NATS (standalone module)
try:
    from tools import fleet_historian_nats
except ImportError:
    fleet_historian_nats = None  # type: ignore[assignment]

logger = logging.getLogger("fleet_nats")

# ── Log rotation: 10MB max, 3 backups ──
_log_file = os.environ.get("FLEET_NATS_LOG", "/tmp/fleet-nats.log")
_log_formatter = logging.Formatter(
    "%(asctime)s [fleet-nats/%(levelname)s] %(message)s", datefmt="%H:%M:%S"
)
_rotating_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=10 * 1024 * 1024, backupCount=3
)
_rotating_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_rotating_handler, _console_handler])

# ── Config ──
NODE_ID = os.environ.get("MULTIFLEET_NODE_ID") or socket.gethostname().split(".")[0]
def _default_nats_url():
    """Resolve NATS URL from config or env.

    Priority: NATS_URL env > tunnel detection > config file > loopback.
    If an SSH tunnel is forwarding port 4222 locally, use 127.0.0.1 to avoid
    router-blocked direct connections.
    """
    # Check if NATS is running locally (direct server or SSH tunnel)
    try:
        import subprocess as _sp
        out = _sp.check_output(["lsof", "-i", ":4222", "-sTCP:LISTEN"], text=True, timeout=3, stderr=_sp.DEVNULL)
        if out.strip():
            # NATS listening locally — always use loopback (fastest, no NAT issues)
            return "nats://127.0.0.1:4222"
    except Exception as e:
        logger.debug("Local NATS detection for port 4222 failed: %s", e)
        pass

    # Try multifleet config (authoritative, scales to 100+)
    mf_conf = REPO_ROOT / ".multifleet" / "config.json"
    if mf_conf.exists():
        try:
            import json as _json
            c = _json.loads(mf_conf.read_text())
            chief_host = c.get("chief", {}).get("host", "")
            if chief_host:
                return f"nats://{chief_host}:4222"
        except Exception as e:
            logger.debug("Multifleet config load for NATS URL failed, using default: %s", e)
            pass

    return "nats://localhost:4222"

NATS_URL = os.environ.get("NATS_URL") or _default_nats_url()

def _cluster_server_urls() -> list[str]:
    """Get all NATS server URLs for clustered failover.

    When NATS clustering is configured, clients connect to ALL servers.
    If any server goes down, the client automatically fails over.
    Falls back to single-server NATS_URL if clustering unavailable.
    """
    try:
        from .nats_cluster import client_urls
        urls = client_urls()
        if len(urls) > 1:
            logger.info("NATS cluster mode: %d servers (%s)", len(urls), ", ".join(urls))
            return urls
    except Exception:
        pass
    return [NATS_URL]
SEED_DIR = "/tmp"
STREAM_NAME = "FLEET"
# ── Channel constants and classes (extracted to channels.py) ──
from .channels import (  # noqa: E402
    ALL_CHANNELS,
    HEAL_SCORE_THRESHOLD,
    CHANNEL_P0_CLOUD,
    CHANNEL_P1_NATS,
    CHANNEL_P2_HTTP,
    CHANNEL_P3_CHIEF,
    CHANNEL_P4_SEED,
    CHANNEL_P5_SSH,
    CHANNEL_P6_WOL,
    CHANNEL_P7_GIT,
    CHANNEL_P8_KEYSTROKE,
    CHANNEL_ORDER,
    CHANNEL_TIMEOUT_S,
    SELF_HEAL_INTERVAL_S,
    SELF_HEAL_MAX_PROBES_PER_CYCLE,
    REPAIR_COOLDOWN_S,
    BROADCAST_PARALLEL_LIMIT,
    BROADCAST_HTTP_TIMEOUT_S,
    GIT_BROADCAST_COOLDOWN_S,
    GOSSIP_FANOUT,
    GOSSIP_TTL,
    UDP_GOSSIP_PORT,
    ChannelState,
    CommunicationProtocol,
)
from .discovery import FleetDiscoveryManager  # noqa: E402
from .self_heal import SelfHealManager  # noqa: E402
from .sessions import SessionManager, normalize_session_id  # noqa: E402


# ── Subjects ──
def _sub_direct(node: str) -> str:
    return f"fleet.{node}.>"

def _sub_all() -> str:
    return "fleet.all.>"

def _sub_heartbeat() -> str:
    return "fleet.heartbeat"

def _sub_ack(node: str) -> str:
    return f"fleet.{node}.ack"


# ── Secret sanitization for git-committed payloads ──
_SENSITIVE_KEYS = {"secret", "token", "password", "key", "api_key", "credential", "auth"}
_IP_PATTERN = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _sanitize_for_git(obj: Any, _depth: int = 0) -> Any:
    """Strip secrets and IPs from a message before git commit.

    - Keys containing sensitive words → value replaced with "***"
    - IP addresses in string values → replaced with "<redacted-ip>"
    - Preserves node_id, subject, type, id, timestamp (fleet-useful fields)
    """
    if _depth > 10:
        return "..."
    if isinstance(obj, dict):
        sanitized = {}
        for k, v in obj.items():
            k_lower = k.lower()
            if any(s in k_lower for s in _SENSITIVE_KEYS):
                sanitized[k] = "***"
            else:
                sanitized[k] = _sanitize_for_git(v, _depth + 1)
        return sanitized
    if isinstance(obj, list):
        return [_sanitize_for_git(item, _depth + 1) for item in obj]
    if isinstance(obj, str):
        return _IP_PATTERN.sub("<redacted-ip>", obj)
    return obj


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
        self._active_subs: list = []  # Track subs for reconnect dedup
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
        except Exception:
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
        # Self-healing, watchdog, and repair escalation
        self._healer = SelfHealManager(self)
        # Discovery, gossip, and heartbeat
        self._disco = FleetDiscoveryManager(self)
        # Session management (injection, detection, notifications)
        self._sessions = SessionManager(self)

        # ── Orphan module wiring (initialized here, connected in serve()) ──

        # Ghost Pulse — background epistemic health monitor
        self._ghost_pulse = None
        self._ghost_pulse_counter = 0  # tick every 30th heartbeat (~5 min)
        try:
            from .ghost_pulse import GhostPulse
            self._ghost_pulse = GhostPulse(repo_root=str(REPO_ROOT))
            logger.info("GhostPulse initialized")
        except Exception as e:
            logger.warning(f"GhostPulse init failed: {e}")

        # Ghost Digest — session evidence digestion
        self._ghost_digest = None
        try:
            from .ghost_digest import GhostDigest
            self._ghost_digest = GhostDigest(node_id=self.node_id)
            logger.info("GhostDigest initialized")
        except Exception as e:
            logger.warning(f"GhostDigest init failed: {e}")

        # Evidence Sync — cross-node evidence sharing (periodic)
        self._evidence_sync = None
        self._evidence_sync_counter = 0  # tick every 30th heartbeat (~5 min)
        try:
            from .evidence_sync import EvidenceSync
            self._evidence_sync = EvidenceSync(node_id=self.node_id)
            logger.info("EvidenceSync initialized")
        except Exception as e:
            logger.warning(f"EvidenceSync init failed: {e}")

        # Supervisor — singleton instance shared across daemon
        self._supervisor = None
        try:
            from .supervisor_store import PersistentSupervisor
            self._supervisor = PersistentSupervisor(node_id=self.node_id)
            logger.info("PersistentSupervisor initialized (singleton)")
        except ImportError:
            from .supervisor import Supervisor
            self._supervisor = Supervisor(node_id=self.node_id)
            logger.info("Supervisor initialized (in-memory singleton)")
        except Exception as e:
            logger.warning(f"Supervisor init failed: {e}")

        # Supervisor Watchdog — detects stalled supervisor phases
        self._supervisor_watchdog = None
        if self._supervisor:
            try:
                from .supervisor_watchdog import SupervisorWatchdog
                self._supervisor_watchdog = SupervisorWatchdog(self._supervisor)
                logger.info("SupervisorWatchdog initialized")
            except Exception as e:
                logger.warning(f"SupervisorWatchdog init failed: {e}")

        # Chief Election — heartbeat + majority vote leader election
        self._chief_election = None
        try:
            from .chief_election import ChiefElection
            self._chief_election = ChiefElection(
                node_id=self.node_id,
                fleet_size=len(self._peers_config) + 1 if self._peers_config else 3,
            )
            # Seed with configured chief if available
            if self._chief:
                self._chief_election.current_chief = self._chief
            logger.info("ChiefElection initialized (chief=%s)", self._chief)
        except Exception as e:
            logger.warning(f"ChiefElection init failed: {e}")

        # Dashboard Event Consumer — live state from EventBus (attached in serve())
        self._dashboard_events = None
        try:
            from .dashboard_events import DashboardEventConsumer
            self._dashboard_events = DashboardEventConsumer()
            logger.info("DashboardEventConsumer initialized (bus attached in serve())")
        except Exception as e:
            logger.warning(f"DashboardEventConsumer init failed: {e}")

        # IDE Bridge — translates fleet events for VS Code
        self._ide_bridge = None
        try:
            from .ide_bridge import IDEBridge
            self._ide_bridge = IDEBridge()
            logger.info("IDEBridge initialized")
        except Exception as e:
            logger.warning(f"IDEBridge init failed: {e}")

        # Data Layer — centralized SQLite connection pool
        self._data_layer = None
        try:
            from .data_layer import FleetDataLayer
            self._data_layer = FleetDataLayer()
            logger.info("FleetDataLayer initialized")
        except Exception as e:
            logger.warning(f"FleetDataLayer init failed: {e}")

        # Backup — fleet state snapshots (triggered on shutdown + HTTP)
        self._backup = None
        try:
            from .backup import FleetBackup
            self._backup = FleetBackup()
            logger.info("FleetBackup initialized")
        except Exception as e:
            logger.warning(f"FleetBackup init failed: {e}")

        # Benchmark — fleet performance measurement (on-demand via HTTP)
        self._benchmark = None
        try:
            from .benchmark import FleetBenchmark
            self._benchmark = FleetBenchmark()
            logger.info("FleetBenchmark initialized")
        except Exception as e:
            logger.warning(f"FleetBenchmark init failed: {e}")

        # EventBus adapter (publishes fleet.* events to IDE/3-surgeons EventBus)
        self._eventbus = None
        try:
            from .eventbus import FleetEventBus
            self._eventbus = FleetEventBus(node_id=self.node_id)
            logger.info("FleetEventBus adapter initialized")
        except Exception as e:
            logger.warning(f"EventBus adapter init failed: {e} — IDE events disabled")

        # EventBus WebSocket server (initialized in serve(), None until then)
        self._event_bus_server = None
        self._core_event_bus = None

        # Task Orchestrator — unified lifecycle from idea to wisdom
        self._task_orchestrator = None
        try:
            from .task_orchestrator import TaskOrchestrator
            self._task_orchestrator = TaskOrchestrator(
                daemon_port=self._http_port if hasattr(self, '_http_port') else 8855,
                node_id=self.node_id,
            )
            logger.info("TaskOrchestrator initialized")
        except Exception as e:
            logger.warning(f"TaskOrchestrator init failed: {e}")

        # Chief Synthesis — aggregates verdicts and produces merge decisions
        self._chief_synthesis = None
        try:
            from .synthesis import ChiefSynthesis
            self._chief_synthesis = ChiefSynthesis()
            logger.info("ChiefSynthesis initialized")
        except Exception as e:
            logger.warning(f"ChiefSynthesis init failed: {e}")

    async def connect(self):
        """Connect to NATS server(s) with auto-reconnect and cluster failover.

        CV-001: Supports token auth via NATS_TOKEN env var or config.
        If set, all connections are authenticated. If not, connects without
        auth (backward-compatible for LAN-only fleets).

        Cluster mode: connects to all fleet NATS servers. If any server
        goes down, the client automatically fails over to another.
        """
        # Multi-seed: connect to all cluster servers for failover
        servers = _cluster_server_urls()
        if len(servers) == 1 and servers[0] != self.nats_url:
            servers = [self.nats_url]
        connect_kwargs = dict(
            servers=servers,
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
            except Exception:
                pass
        if nats_token:
            connect_kwargs["token"] = nats_token
            logger.info("NATS auth: using token")
        else:
            logger.warning("NATS auth: no token configured (NATS_TOKEN env or config.protocol.natsToken) -- LAN-only mode")

        self.nc = await nats.connect(**connect_kwargs)
        connected_url = getattr(self.nc, 'connected_url', self.nats_url) or self.nats_url
        logger.info(f"Connected to NATS at {connected_url} as {self.node_id} ({len(servers)} server(s) configured)")

        # Set up JetStream for persistent messaging
        self.js = self.nc.jetstream()
        try:
            await self.js.find_stream_name_by_subject("fleet.>")
            logger.info("JetStream stream FLEET found")
        except Exception:
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
        logger.info("Reconnected to NATS — re-subscribing to all subjects")
        try:
            await self._subscribe_all()
            logger.info("Re-subscribed to all NATS subjects after reconnect")
        except Exception as e:
            logger.error(f"Failed to re-subscribe after reconnect: {e}")
            self._stats["errors"] += 1

    async def _on_disconnect(self):
        logger.warning(f"Disconnected from NATS at {datetime.now(timezone.utc).isoformat()} -- will auto-reconnect")

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

            # Sanitize incoming message body (defense against prompt injection + credential leaks)
            try:
                from multifleet.sanitize import sanitize_message
                body = sanitize_message(body)
                payload["body"] = body
            except (ImportError, Exception):
                pass  # Graceful degradation — sanitize not available

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

                        # SECURITY: claude -p spawning DISABLED (commit 1fb2a95c).
                        # Fleet-sourced data must NEVER be passed to `claude -p` as it
                        # enables prompt injection from any node on the NATS bus.
                        # Repair actions are handled by SAFE_ACTIONS dict above.
                        pass

                    elif now - first_seen > 120:
                        # 2 min passed, peer hasn't fixed it -- escalate to L3 (SSH fix)
                        logger.info(f"2min timeout: {reporter} still broken. Escalating to SSH repair.")
                        self._repair_cooldowns[cooldown_key] = now
                        self._repair_notified[cooldown_key] = 0  # reset for next cycle
                        # SSH repair attempt
                        peer = self._resolve_peer(reporter)
                        ip = peer.get("ip")
                        user = peer.get("user") or os.environ.get("USER", "")
                        if ip:
                            asyncio.create_task(self._ssh_repair_peer(ip, user, action))
                    # else: between first notification and 2min -- silent wait
                except Exception as e:
                    logger.warning(f"Repair handler error: {e}")
                return

            # Handle fleet_assist requests (cross-node repair)
            if msg_type == "fleet_assist" or subject == "assist-request":
                try:
                    if hasattr(self, '_healer') and self._healer:
                        asyncio.create_task(self._healer._handle_fleet_assist(data))
                except Exception as e:
                    logger.debug(f"Fleet assist handler error: {e}")
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
            elif priority == "seed_write":
                # Remote P4: another node asks us to write a seed file locally
                seed_path = self._resolve_seed_path(data)
                seed_text = f"# Fleet: {subject}\nFrom: {sender}\n\n{body}\n"
                Path(seed_path).write_text(seed_text)
                logger.info(f"P4 seed_write from {sender} -> {seed_path}")
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
        return await self._disco.handle_heartbeat(msg)

    # ── Self-healing / Repair — delegated to SelfHealManager ──

    async def _ssh_repair_peer(self, ip: str, user: str, action: str):
        return await self._healer._ssh_repair_peer(ip, user, action)

    async def _handle_ack(self, msg):
        """Process inbound ACK -- mark pending message as delivered."""
        try:
            data = json.loads(msg.data.decode())
            ref = data.get("ref")
            if ref in self._pending_acks:
                self._pending_acks[ref]["acked"] = True
                logger.info(f"ACK: {ref[:8]} from {data.get('node','?')} via {data.get('channel','?')}")
        except Exception:
            pass

    async def _send_ack(self, sender: str, msg_id: str, status: str, channel: str):
        """Publish ACK back to the original sender."""
        if not self.nc or not self.nc.is_connected or sender == self.node_id:
            return
        ack = {"ref": msg_id, "status": status, "node": self.node_id, "channel": channel}
        try:
            await self.nc.publish(_sub_ack(sender), json.dumps(ack).encode())
        except Exception:
            pass

    # ── Injection (last mile) — delegated to SessionManager ──

    def _format_resume_message(self, data: dict) -> str:
        return self._sessions._format_resume_message(data)

    def _get_active_session_full_id(self) -> Optional[str]:
        return self._sessions._get_active_session_full_id()

    def _inject_via_resume(self, session_id: str, message: str) -> bool:
        return self._sessions._inject_via_resume(session_id, message)

    def _resolve_seed_path(self, data: dict) -> str:
        return self._sessions._resolve_seed_path(data)

    def _inject_context(self, data: dict):
        self._sessions._inject_context(data)

    def _inject_seed(self, data: dict):
        self._sessions._inject_seed(data)

    def _keystroke_trigger(self, summary: str):
        self._sessions._keystroke_trigger(summary)

    def _inject_alert(self, data: dict):
        self._sessions._inject_alert(data)

    def _inject_task(self, data: dict):
        self._sessions._inject_task(data)

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

    # ── Memory Cleanup (30-day survival) ──

    def _cleanup_completed(self, max_age_s: int = 86400):
        """Remove terminal _chains and _dispatches older than max_age_s (default 24h).

        Terminal statuses: done, failed, completed, error.
        Called hourly from the watchdog loop to prevent unbounded memory growth.
        """
        now = time.time()
        terminal = {"done", "failed", "completed", "error"}

        stale_chains = [
            cid for cid, c in self._chains.items()
            if c.get("status") in terminal
            and (now - c.get("created_at", now)) > max_age_s
        ]
        for cid in stale_chains:
            del self._chains[cid]
        if stale_chains:
            logger.info(f"Cleanup: evicted {len(stale_chains)} completed chains")

        stale_dispatches = [
            did for did, d in self._dispatches.items()
            if d.get("status") in terminal
            and (now - d.get("completed_at", d.get("sent_at", now))) > max_age_s
        ]
        for did in stale_dispatches:
            del self._dispatches[did]
        if stale_dispatches:
            logger.info(f"Cleanup: evicted {len(stale_dispatches)} completed dispatches")

        return {"chains_evicted": len(stale_chains), "dispatches_evicted": len(stale_dispatches)}

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
        self._sessions._notify(message)

    def _find_claude(self) -> Optional[str]:
        return self._sessions._find_claude()

    # ── Health ──

    def _build_health(self) -> dict:
        """Build health response with VS Code state and session idle times."""
        from multifleet.protocol import PROTOCOL_VERSION
        git = self._get_git_context()
        sessions = self._detect_sessions()
        return {
            "nodeId": self.node_id,
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
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
                    "protocol_version": v.get("protocol_version"),
                    "version_status": v.get("_version_status", "unknown"),
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
        except Exception:
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
            except Exception:
                pass
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
            except Exception:
                pass
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
        except Exception:
            return {"branch": "unknown", "commit": "unknown"}

    def _detect_vscode_state(self) -> str:
        return self._sessions._detect_vscode_state()

    def _detect_sessions(self) -> list:
        return self._sessions._detect_sessions()

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
        """Resolve peer connection info from heartbeats (dynamic) merged with config (static).

        Heartbeat provides live IP/user. Config provides static fields like tunnel_port.
        Both are merged so tunnel fallback works even when heartbeats are active.
        """
        config = self._peers_config.get(target, {})
        hb = self._peers.get(target, {})
        if hb:
            resolved = dict(config)  # start with static config (tunnel_port, etc.)
            resolved.update({"ip": hb.get("ip", ""), "user": hb.get("user", ""), "mac_address": hb.get("mac", "")})
            return resolved
        return config

    # ── Local-first repair actions (Protocol: fix yourself before others SSH in) ──

    async def _subscribe_all(self):
        """Subscribe to all fleet NATS subjects. Called on connect and reconnect.

        Tracks subscription objects in self._active_subs. On reconnect,
        existing subs are unsubscribed first to prevent duplicate delivery.
        """
        # Unsubscribe prior subs to prevent duplicates on reconnect
        for sub in self._active_subs:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        self._active_subs.clear()

        self._active_subs.append(await self.nc.subscribe(_sub_direct(self.node_id), cb=self._handle_message))
        self._active_subs.append(await self.nc.subscribe(_sub_all(), cb=self._handle_message))
        self._active_subs.append(await self.nc.subscribe(_sub_heartbeat(), cb=self._handle_heartbeat))
        self._active_subs.append(await self.nc.subscribe(_sub_ack(self.node_id), cb=self._handle_ack))
        # Session historian subscription (fleet.sessions.> wildcard)
        if fleet_historian_nats:
            sub, cb = fleet_historian_nats.subscribe_sessions(self)
            self._active_subs.append(await self.nc.subscribe(sub, cb=cb))
            logger.info(
                f"NATS subscribed: fleet.{self.node_id}.> + fleet.all.> + "
                f"fleet.heartbeat + ack + fleet.sessions.>"
            )
        else:
            logger.info(f"NATS subscribed: fleet.{self.node_id}.> + fleet.all.> + fleet.heartbeat + ack")

    async def _repair_restart_nats(self, heal: dict):
        return await self._healer._repair_restart_nats(heal)

    async def _repair_restart_tunnel(self, heal: dict):
        return await self._healer._repair_restart_tunnel(heal)

    async def _repair_git_pull(self, heal: dict):
        return await self._healer._repair_git_pull(heal)

    async def _repair_kill_zombie(self, heal: dict):
        return await self._healer._repair_kill_zombie(heal)

    async def _repair_restart_daemon(self, heal: dict):
        return await self._healer._repair_restart_daemon(heal)

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
            except Exception:
                continue
        return {"delivered": False}

    async def _send_p2t_tailscale(self, target: str, message: dict) -> dict:
        """P2t: HTTP direct via Tailscale overlay network. Cross-network connectivity."""
        peer = self._resolve_peer(target)
        try:
            from multifleet.config import resolve_tailscale_ip
            ts_ip = resolve_tailscale_ip(peer, target)
        except (ImportError, Exception):
            ts_ip = peer.get("tailscale_ip", "")
        if not ts_ip:
            return {"delivered": False}
        import urllib.request
        payload = json.dumps(message).encode()
        url = f"http://{ts_ip}:8855/message"
        try:
            req = urllib.request.Request(url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST")
            resp = urllib.request.urlopen(req, timeout=5)
            return {"delivered": True, "url": url, "channel": "P2t_tailscale"}
        except Exception:
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
            except Exception:
                continue
        return {"delivered": False}

    async def _send_p4_seed(self, target: str, message: dict) -> dict:
        """P4: Seed file -- local (session-aware) or via NATS/SSH.

        Local: resolves to per-session seed file when sessions are detected.
        Remote: tries NATS seed_write request first, then falls through to SSH.
        """
        payload = message.get("payload", {})
        text = f"# Fleet: {payload.get('subject','')}\nFrom: {message.get('from','?')}\n\n{payload.get('body','')}\n"
        if target == self.node_id:
            seed_path = self._resolve_seed_path(message)
            Path(seed_path).write_text(text)
            return {"delivered": True, "seedPath": seed_path}
        # Remote: try NATS-based seed write (works even when SSH is down)
        if self.nc and self.nc.is_connected:
            try:
                seed_msg = {
                    "id": str(uuid.uuid4()),
                    "type": "seed_write",
                    "from": self.node_id,
                    "to": target,
                    "payload": payload,
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                }
                nats_subject = f"fleet.{target}.message"
                await self.nc.publish(nats_subject, json.dumps(seed_msg).encode())
                logger.info(f"P4 seed via NATS to {target}")
                return {"delivered": True, "method": "seed_via_nats"}
            except Exception as e:
                logger.debug(f"P4 NATS seed failed: {e}")
        # Fallback: SSH-based seed (handled by P5 cascade)
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
        except Exception:
            pass
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
            except Exception:
                pass
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
        except Exception:
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
            sanitized = _sanitize_for_git(message)
            msg_file.write_text(json.dumps(sanitized, indent=2))
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

    async def _send_p0_discord(self, target: str, message: dict) -> dict:
        """P0: Discord channel — highest priority, tried before all others.

        Uses the DiscordFleetBridge from discord_bridge module. Falls through
        gracefully when Discord is not configured or bridge is not connected.
        """
        try:
            from .discord_bridge import DiscordFleetBridge, get_bot_token, validate_bot_token
        except ImportError:
            return {"delivered": False, "error": "discord_bridge not available"}

        # Check if bridge is attached to this nerve instance
        bridge = getattr(self, "_discord_bridge", None)
        if bridge is None:
            return {"delivered": False, "error": "Discord bridge not attached"}

        try:
            result = bridge.send_to_discord(message)
            if result.get("sent"):
                return {"delivered": True, "method": "discord"}
            if result.get("queued"):
                return {"delivered": False, "error": "queued (bridge disconnected)"}
            return {"delivered": False, "error": "Discord send failed"}
        except Exception as e:
            return {"delivered": False, "error": f"discord send failed: {e}"}

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
                full_sid = chosen.get('sessionId', '?')
                session_target = full_sid[:12]  # display only
                message['_target_session'] = full_sid
                routing = 'task_to_idle_session'
            else:
                routing = 'task_no_idle'

        elif msg_type in ('context', 'reply'):
            if sessions:
                primary = min(sessions, key=lambda s: s.get('idle_s', 9999))
                full_sid = primary.get('sessionId', '?')
                session_target = full_sid[:12]  # display only
                message['_target_session'] = full_sid
                routing = 'context_to_primary'

        result = await self.send_with_fallback(target, message)
        result.update(routing=routing, session_target=session_target,
                      elapsed_s=round(time.time() - t0, 2))
        logger.info(f'send_smart: {target} type={msg_type} routing={routing} '
                     f'session={session_target} delivered={result.get("delivered")}')
        return result

    def _update_channel_health(self, target: str, channel: str, status: str):
        self._healer._update_channel_health(target, channel, status)

    def _get_peer_health_score(self, target: str) -> tuple:
        return self._healer._get_peer_health_score(target)

    def _attempt_self_heal(self, target: str, working_channel: str, broken: list):
        self._healer._attempt_self_heal(target, working_channel, broken)

    async def _verify_channel(self, target: str, channel: str) -> bool:
        return await self._healer._verify_channel(target, channel)

    async def repair_with_escalation(self, target: str, broken_channels: list, action: str = "restart_nats"):
        return await self._healer.repair_with_escalation(target, broken_channels, action)

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
            except Exception:
                break

        await sub.unsubscribe()
        results.sort(key=lambda r: r["node"])
        return results

    # ── Heartbeat / Discovery / Gossip — delegated to FleetDiscoveryManager ──

    async def _heartbeat_loop(self):
        return await self._disco.heartbeat_loop()

    def _get_local_ip(self) -> str:
        return self._disco.get_local_ip()

    def _get_known_peer_ips(self) -> dict[str, str]:
        return self._disco.get_known_peer_ips()

    def _select_gossip_targets(self, exclude_nodes: set[str] | None = None) -> list[tuple[str, str]]:
        return self._disco.select_gossip_targets(exclude_nodes=exclude_nodes)

    async def _udp_gossip_loop(self):
        return await self._disco.udp_gossip_loop()

    async def _udp_gossip_listener(self):
        return await self._disco.udp_gossip_listener()

    # ── Proactive Fleet Watchdog — delegated to SelfHealManager ──

    async def _watchdog_loop(self):
        return await self._healer._watchdog_loop()

    def _touch_activity(self):
        """Mark that NATS activity occurred (call on send/receive)."""
        if hasattr(self, "_last_activity_at"):
            self._last_activity_at = time.time()

    async def _watchdog_check_idle(self):
        return await self._healer._watchdog_check_idle()

    async def _productive_idle_scan(self):
        return await self._healer._productive_idle_scan()

    async def _watchdog_check_peers(self):
        return await self._healer._watchdog_check_peers()

    async def _watchdog_check_channel_health(self):
        return await self._healer._watchdog_check_channel_health()

    async def _watchdog_check_pending_acks(self):
        return await self._healer._watchdog_check_pending_acks()

    async def _watchdog_check_zombies(self):
        return await self._healer._watchdog_check_zombies()

    async def _watchdog_check_git(self):
        return await self._healer._watchdog_check_git()

    async def _watchdog_check_tunnels(self):
        return await self._healer._watchdog_check_tunnels()

    # ── ACK Retry Loop — delegated to SelfHealManager ──

    async def _ack_retry_loop(self):
        return await self._healer._ack_retry_loop()

    # ── Memory Bridge Promotion Loop ──

    async def _memory_promotion_loop(self):
        """Run evidence→memory promotion every ~100s (10 heartbeat cycles)."""
        while self._running:
            try:
                await asyncio.sleep(100)
                if self._memory_bridge:
                    result = self._memory_bridge.run_promotion_cycle()
                    if result.get("promoted_count", 0) > 0:
                        logger.info("Memory promotion: %d entries promoted",
                                     result["promoted_count"])
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Memory promotion cycle failed: %s", e)

    async def _ghost_pulse_loop(self):
        """Run ghost pulse every ~300s for epistemic health maintenance."""
        while self._running:
            try:
                await asyncio.sleep(300)
                if self._ghost_pulse:
                    result = self._ghost_pulse.pulse()
                    logger.info("Ghost pulse: %s (%.0fms)", result.summary(), result.duration_ms)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Ghost pulse cycle failed: %s", e)

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

        # Start fleet health monitor (background thread)
        try:
            from .health_monitor import FleetHealthMonitor
            self._health_monitor = FleetHealthMonitor(
                daemon_port=self._http_port if hasattr(self, '_http_port') else 8855,
                node_id=self.node_id,
            )
            if self._eventbus:
                self._health_monitor.wire_eventbus(self._eventbus)
            self._health_monitor.start_background()
            logger.info("Fleet health monitor started (interval=30s)")
        except Exception as e:
            logger.warning("Health monitor failed to start: %s", e)

        # Wire chief election into health monitor (auto-triggers on chief timeout)
        if self._chief_election and hasattr(self, '_health_monitor') and self._health_monitor:
            try:
                self._chief_election.wire_to_health_monitor(self._health_monitor)
                logger.info("ChiefElection wired to health monitor")
            except Exception as e:
                logger.warning("ChiefElection wiring failed: %s", e)

        # Start memory bridge promotion loop (every ~100s)
        try:
            from .memory_bridge import MemoryBridge
            from .evidence_ledger import EvidenceLedger
            self._memory_bridge = MemoryBridge(
                evidence_ledger=EvidenceLedger(),
                eventbus=self._eventbus,
            )
            asyncio.create_task(self._memory_promotion_loop())
            logger.info("Memory bridge started (promotion every ~100s)")
        except Exception as e:
            logger.warning("Memory bridge failed to start: %s", e)
            self._memory_bridge = None

        # Start EventBus WebSocket server (port 8031) — bridges fleet/IDE events
        try:
            from .event_bus_server import EventBus as CoreEventBus, EventBusServer
            self._core_event_bus = CoreEventBus()
            self._event_bus_server = EventBusServer(self._core_event_bus)
            await self._event_bus_server.start_background()
            # Wire FleetEventBus adapter to use in-process bus (faster than HTTP POST)
            if self._eventbus is not None:
                self._eventbus.set_core_bus(self._core_event_bus)
            logger.info("EventBus WebSocket server started on :8031")

            # Wire DashboardEventConsumer to core bus for live state
            if self._dashboard_events and self._core_event_bus:
                try:
                    self._dashboard_events.attach(self._core_event_bus)
                    logger.info("DashboardEventConsumer attached to EventBus")
                except Exception as e:
                    logger.warning(f"DashboardEventConsumer attach failed: {e}")

            # Wire IDEBridge as EventBus consumer for VS Code notifications
            if self._ide_bridge and self._core_event_bus:
                try:
                    self._core_event_bus.on("fleet.*", lambda evt: self._ide_bridge.translate_event(
                        evt.get("type", "unknown"), evt,
                    ))
                    logger.info("IDEBridge attached to EventBus")
                except Exception as e:
                    logger.warning(f"IDEBridge EventBus attach failed: {e}")

        except Exception as e:
            logger.warning(f"EventBus WebSocket server failed to start: {e} — IDE events via JSONL only")
            self._core_event_bus = None
            self._event_bus_server = None

        # Wire HealthMonitor into TaskOrchestrator (health-aware task scheduling)
        if self._task_orchestrator and hasattr(self, '_health_monitor'):
            try:
                self._task_orchestrator._health_monitor = self._health_monitor
                logger.info("HealthMonitor wired into TaskOrchestrator")
            except Exception as e:
                logger.warning(f"HealthMonitor→TaskOrchestrator wiring failed: {e}")

        # Wire EvidenceSync into TaskOrchestrator (evidence sharing after task completion)
        if self._task_orchestrator and self._evidence_sync:
            try:
                self._task_orchestrator._evidence_sync = self._evidence_sync
                logger.info("EvidenceSync wired into TaskOrchestrator")
            except Exception as e:
                logger.warning(f"EvidenceSync→TaskOrchestrator wiring failed: {e}")

        # Wire ChiefSynthesis into TaskOrchestrator (verdict aggregation)
        if self._task_orchestrator and self._chief_synthesis:
            try:
                self._task_orchestrator._chief_synthesis = self._chief_synthesis
                logger.info("ChiefSynthesis wired into TaskOrchestrator")
            except Exception as e:
                logger.warning(f"ChiefSynthesis→TaskOrchestrator wiring failed: {e}")

        # Start ghost pulse loop (epistemic health heartbeat, every ~300s)
        try:
            from multifleet.ghost_pulse import GhostPulse
            self._ghost_pulse = GhostPulse(repo_root=".", evidence_ledger_path=None)
            asyncio.create_task(self._ghost_pulse_loop())
            logger.info("Ghost pulse started (interval=300s)")
        except Exception as e:
            logger.warning("Ghost pulse failed to start: %s", e)
            self._ghost_pulse = None

        # Keep running
        try:
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            # Shutdown hooks: backup state, close data layer, then NATS
            if self._backup:
                try:
                    snapshot_path = self._backup.create_snapshot()
                    logger.info(f"Shutdown backup created: {snapshot_path}")
                except Exception as e:
                    logger.warning(f"Shutdown backup failed: {e}")
            if self._data_layer:
                try:
                    self._data_layer.close_all()
                    logger.info("FleetDataLayer connections closed")
                except Exception as e:
                    logger.warning(f"FleetDataLayer close failed: {e}")
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
                elif self.path == "/dashboard/data":
                    try:
                        from .dashboard_data import FleetDashboardData
                        dd = FleetDashboardData(daemon_port=8855)
                        self._json_response(200, dd.snapshot())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/theatrical":
                    try:
                        from .dashboard_data import FleetDashboardData
                        from .theatrical.theatrical_dashboard import TheatricalDashboard
                        dd = FleetDashboardData(daemon_port=8855)
                        td = TheatricalDashboard(dashboard_data=dd)
                        raw_html = td.render()
                        # Override auto-refresh to 30s
                        html_out = raw_html.replace(
                            'content="10"', 'content="30"', 1
                        )
                        data = html_out.encode("utf-8")
                    except Exception as e:
                        data = f"<html><body><h1>Theatrical Dashboard Error</h1><pre>{e}</pre></body></html>".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path == "/theatrical/json":
                    try:
                        from .dashboard_data import FleetDashboardData
                        from .theatrical.theatrical_dashboard import TheatricalDashboard
                        dd = FleetDashboardData(daemon_port=8855)
                        td = TheatricalDashboard(dashboard_data=dd)
                        self._json_response(200, td.gather_data())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
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
                elif self.path.startswith("/gains-gate"):
                    try:
                        from .gains_gate import FleetGainsGate
                        quick = "quick=1" in self.path or "quick=true" in self.path.lower()
                        gate = FleetGainsGate(daemon_port=8855)
                        result = gate.run(quick=quick)
                        self._json_response(200, result.to_dict())
                    except Exception as e:
                        self._json_response(500, {"error": str(e), "passed": False})
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
                elif self.path == "/wisdom":
                    try:
                        if hasattr(daemon, '_memory_bridge') and daemon._memory_bridge:
                            stats = daemon._memory_bridge.get_wisdom_stats()
                            self._json_response(200, stats)
                        else:
                            self._json_response(503, {"error": "memory bridge not started"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence" or self.path.startswith("/evidence?"):
                    try:
                        from .evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        # Parse query params
                        params = {}
                        if "?" in self.path:
                            from urllib.parse import parse_qs, urlparse
                            qs = parse_qs(urlparse(self.path).query)
                            if "event_type" in qs:
                                params["event_type"] = qs["event_type"][0]
                            if "node_id" in qs:
                                params["node_id"] = qs["node_id"][0]
                            if "since" in qs:
                                params["since"] = float(qs["since"][0])
                            if "limit" in qs:
                                params["limit"] = int(qs["limit"][0])
                        entries = ledger.query(**params)
                        self._json_response(200, {"entries": entries, "count": len(entries)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence/latest":
                    try:
                        from .evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        entries = ledger.latest(count=10)
                        self._json_response(200, {"entries": entries, "count": len(entries)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence/verify":
                    try:
                        from .evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        result = ledger.verify_chain()
                        self._json_response(200, result)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence/stats":
                    try:
                        from .evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        self._json_response(200, ledger.stats)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/races":
                    racer = self._get_race_manager()
                    if racer:
                        self._json_response(200, {"races": racer.list_races(), "count": len(racer.list_races())})
                    else:
                        self._json_response(503, {"error": "FleetRace not available"})
                elif self.path.startswith("/race/"):
                    racer = self._get_race_manager()
                    if not racer:
                        self._json_response(503, {"error": "FleetRace not available"})
                    else:
                        race_id = self.path.split("/")[2]
                        result = racer.status(race_id)
                        code = 404 if "error" in result else 200
                        self._json_response(code, result)
                elif self.path == "/truth" or self.path.startswith("/truth?"):
                    try:
                        from .truth_ladder import TruthLadder
                        ladder = TruthLadder()
                        params = {}
                        if "?" in self.path:
                            from urllib.parse import parse_qs, urlparse
                            qs = parse_qs(urlparse(self.path).query)
                            if "layer" in qs:
                                params["layer"] = qs["layer"][0]
                            if "limit" in qs:
                                params["limit"] = int(qs["limit"][0])
                        entries = ladder.query(**params)
                        self._json_response(200, {
                            "entries": [e.to_dict() for e in entries],
                            "count": len(entries),
                            "stats": ladder.stats,
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/truth/stats":
                    try:
                        from .truth_ladder import TruthLadder
                        self._json_response(200, TruthLadder().stats)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/supervisor" or self.path.startswith("/supervisor?"):
                    try:
                        sup = self._get_supervisor()
                        if not sup:
                            self._json_response(500, {"error": "Supervisor init failed"})
                            return
                        active = sup.list_active()
                        all_tasks = sup.list_all()
                        self._json_response(200, {
                            "tasks": [s.to_dict() for s in active],
                            "count": len(active),
                            "total": len(all_tasks),
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/overlays":
                    try:
                        from .node_overlay import NodeOverlayManager
                        mgr = self._get_overlay_manager()
                        overlays = mgr.all_overlays()
                        self._json_response(200, {
                            "overlays": [o.to_dict() for o in overlays],
                            "count": len(overlays),
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path.startswith("/overlay/"):
                    try:
                        from .node_overlay import NodeOverlayManager
                        node_id = self.path[9:]  # strip "/overlay/"
                        mgr = self._get_overlay_manager()
                        overlay = mgr.get(node_id)
                        if overlay:
                            self._json_response(200, overlay.to_dict())
                        else:
                            self._json_response(404, {"error": f"no overlay for node '{node_id}'"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/fleet/health":
                    try:
                        from .health_monitor import FleetHealthMonitor
                        if not hasattr(Handler, '_health_monitor') or Handler._health_monitor is None:
                            Handler._health_monitor = FleetHealthMonitor(
                                daemon_port=daemon._http_port if hasattr(daemon, '_http_port') else 8855,
                                node_id=daemon.node_id,
                            )
                        self._json_response(200, Handler._health_monitor.get_fleet_status())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/fleet/alerts":
                    try:
                        from .health_monitor import FleetHealthMonitor
                        if not hasattr(Handler, '_health_monitor') or Handler._health_monitor is None:
                            Handler._health_monitor = FleetHealthMonitor(
                                daemon_port=daemon._http_port if hasattr(daemon, '_http_port') else 8855,
                                node_id=daemon.node_id,
                            )
                        monitor = Handler._health_monitor
                        self._json_response(200, {
                            "alerts": monitor._alerts[-50:],
                            "count": len(monitor._alerts),
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/metrics":
                    # Prometheus text exposition — fleet-wide aggregated metrics
                    try:
                        from .fleet_metrics import FleetMetricsAggregator
                        fa = FleetMetricsAggregator(
                            daemon_port=daemon._http_port if hasattr(daemon, '_http_port') else 8855,
                        )
                        body = fa.to_prometheus().encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/metrics/json":
                    # JSON fleet metrics — aggregate + per-node breakdown
                    try:
                        from .fleet_metrics import FleetMetricsAggregator
                        fa = FleetMetricsAggregator(
                            daemon_port=daemon._http_port if hasattr(daemon, '_http_port') else 8855,
                        )
                        self._json_response(200, fa.aggregate())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/scan/results":
                    # Wire 6: Return latest probe results and update overlay
                    try:
                        from .probe_engine import ProbeRegistry
                        registry = ProbeRegistry()
                        results = registry.run_all()
                        result_dicts = [r.to_dict() if hasattr(r, 'to_dict') else r for r in results]
                        # Update overlay with probe results
                        mgr = self._get_overlay_manager()
                        mgr.update_from_probe(daemon.node_id, result_dicts)
                        self._json_response(200, {"results": result_dicts, "count": len(result_dicts)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path in ("/arbiter", "/arbiter/"):
                    try:
                        from .arbiter_view import build_arbiter_html
                        html = build_arbiter_html(daemon_port=daemon._http_port if hasattr(daemon, '_http_port') else 8855)
                        data = html.encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/arbiter/pending":
                    try:
                        from .arbiter_view import get_pending_decisions
                        pending = get_pending_decisions()
                        self._json_response(200, {"decisions": pending, "count": len(pending)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/review" or self.path.startswith("/review?"):
                    try:
                        from .review_flow import get_review_flow
                        flow = get_review_flow()
                        active = flow.list_active()
                        self._json_response(200, {
                            "reviews": active,
                            "count": len(active),
                            "stats": flow.stats,
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path.startswith("/review/") and not self.path.endswith("/submit"):
                    try:
                        from .review_flow import get_review_flow
                        task_id = self.path.split("/")[2]
                        status = get_review_flow().get_status(task_id)
                        code = 404 if "error" in status else 200
                        self._json_response(code, status)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/adapters":
                    try:
                        from .adapters.chief_sync import get_chief_sync
                        from .adapters.surgeon_events import get_surgeon_events
                        from .adapters.overlay_stream import get_overlay_stream
                        self._json_response(200, {
                            "chief_sync": {"available": get_chief_sync().available},
                            "surgeon_events": get_surgeon_events().stats,
                            "overlay_stream": get_overlay_stream().stats,
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/rebuttals":
                    # Wire P2-3: GET /rebuttals — list active rebuttal sessions
                    try:
                        mgr = self._get_rebuttal_manager()
                        if mgr is None:
                            self._json_response(503, {"error": "RebuttalManager unavailable"})
                            return
                        active = [s.to_dict() for s in mgr.active_sessions()]
                        all_sessions = [s.to_dict() for s in mgr.all_sessions()]
                        self._json_response(200, {
                            "active": active,
                            "all": all_sessions,
                            "active_count": len(active),
                            "total_count": len(all_sessions),
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/leases":
                    # Wire P2-4: GET /leases — list active resource leases
                    try:
                        mgr = self._get_lease_manager()
                        if mgr is None:
                            self._json_response(503, {"error": "LeaseManager unavailable"})
                            return
                        active = mgr.list_active()
                        self._json_response(200, {
                            "leases": active,
                            "count": len(active),
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/probes":
                    # Wire P2-5: GET /probes — return latest cached probe results
                    try:
                        from .probe_engine import ProbeRegistry
                        registry = ProbeRegistry()
                        results = registry.latest_results()
                        result_dicts = [r.to_dict() if hasattr(r, 'to_dict') else r for r in results]
                        self._json_response(200, {
                            "results": result_dicts,
                            "count": len(result_dicts),
                            "cached": True,
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
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

            _race_manager = None
            _overlay_manager = None
            _supervisor = None
            _evidence_ledger = None
            _truth_ladder = None
            _probe_engine = None
            _rebuttal_manager = None
            _lease_manager = None

            def _get_overlay_manager(self):
                """Lazy-init NodeOverlayManager instance."""
                if Handler._overlay_manager is not None:
                    return Handler._overlay_manager
                try:
                    from .node_overlay import NodeOverlayManager
                    Handler._overlay_manager = NodeOverlayManager()
                    return Handler._overlay_manager
                except Exception as e:
                    logger.warning("NodeOverlayManager init failed: %s", e)
                    from .node_overlay import NodeOverlayManager
                    return NodeOverlayManager()

            def _get_race_manager(self):
                """Lazy-init FleetRace instance."""
                if Handler._race_manager is not None:
                    return Handler._race_manager
                try:
                    from .fleet_race import FleetRace
                    Handler._race_manager = FleetRace(
                        daemon_port=8855, node_id=daemon.node_id
                    )
                    return Handler._race_manager
                except Exception as e:
                    logger.warning("FleetRace init failed: %s", e)
                    return None

            def _get_supervisor(self):
                """Return daemon's singleton Supervisor instance."""
                if Handler._supervisor is not None:
                    return Handler._supervisor
                # Use daemon-level singleton (initialized in __init__)
                if daemon._supervisor is not None:
                    Handler._supervisor = daemon._supervisor
                    return Handler._supervisor
                # Fallback: lazy-init if daemon didn't create one
                try:
                    from .supervisor_store import PersistentSupervisor
                    Handler._supervisor = PersistentSupervisor(node_id=daemon.node_id)
                    return Handler._supervisor
                except ImportError:
                    from .supervisor import Supervisor
                    Handler._supervisor = Supervisor(node_id=daemon.node_id)
                    return Handler._supervisor
                except Exception as e:
                    logger.warning("Supervisor init failed: %s", e)
                    return None

            def _get_evidence_ledger(self):
                """Lazy-init EvidenceLedger singleton."""
                if Handler._evidence_ledger is not None:
                    return Handler._evidence_ledger
                try:
                    from .evidence_ledger import EvidenceLedger
                    Handler._evidence_ledger = EvidenceLedger()
                    return Handler._evidence_ledger
                except Exception as e:
                    logger.warning("EvidenceLedger init failed: %s", e)
                    return None

            def _get_truth_ladder(self):
                """Lazy-init TruthLadder singleton."""
                if Handler._truth_ladder is not None:
                    return Handler._truth_ladder
                try:
                    from .truth_ladder import TruthLadder
                    Handler._truth_ladder = TruthLadder()
                    return Handler._truth_ladder
                except Exception as e:
                    logger.warning("TruthLadder init failed: %s", e)
                    return None

            def _get_probe_engine(self):
                """Lazy-init ProbeEngine singleton."""
                if Handler._probe_engine is not None:
                    return Handler._probe_engine
                try:
                    from .probe_engine import ProbeEngine
                    Handler._probe_engine = ProbeEngine()
                    return Handler._probe_engine
                except Exception as e:
                    logger.warning("ProbeEngine init failed: %s", e)
                    return None

            def _get_rebuttal_manager(self):
                """Lazy-init RebuttalManager singleton (in-memory state)."""
                if Handler._rebuttal_manager is not None:
                    return Handler._rebuttal_manager
                try:
                    from .cross_rebuttal import RebuttalManager
                    Handler._rebuttal_manager = RebuttalManager()
                    return Handler._rebuttal_manager
                except Exception as e:
                    logger.warning("RebuttalManager init failed: %s", e)
                    return None

            def _get_lease_manager(self):
                """Lazy-init LeaseManager singleton (SQLite-backed via daemon store)."""
                if Handler._lease_manager is not None:
                    return Handler._lease_manager
                try:
                    from .leases import LeaseManager
                    store = daemon._store
                    if not store:
                        logger.warning("LeaseManager: daemon store not available")
                        return None
                    Handler._lease_manager = LeaseManager(
                        store=store, node_id=daemon.node_id
                    )
                    return Handler._lease_manager
                except Exception as e:
                    logger.warning("LeaseManager init failed: %s", e)
                    return None

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
                elif self.path == "/wisdom/promote":
                    try:
                        if hasattr(daemon, '_memory_bridge') and daemon._memory_bridge:
                            hours = body.get("hours", 24)
                            min_severity = body.get("min_severity", "medium")
                            promoted = daemon._memory_bridge.promote_recent(
                                hours=hours, min_severity=min_severity
                            )
                            self._json_response(200, {
                                "promoted_count": len(promoted),
                                "promoted_ids": [e.get("entry_id", "")[:8] for e in promoted],
                            })
                        else:
                            self._json_response(503, {"error": "memory bridge not started"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/inject":
                    # Direct --resume injection for testing
                    # Accepts: {"session_id": "...", "message": "..."} or {"message": "..."}
                    # If session_id omitted, auto-detects active session
                    raw_sid = body.get("session_id", "")
                    session_id = normalize_session_id(raw_sid) if raw_sid else daemon._get_active_session_full_id()
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
                elif self.path == "/arbiter/decide":
                    try:
                        from .arbiter_view import record_decision
                        decision_id = body.get("decision_id", "")
                        action = body.get("action", "")
                        details = body.get("details", {})
                        if not decision_id or not action:
                            self._json_response(400, {"error": "missing decision_id or action"})
                            return
                        result = record_decision(decision_id, action, details)
                        code = 200 if "error" not in result else 404
                        self._json_response(code, result)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/evidence":
                    try:
                        from .evidence_ledger import EvidenceLedger
                        ledger = EvidenceLedger()
                        event_type = body.get("event_type", "")
                        ev_node_id = body.get("node_id", daemon.node_id)
                        subject = body.get("subject", "")
                        payload = body.get("payload", {})
                        if not event_type:
                            self._json_response(400, {"error": "missing event_type"})
                            return
                        entry = ledger.record(event_type, ev_node_id, subject, payload)
                        self._json_response(200, entry)
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/race":
                    racer = self._get_race_manager()
                    if not racer:
                        self._json_response(503, {"error": "FleetRace not available"})
                        return
                    task = body.get("task", "")
                    if not task:
                        self._json_response(400, {"error": "missing 'task'"})
                        return
                    nodes = body.get("nodes")
                    timeout_s = int(body.get("timeout_s", 600))
                    result = racer.start(task, nodes=nodes, timeout_s=timeout_s)
                    self._json_response(200, result)
                elif self.path.startswith("/race/") and self.path.endswith("/submit"):
                    racer = self._get_race_manager()
                    if not racer:
                        self._json_response(503, {"error": "FleetRace not available"})
                        return
                    # /race/<id>/submit
                    parts = self.path.split("/")
                    race_id = parts[2] if len(parts) >= 4 else ""
                    node = body.get("node", daemon.node_id)
                    result_data = body.get("result", {})
                    if not race_id:
                        self._json_response(400, {"error": "missing race_id"})
                        return
                    result = racer.submit_result(race_id, node, result_data)
                    code = 404 if "error" in result else 200
                    self._json_response(code, result)
                elif self.path == "/truth":
                    try:
                        from .truth_ladder import TruthLadder
                        ladder = TruthLadder()
                        action = body.get("action", "")
                        if action == "add_inbox":
                            entry = ladder.add_inbox(
                                body.get("title", ""),
                                body.get("content", ""),
                                metadata=body.get("metadata"),
                            )
                            self._json_response(200, {"ok": True, "entry": entry.to_dict()})
                        elif action == "promote":
                            entry_id = body.get("entry_id", "")
                            target_layer = body.get("layer", "")
                            refs = body.get("evidence_refs", [])
                            promoted_by = body.get("promoted_by", daemon.node_id)
                            if target_layer == "vision":
                                entry = ladder.promote_to_vision(entry_id, refs, promoted_by)
                            elif target_layer == "snapshot":
                                entry = ladder.promote_to_snapshot(entry_id, refs, promoted_by)
                            elif target_layer == "dao":
                                entry = ladder.promote_to_dao(entry_id, refs, promoted_by)
                            else:
                                self._json_response(400, {"error": f"Invalid target layer: {target_layer}"})
                                return
                            self._json_response(200, {"ok": True, "entry": entry.to_dict()})
                        else:
                            self._json_response(400, {"error": f"Unknown action: {action}. Use: add_inbox, promote"})
                    except ValueError as e:
                        self._json_response(400, {"error": str(e)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/supervisor":
                    try:
                        sup = self._get_supervisor()
                        if not sup:
                            self._json_response(500, {"error": "Supervisor init failed"})
                            return
                        action = body.get("action", "")
                        if action == "start":
                            state = sup.start_task(
                                body.get("task_id", ""),
                                body.get("description", ""),
                                body.get("done_criteria", []),
                            )
                            self._json_response(200, {"ok": True, "state": state.to_dict()})
                        elif action == "advance":
                            state = sup.advance(body.get("task_id", ""), body.get("evidence"))
                            self._json_response(200, {"ok": True, "state": state.to_dict()})
                        elif action == "block":
                            state = sup.block(body.get("task_id", ""), body.get("reason", ""))
                            self._json_response(200, {"ok": True, "state": state.to_dict()})
                        else:
                            self._json_response(400, {"error": f"Unknown action: {action}. Use: start, advance, block"})
                    except ValueError as e:
                        self._json_response(400, {"error": str(e)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/accept":
                    try:
                        from .acceptance_gate import AcceptanceGate
                        task_id = body.get("task_id", "")
                        if not task_id:
                            self._json_response(400, {"error": "missing 'task_id'"})
                            return
                        # Gather subsystem data for evaluate() kwargs
                        eval_kwargs = {}
                        # Evidence count
                        try:
                            from .evidence_ledger import EvidenceLedger
                            ledger = EvidenceLedger()
                            entries = ledger.query(limit=100)
                            eval_kwargs["evidence_count"] = len(entries)
                        except Exception:
                            eval_kwargs["evidence_count"] = 0
                        # Probe results from body context
                        eval_kwargs["probe_results"] = body.get("probe_results", [])
                        eval_kwargs["surgeon_resolved"] = body.get("surgeon_resolved", False)
                        eval_kwargs["surgeon_intensity"] = body.get("surgeon_intensity", "light")
                        eval_kwargs["surgeon_dissent_count"] = body.get("surgeon_dissent_count", 0)
                        eval_kwargs["invariants_satisfied"] = body.get("invariants_satisfied", True)
                        eval_kwargs["epistemic_packet"] = body.get("epistemic_packet")
                        strict = body.get("strict", False)
                        gate = AcceptanceGate(
                            strict=strict,
                            evidence_ledger=self._get_evidence_ledger(),
                            probe_engine=self._get_probe_engine(),
                        )
                        result = gate.evaluate(**eval_kwargs)
                        self._json_response(200, result.to_dict())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/overlay":
                    try:
                        from .node_overlay import NodeOverlayManager
                        mgr = self._get_overlay_manager()
                        node_id = body.get("node_id", daemon.node_id)
                        updates = {k: v for k, v in body.items() if k != "node_id"}
                        overlay = mgr.update(node_id, **updates)
                        self._json_response(200, overlay.to_dict())
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/review":
                    try:
                        from .review_flow import get_review_flow
                        flow = get_review_flow()
                        action = body.get("action", "")
                        if action == "start":
                            session = flow.start_review(
                                task_id=body.get("task_id", ""),
                                assigned_machines=body.get("machines", []),
                                task_summary=body.get("summary", ""),
                                deadline_minutes=int(body.get("deadline_minutes", 30)),
                            )
                            self._json_response(200, {
                                "ok": True,
                                "task_id": session.task_id,
                                "phase": session.phase.value,
                            })
                        elif action == "verdict":
                            result = flow.submit_verdict(
                                body.get("task_id", ""),
                                body.get("machine", daemon.node_id),
                                body.get("verdict", {}),
                            )
                            self._json_response(200, result)
                        elif action == "critique":
                            result = flow.submit_critique(
                                body.get("task_id", ""),
                                body.get("machine", daemon.node_id),
                                body.get("critique", {}),
                            )
                            self._json_response(200, result)
                        elif action == "synthesize":
                            result = flow.run_synthesis(body.get("task_id", ""))
                            self._json_response(200, result.to_dict())
                        elif action == "cleanup":
                            removed = flow.cleanup_expired()
                            self._json_response(200, {"removed": removed})
                        else:
                            self._json_response(400, {
                                "error": f"Unknown action: {action}. "
                                         "Use: start, verdict, critique, synthesize, cleanup"
                            })
                    except ValueError as e:
                        self._json_response(400, {"error": str(e)})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/probes":
                    # Wire P2-5: POST /probes — run a probe scan
                    try:
                        from .probe_engine import ProbeRegistry
                        registry = ProbeRegistry()
                        context = body.get("context", {})
                        results = registry.run_all(context=context)
                        result_dicts = [r.to_dict() if hasattr(r, 'to_dict') else r for r in results]
                        # Update overlay with results
                        try:
                            mgr = self._get_overlay_manager()
                            if mgr:
                                mgr.update_from_probe(daemon.node_id, result_dicts)
                        except Exception:
                            pass
                        self._json_response(200, {
                            "results": result_dicts,
                            "count": len(result_dicts),
                            "cached": False,
                        })
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                elif self.path == "/orchestrate":
                    try:
                        orch = daemon._task_orchestrator
                        if not orch:
                            self._json_response(503, {"error": "TaskOrchestrator not available"})
                            return
                        action = body.get("action", "run")
                        if action == "run":
                            task_desc = body.get("task", "")
                            if not task_desc:
                                self._json_response(400, {"error": "missing 'task'"})
                                return
                            criteria_raw = body.get("criteria", "Complete")
                            criteria = [c.strip() for c in criteria_raw.split(",") if c.strip()]
                            parallel = body.get("parallel", "false") in (True, "true", "1")
                            nodes_raw = body.get("nodes", "")
                            nodes = [n.strip() for n in nodes_raw.split(",") if n.strip()] if nodes_raw else None
                            result = orch.run_task(
                                task_desc, done_criteria=criteria,
                                parallel=parallel, nodes=nodes,
                            )
                            self._json_response(200, result)
                        elif action == "status":
                            task_id = body.get("task_id", "")
                            if not task_id:
                                self._json_response(400, {"error": "missing 'task_id'"})
                                return
                            self._json_response(200, orch.status(task_id))
                        elif action == "list":
                            self._json_response(200, {"tasks": orch.list_tasks()})
                        else:
                            self._json_response(400, {"error": f"Unknown action: {action}. Use: run, status, list"})
                    except Exception as e:
                        self._json_response(500, {"error": str(e)})
                else:
                    self.send_response(404)
                    self.end_headers()

        # Expose Handler class on daemon so discovery.py can access overlay_manager
        daemon._http_handler_cls = Handler

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

        # Wire: Start SSE event stream server for IDE consumption (best-effort)
        try:
            from multifleet.event_stream import start_server as start_sse
            start_sse(port=8033, daemon=True)
            logger.info("SSE event stream on :8033")
        except Exception as e:
            logger.debug("SSE event stream not started: %s", e)


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
