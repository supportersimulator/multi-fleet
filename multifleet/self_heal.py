"""
Self-healing, watchdog, and repair escalation for fleet messaging.

Extracted from nats_client.py — contains:
- SelfHealManager: repair actions, watchdog loops, ACK retry,
  channel health tracking, peer health scoring, idle detection
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .nats_client import FleetNerveNATS

from .channels import ALL_CHANNELS, HEAL_SCORE_THRESHOLD

logger = logging.getLogger("fleet_nats")

SEED_DIR = "/tmp"
REPO_ROOT = Path(__file__).parent.parent


class SelfHealManager:
    """Self-healing, watchdog monitoring, and repair escalation manager.

    Delegates to the parent FleetNerveNATS (``nerve``) for node identity,
    NATS connection, peer state, and message sending.  All watchdog state
    is local to this manager.
    """

    ACK_TIMEOUT_S = 60.0
    ACK_MAX_RETRIES = 3

    # ── Repair cascade prevention ──
    # Only the chief node (mac1) is allowed to execute L4 SSH repairs.
    # Non-chief nodes escalate L4 to chief via fleet message instead.
    L4_CHIEF_ONLY = os.environ.get("FLEET_REPAIR_L4_CHIEF_ONLY", "true").lower() in ("true", "1", "yes")

    def __init__(self, nerve: FleetNerveNATS):
        self._nerve = nerve
        # Repair lock: target -> timestamp. Prevents concurrent repairs on same target.
        self._repair_locks: dict[str, float] = {}
        self._repair_lock_ttl = 300  # 5 min lock duration

    # ── Repair Lock (cascade prevention) ──

    def _is_repair_locked(self, target: str) -> bool:
        """Check if a repair is already in progress for target."""
        lock_time = self._repair_locks.get(target)
        if lock_time is None:
            return False
        if time.time() - lock_time > self._repair_lock_ttl:
            # Lock expired — clean up
            del self._repair_locks[target]
            return False
        return True

    def _acquire_repair_lock(self, target: str) -> bool:
        """Try to acquire the repair lock for target. Returns True if acquired."""
        if self._is_repair_locked(target):
            return False
        self._repair_locks[target] = time.time()
        return True

    def _release_repair_lock(self, target: str):
        """Release the repair lock for target."""
        self._repair_locks.pop(target, None)

    def _is_chief(self) -> bool:
        """Check if this node is the chief (mac1)."""
        chief = getattr(self._nerve, '_chief', None)
        if chief:
            return self._nerve.node_id == chief
        # Fallback: mac1 is always chief
        return self._nerve.node_id == "mac1"

    # ── Repair Actions ──

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

    async def _repair_restart_nats(self, heal: dict):
        """Reconnect to NATS server without killing the daemon."""
        logger.info("Repair: reconnecting to NATS...")
        if self._nerve.nc:
            try:
                await self._nerve.nc.close()
            except Exception:
                pass
        await asyncio.sleep(1)
        await self._nerve.connect()
        await self._nerve._subscribe_all()
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

    # ── Channel Health Tracking ──

    def _update_channel_health(self, target: str, channel: str, status: str):
        """Track per-peer channel health. Status: 'ok' or 'broken'."""
        if target not in self._nerve._channel_health:
            self._nerve._channel_health[target] = {ch: "ok" for ch in ALL_CHANNELS}
        self._nerve._channel_health[target][channel] = status

    def _get_peer_health_score(self, target: str) -> tuple:
        """Returns (score, broken_list) for a peer. Max score = len(ALL_CHANNELS)."""
        ch = self._nerve._channel_health.get(target, {})
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
        existing = self._nerve._heal_in_flight.get(target)
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
                self._nerve._heal_in_flight.pop(target, None)

        self._nerve._heal_in_flight[target] = asyncio.ensure_future(_heal_and_verify())

    async def _verify_channel(self, target: str, channel: str) -> bool:
        """Probe a single channel to verify it works. Returns True if delivered."""
        probe_msg = {"type": "sync", "payload": {"subject": "ping"}, "from": self._nerve.node_id}
        sender_map = {
            "P1_nats": self._nerve._send_p1_nats, "P2_http": self._nerve._send_p2_http,
            "P3_chief": self._nerve._send_p3_chief, "P4_seed": self._nerve._send_p4_seed,
            "P5_ssh": self._nerve._send_p5_ssh, "P7_git": self._nerve._send_p7_git,
        }
        sender = sender_map.get(channel)
        if not sender:
            return False
        try:
            result = await asyncio.wait_for(sender(target, probe_msg), timeout=5)
            return bool(result.get("delivered"))
        except Exception:
            return False

    async def repair_with_escalation(self, target: str, broken_channels: list, action: str = "restart_nats"):
        """Multi-level repair escalation. Total time to L4: ~90s.

        Level 1 NOTIFY: Silent repair msg to target (30s wait)
        Level 2 GUIDE: Send specific repair command (30s wait)
        Level 3 ASSIST: Send auto-exec repair action (30s wait)
        Level 4 REMOTE: SSH direct intervention (~90s after detection)
          - Chief-only: non-chief nodes escalate L4 to chief via fleet message
          - Repair lock: only one repair per target at a time

        All repair messages use type:"repair" (NOT "context") to avoid
        seed file writes and keystroke injection spam on the target.
        Each level uses send_with_fallback() for delivery.
        """
        # Repair lock: skip if another repair is in progress for this target
        if not self._acquire_repair_lock(target):
            logger.info(f"Repair skipped for {target}: repair already in progress")
            return {"repaired": False, "reason": "repair_locked"}

        logger.info(f"Repair escalation for {target}: broken={broken_channels}, action={action}")

        # Level 1: NOTIFY -- silent repair msg (no seed/keystroke), 2min wait
        logger.info(f"Repair L1 NOTIFY -> {target}")
        notify_msg = {
            "type": "repair", "from": self._nerve.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": action,
                    "reporter": self._nerve.node_id,
                    "broken_channels": broken_channels,
                    "level": 1,
                }),
            },
        }
        result = await self._nerve.send_with_fallback(target, notify_msg)
        if result.get("delivered"):
            logger.info(f"Repair L1 delivered via {result.get('channel')}. Waiting 120s for local fix...")
            await asyncio.sleep(120)  # 2min -- give target time to self-fix
            probe = await self._nerve._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L1 notify")
                self._release_repair_lock(target)
                return {"repaired": True, "level": 1}

        # Level 2: GUIDE -- auto-executable repair command (still silent, no seed/keystroke)
        logger.info(f"Repair L2 GUIDE -> {target}")
        guide_msg = {
            "type": "repair", "from": self._nerve.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": "restart_daemon",
                    "reporter": self._nerve.node_id,
                    "broken_channels": broken_channels,
                    "level": 2,
                }),
            },
        }
        result = await self._nerve.send_with_fallback(target, guide_msg)
        if result.get("delivered"):
            logger.info(f"Repair L2 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._nerve._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L2 guide")
                self._release_repair_lock(target)
                return {"repaired": True, "level": 2}

        # Level 3: ASSIST -- send auto-executable repair action (mac1's handlers)
        logger.info(f"Repair L3 ASSIST -> {target}")
        repair_msg = {
            "type": "repair", "from": self._nerve.node_id, "to": target,
            "payload": {
                "subject": "repair",
                "body": json.dumps({
                    "action": action,
                    "reporter": self._nerve.node_id,
                    "broken_channels": broken_channels,
                    "level": 3,
                }),
            },
        }
        result = await self._nerve.send_with_fallback(target, repair_msg)
        if result.get("delivered"):
            logger.info(f"Repair L3 delivered via {result.get('channel')}. Waiting 30s...")
            await asyncio.sleep(30)
            probe = await self._nerve._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
            if probe.get("delivered"):
                logger.info(f"Repair SUCCESS: {target} back on P1 after L3 assist")
                self._release_repair_lock(target)
                return {"repaired": True, "level": 3}

        # Level 4: REMOTE -- SSH direct intervention (last resort, ~90s after first detection)
        # Chief-only gate: non-chief nodes escalate L4 to chief instead of executing directly
        if self.L4_CHIEF_ONLY and not self._is_chief():
            logger.info(f"Repair L4: non-chief node, escalating to chief for {target}")
            try:
                chief_id = getattr(self._nerve, '_chief', None) or "mac1"
                escalation_msg = {
                    "type": "repair", "from": self._nerve.node_id, "to": chief_id,
                    "payload": {
                        "subject": "l4_escalation",
                        "body": json.dumps({
                            "action": action,
                            "target": target,
                            "reporter": self._nerve.node_id,
                            "broken_channels": broken_channels,
                            "level": 4,
                            "escalated": True,
                        }),
                    },
                }
                await self._nerve.send_with_fallback(chief_id, escalation_msg)
            except Exception as e:
                logger.warning(f"Repair L4 escalation to chief failed: {e}")
            self._release_repair_lock(target)
            return {"repaired": False, "level": 4, "reason": "escalated_to_chief"}
        logger.info(f"Repair L4 REMOTE -> {target} (~90s failure, all local attempts exhausted)")
        peer = self._nerve._resolve_peer(target)
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
                    self._release_repair_lock(target)
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
                    probe = await self._nerve._send_p1_nats(target, {"type": "sync", "payload": {"subject": "ping"}})
                    if probe.get("delivered"):
                        logger.info(f"Repair SUCCESS: {target} back on P1 after L4 remote restart")
                        self._release_repair_lock(target)
                        return {"repaired": True, "level": 4}
            except Exception as e:
                logger.warning(f"Repair L4 SSH failed: {e}")

        self._release_repair_lock(target)
        logger.warning(f"Repair FAILED: all 4 levels exhausted for {target}")
        return {"repaired": False, "level": 4, "reason": "all_levels_failed"}

    # ── Watchdog Loop ──

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
        self._nerve._last_activity_at = time.time()  # updated on send/receive
        self._idle_wake_sent = {}  # peer_id -> timestamp of last idle-wake sent
        self._last_suggestion_scan = 0.0  # productive idle rate limit
        self._last_maintenance_run = 0.0  # hourly maintenance tracking
        self._maintenance_interval = 3600  # 1 hour

        while self._nerve._running:
            try:
                await self._watchdog_check_peers()
                await self._watchdog_check_channel_health()
                await self._watchdog_check_zombies()
                await self._watchdog_check_git()
                await self._watchdog_check_tunnels()
                await self._watchdog_check_idle()
                await self._watchdog_check_pending_acks()
                # Hourly maintenance: chain/dispatch cleanup + store maintenance
                await self._watchdog_run_maintenance()
            except Exception as e:
                logger.debug(f"Watchdog cycle error: {e}")
            await asyncio.sleep(30)

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
        idle_seconds = now - self._nerve._last_activity_at
        if idle_seconds > idle_threshold_self:
            pending_items = []

            # Check fleet inbox for unprocessed messages
            inbox_dir = REPO_ROOT / ".fleet-messages" / self._nerve.node_id
            if inbox_dir.exists():
                unprocessed = [f.name for f in inbox_dir.iterdir()
                               if f.is_file() and f.suffix == ".md"]
                if unprocessed:
                    pending_items.append(f"{len(unprocessed)} inbox message(s)")

            # Check seed file for pending messages
            seed_path = Path(SEED_DIR) / f"fleet-seed-{self._nerve.node_id}.md"
            if seed_path.exists() and seed_path.stat().st_size > 0:
                pending_items.append("pending seed file")

            pending_note = f" -- found: {', '.join(pending_items)}" if pending_items else ""
            idle_msg = (
                f"Node {self._nerve.node_id} idle ({int(idle_seconds)}s)"
                f" -- checking plans and exploring opportunities{pending_note}"
            )
            logger.info(f"Watchdog idle: {idle_msg}")

            # Broadcast idle status to fleet
            try:
                if self._nerve.nc and self._nerve.nc.is_connected:
                    await self._nerve.send("all", "idle", "idle-check", idle_msg)
            except Exception as e:
                logger.debug(f"Watchdog idle broadcast failed: {e}")

            # Trigger git pull if behind (reuses existing method's cooldown)
            await self._watchdog_check_git()

            # Productive idle: scan for actionable work and broadcast suggestions
            await self._productive_idle_scan()

            # Reset activity timer so we don't spam every 30s
            self._nerve._last_activity_at = now

        # ── Peer idle check ──
        for peer_id, hb_data in self._nerve._peers.items():
            if peer_id == self._nerve.node_id:
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
                if self._nerve.nc and self._nerve.nc.is_connected:
                    await self._nerve.send(peer_id, "idle-wake", "idle-wake", wake_msg)
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
            "node": self._nerve.node_id,
            "scanned_at": datetime.now(timezone.utc).isoformat() + "Z",
            "suggestions": suggestions,
        }
        try:
            Path("/tmp/fleet-watchdog-suggestions.json").write_text(
                json.dumps(output, indent=2))
        except Exception as e:
            logger.debug(f"Productive idle: write suggestions failed: {e}")

        # Broadcast summary to fleet
        if suggestions and self._nerve.nc and self._nerve.nc.is_connected:
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
                await self._nerve.send(
                    "all", "idle-suggestions", "productive-idle", body)
            except Exception:
                pass

        logger.info(f"Productive idle scan: "
                    f"{len(suggestions)} suggestion group(s)")

    async def _watchdog_check_peers(self):
        """Detect stale peers and trigger repair escalation."""
        now = time.time()
        all_peers = set(self._nerve._peers_config.keys()) - {self._nerve.node_id}
        seen_peers = set(self._nerve._peers.keys())

        for peer_id in all_peers:
            # Skip if repair already in progress
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:  # 5min cooldown
                continue

            if peer_id in seen_peers:
                last_seen = now - self._nerve._peers[peer_id].get("_received_at", 0)
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
                if now - self._nerve._stats["started_at"] > 300:
                    logger.warning(f"Watchdog: {peer_id} never seen -- attempting contact")
                    self._repair_in_progress[peer_id] = {"started_at": now, "level": 1}
                    asyncio.create_task(
                        self.repair_with_escalation(peer_id, ["P1_nats", "P2_http"], "restart_nats"))

    async def _watchdog_check_channel_health(self):
        """Check channel health scores and trigger repair for degraded peers."""
        now = time.time()
        for peer_id, channels in self._nerve._channel_health.items():
            score, broken = self._get_peer_health_score(peer_id)
            if score >= HEAL_SCORE_THRESHOLD or not broken:
                continue
            # Skip if repair already in progress (5min cooldown)
            repair = self._repair_in_progress.get(peer_id, {})
            if repair and now - repair.get("started_at", 0) < 300:
                continue
            existing = self._nerve._heal_in_flight.get(peer_id)
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
        for msg_id, info in list(self._nerve._pending_acks.items()):
            if info["acked"]:
                to_remove.append(msg_id)
                continue
            age = now - info["sent_at"]
            if age < self._nerve._ack_timeout_s:
                continue
            if info["retries"] >= self._nerve._ack_max_retries:
                logger.warning(f"ACK failed after {self._nerve._ack_max_retries} retries: "
                               f"{msg_id[:8]} -> {info['target']}")
                to_remove.append(msg_id)
                continue
            info["retries"] += 1
            logger.info(f"ACK retry {info['retries']}/{self._nerve._ack_max_retries}: "
                        f"{msg_id[:8]} -> {info['target']}")
            asyncio.create_task(self._nerve.send_with_fallback(info["target"], info["message"]))
        for msg_id in to_remove:
            self._nerve._pending_acks.pop(msg_id, None)

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
                    env={**os.environ, "MULTIFLEET_NODE_ID": self._nerve.node_id})
                logger.info("Watchdog: tunnels restarted")
            except Exception as e:
                logger.debug(f"Watchdog tunnel restart failed: {e}")

    # ── ACK Retry Loop ──

    async def _ack_retry_loop(self):
        """Check for unacknowledged messages and retry via next fallback channel.

        Runs every 30s. Messages awaiting ACK past ACK_TIMEOUT_S get re-sent.
        After ACK_MAX_RETRIES, marked as failed and surfaced via _inject_alert.
        """
        await asyncio.sleep(30)  # Let daemon stabilize
        logger.info("ACK retry loop started (30s interval)")
        while self._nerve._running:
            try:
                if not self._nerve._store:
                    await asyncio.sleep(30)
                    continue
                pending = self._nerve._store.get_awaiting_ack(
                    max_age_s=self.ACK_TIMEOUT_S, max_retries=self.ACK_MAX_RETRIES
                )
                for row in pending:
                    msg_id = row["id"]
                    target = row["to_node"]
                    retries = row.get("retry_count", 0)

                    if retries >= self.ACK_MAX_RETRIES:
                        # Exhausted retries -- mark failed and alert
                        self._nerve._store.mark_ack_failed(msg_id)
                        logger.warning(f"ACK failed after {retries} retries: {msg_id[:8]}… -> {target}")
                        self._nerve._inject_alert({
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
                        "from": row.get("from_node", self._nerve.node_id),
                        "to": target,
                        "payload": body_data,
                        "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                    }

                    self._nerve._store.bump_retry(msg_id)
                    logger.info(f"ACK retry {retries + 1}/{self.ACK_MAX_RETRIES}: {msg_id[:8]}… -> {target}")

                    # Re-send (send_with_fallback will skip re-storing since it already exists)
                    try:
                        result = await self._nerve.send_with_fallback(target, message)
                        if result.get("delivered"):
                            logger.info(f"ACK retry delivered: {msg_id[:8]}… via {result.get('channel')}")
                    except Exception as e:
                        logger.warning(f"ACK retry send failed: {msg_id[:8]}…: {e}")

            except Exception as e:
                logger.error(f"ACK retry loop error: {e}")
            await asyncio.sleep(30)
