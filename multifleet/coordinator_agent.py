"""Coordinator Agent — persistent daemon managing local fleet node.

Designed March 2026, implemented April 2026. Each Mac (mac1, mac2, mac3)
runs one CoordinatorAgent that:

1. Monitors local health (Redis, LLM, webhook, fleet daemon, NATS, surgeons)
2. Receives fleet-wide decisions via NATS and event bus
3. Manages local 3-Surgeons sessions (probe, track, escalate)
4. Feeds evidence into the global evidence_ledger hash chain
5. Sends periodic heartbeats with rich local state
6. Handles task assignment from chief synthesis
7. Coordinates with invariance gates for quality-gated operations
8. Self-heals local issues, escalates to fleet when needed

Usage:
    agent = CoordinatorAgent(node_id="mac2")
    await agent.start()
    # ... runs until stopped ...
    await agent.stop()

CLI:
    python -m multifleet.coordinator_agent --node-id mac2
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .coordinator import (
    build_heartbeat,
    get_node_id,
    load_config,
    queue_packet,
    send_to_chief,
    surgeon_status,
)
from .coordinator_evidence import EvidencePipeline
from .coordinator_health import HealthChecker, LocalHealthSnapshot
from .coordinator_surgeon_proxy import SurgeonProxy
from .packets import FleetPacket
from .superset_bridge import SupersetBridge

logger = logging.getLogger("fleet-nerve.coordinator-agent")

REPO_ROOT = Path(__file__).parent.parent


class CoordinatorAgent:
    """Persistent daemon coordinating a single fleet node.

    Composes existing coordinator.py functionality with new subsystems:
    - HealthChecker: periodic local service monitoring
    - SurgeonProxy: 3-Surgeons availability and request proxying
    - EvidencePipeline: local findings -> global evidence chain
    - NATS subscription: fleet decision channels
    - Invariance gates: quality-gated operations
    """

    def __init__(
        self,
        node_id: str = None,
        heartbeat_interval_s: float = 30.0,
        health_check_interval_s: float = 30.0,
        surgeon_probe_interval_s: float = 60.0,
        nats_url: str = None,
        config: dict = None,
    ):
        self.node_id = node_id or get_node_id()
        self.heartbeat_interval_s = heartbeat_interval_s
        self._config = config or load_config()
        self._nats_url = nats_url or os.environ.get(
            "NATS_URL", "nats://127.0.0.1:4222"
        )

        # Subsystems
        self.health = HealthChecker(
            node_id=self.node_id,
            check_interval_s=health_check_interval_s,
        )
        self.surgeons = SurgeonProxy(
            node_id=self.node_id,
            probe_interval_s=surgeon_probe_interval_s,
        )
        self.evidence = EvidencePipeline(node_id=self.node_id)

        # NATS connection (lazy)
        self._nats_conn = None
        self._nats_subscriptions = []

        # Task management
        self._task_state = None
        self._active_tasks: dict[str, dict] = {}

        # Superset bridge (graceful — fleet works without it)
        self._superset = SupersetBridge()
        self._superset_task_map: dict[str, str] = {}  # fleet_task_id -> superset_task_id

        # Invariance gates (lazy)
        self._send_gate = None
        self._repair_gate = None
        self._merge_gate = None

        # Lifecycle
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._queue_flush_task: Optional[asyncio.Task] = None
        self._decision_handlers: dict[str, list] = {}
        self._prev_health: Optional[LocalHealthSnapshot] = None
        self._stats = {
            "heartbeats_sent": 0,
            "heartbeats_failed": 0,
            "decisions_received": 0,
            "tasks_received": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "started_at": 0.0,
        }

    # ── Lifecycle ──

    async def start(self):
        """Start all coordinator subsystems."""
        if self._running:
            logger.warning("Coordinator agent already running")
            return

        self._running = True
        self._stats["started_at"] = time.time()
        logger.info(
            "Coordinator agent starting: node=%s, heartbeat=%.0fs",
            self.node_id, self.heartbeat_interval_s,
        )

        # Initialize subsystems
        await self.health.start()
        await self.surgeons.start()
        self._init_gates()
        self._init_task_state()

        # Connect to NATS
        await self._connect_nats()

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Start queue flush loop
        self._queue_flush_task = asyncio.create_task(self._queue_flush_loop())

        # Run initial health check
        snapshot = await self.health.check_all()
        await self.surgeons.probe_availability()

        # Record startup
        self.evidence.record(
            "coordinator_started",
            f"Coordinator agent started on {self.node_id}",
            {
                "node_id": self.node_id,
                "config_fleet_id": self._config.get("fleetId", "unknown"),
                "initial_health": snapshot.to_dict(),
            },
        )

        logger.info("Coordinator agent running on %s", self.node_id)

    async def stop(self):
        """Stop all coordinator subsystems gracefully."""
        if not self._running:
            return

        logger.info("Coordinator agent stopping on %s", self.node_id)
        self._running = False

        # Cancel background tasks
        for task in (self._heartbeat_task, self._queue_flush_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Stop subsystems
        await self.health.stop()
        await self.surgeons.stop()

        # Disconnect NATS
        await self._disconnect_nats()

        # Flush evidence queue
        self.evidence.flush_queue()

        # Record shutdown
        self.evidence.record(
            "coordinator_stopped",
            f"Coordinator agent stopped on {self.node_id}",
            {
                "node_id": self.node_id,
                "uptime_s": time.time() - self._stats["started_at"],
                "stats": dict(self._stats),
            },
        )

        logger.info("Coordinator agent stopped")

    @property
    def running(self) -> bool:
        return self._running

    # ── Heartbeat ──

    async def _heartbeat_loop(self):
        """Send periodic heartbeats with rich local state."""
        while self._running:
            try:
                await self._send_heartbeat()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._stats["heartbeats_failed"] += 1
                logger.error("Heartbeat failed: %s", e)
            await asyncio.sleep(self.heartbeat_interval_s)

    async def _send_heartbeat(self):
        """Build and send a single heartbeat with full local state."""
        # Gather health snapshot
        snapshot = self.health.latest
        if snapshot is None:
            snapshot = await self.health.check_all()

        # Detect health changes
        await self._detect_health_changes(snapshot)
        self._prev_health = snapshot

        # Build heartbeat packet
        base_hb = build_heartbeat(self._config)
        heartbeat = {
            **base_hb,
            "coordinator_version": "1.0.0",
            "health": snapshot.to_dict(),
            "surgeons": self.surgeons.get_availability(),
            "surgeons_available": self.surgeons.get_available_count(),
            "active_tasks": len(self._active_tasks),
            "evidence_queue": self.evidence.queue_size,
            "stats": dict(self._stats),
        }

        # Send to chief via HTTP
        loop = asyncio.get_event_loop()
        sent = await loop.run_in_executor(
            None, lambda: send_to_chief(heartbeat, self._config)
        )
        if sent:
            self._stats["heartbeats_sent"] += 1
        else:
            self._stats["heartbeats_failed"] += 1
            # Queue for later
            await loop.run_in_executor(None, lambda: queue_packet(heartbeat))

        # Also publish via NATS if connected
        if self._nats_conn:
            try:
                await self._nats_conn.publish(
                    "fleet.heartbeat",
                    json.dumps(heartbeat).encode(),
                )
            except Exception as e:
                logger.debug("NATS heartbeat publish failed: %s", e)

        # Record evidence (periodically, not every heartbeat)
        if self._stats["heartbeats_sent"] % 10 == 0:
            self.evidence.record_heartbeat(snapshot.to_dict())

    async def _detect_health_changes(self, current: LocalHealthSnapshot):
        """Detect and record health state transitions."""
        if self._prev_health is None:
            return

        prev_degraded = set(self._prev_health.degraded_services)
        curr_degraded = set(current.degraded_services)

        # Newly degraded
        for svc in curr_degraded - prev_degraded:
            svc_health = current.services.get(svc)
            details = svc_health.to_dict() if svc_health else {}
            self.evidence.record_health_change(svc, healthy=False, details=details)
            logger.warning("Service degraded: %s", svc)

        # Recovered
        for svc in prev_degraded - curr_degraded:
            self.evidence.record_health_change(svc, healthy=True)
            logger.info("Service recovered: %s", svc)

    # ── NATS Subscription ──

    async def _connect_nats(self):
        """Connect to NATS and subscribe to fleet decision channels."""
        try:
            import nats as nats_module
        except ImportError:
            logger.warning("nats-py not installed, NATS subscription disabled")
            return

        try:
            self._nats_conn = await nats_module.connect(
                self._nats_url,
                name=f"coordinator-{self.node_id}",
                reconnect_time_wait=5,
                max_reconnect_attempts=-1,  # infinite
            )
            logger.info("Connected to NATS at %s", self._nats_url)

            # Subscribe to fleet decision channels
            subjects = [
                f"fleet.{self.node_id}.>",  # Direct to this node
                "fleet.all.>",               # Broadcasts
                "fleet.decisions.>",         # Chief decisions
            ]
            for subject in subjects:
                sub = await self._nats_conn.subscribe(
                    subject, cb=self._on_nats_message,
                )
                self._nats_subscriptions.append(sub)
                logger.info("Subscribed to NATS: %s", subject)

        except Exception as e:
            logger.warning("NATS connection failed (will retry via heartbeat): %s", e)
            self._nats_conn = None

    async def _disconnect_nats(self):
        """Disconnect from NATS."""
        if self._nats_conn:
            try:
                for sub in self._nats_subscriptions:
                    await sub.unsubscribe()
                await self._nats_conn.close()
            except Exception as e:
                logger.debug("NATS disconnect error: %s", e)
            self._nats_conn = None
            self._nats_subscriptions = []

    async def _on_nats_message(self, msg):
        """Handle incoming NATS message."""
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Invalid NATS message: %s", e)
            return

        msg_type = data.get("type", "")
        logger.debug("NATS message received: %s (subject=%s)", msg_type, msg.subject)

        # Route by type
        if msg_type == "chief_decision":
            await self._handle_fleet_decision(data)
        elif msg_type == "task":
            await self._handle_task_assignment(data)
        elif msg_type == "repair":
            await self._handle_repair_request(data)
        elif msg_type == "surgeon_request":
            await self._handle_surgeon_request(data)
        elif msg_type == "sync_hold":
            await self._handle_sync_hold(data)
        else:
            # Call registered handlers (supports both sync and async)
            handlers = self._decision_handlers.get(msg_type, [])
            for handler in handlers:
                try:
                    import asyncio as _asyncio
                    result = handler(data)
                    if _asyncio.iscoroutine(result) or _asyncio.isfuture(result):
                        await result
                except Exception as e:
                    logger.error("Handler for %s failed: %s", msg_type, e)

    def on_message(self, msg_type: str, handler):
        """Register a handler for a specific message type."""
        self._decision_handlers.setdefault(msg_type, []).append(handler)

    # ── Fleet Decision Handling ──

    async def _handle_fleet_decision(self, data: dict):
        """Execute a fleet decision locally."""
        self._stats["decisions_received"] += 1
        decision = data.get("decision", "")
        task_id = data.get("task_id", "unknown")

        logger.info(
            "Fleet decision received: %s for task %s",
            decision, task_id[:8],
        )

        # Record in evidence
        self.evidence.record_fleet_decision(data)

        # Execute decision based on type
        if decision == "merge":
            await self._execute_merge_decision(data)
        elif decision == "cherry_pick":
            await self._execute_cherry_pick(data)
        elif decision == "needs_rebuttal":
            logger.info("Task %s needs rebuttal — awaiting further instructions", task_id[:8])
        elif decision == "reject":
            logger.info("Task %s rejected by fleet", task_id[:8])

    async def _execute_merge_decision(self, data: dict):
        """Execute a merge decision locally (gate-checked)."""
        branch = data.get("winner_branch", "")
        if not branch:
            logger.warning("Merge decision has no winner_branch")
            return

        # Check merge gate
        if self._merge_gate:
            result = self._merge_gate.check(branch)
            if not result.passed:
                logger.warning("Merge gate FAILED for %s: %s", branch, result.summary)
                self.evidence.record(
                    "coordinator_fleet_decision",
                    f"Merge gate blocked {branch}",
                    {"branch": branch, "gate_result": result.to_dict()},
                )
                return
            logger.info("Merge gate PASSED for %s", branch)

    async def _execute_cherry_pick(self, data: dict):
        """Log cherry-pick decision for manual execution."""
        cherry_picks = data.get("cherry_pick_from", [])
        logger.info(
            "Cherry-pick decision: %d picks from non-winners",
            len(cherry_picks),
        )

    # ── Task Assignment ──

    async def _handle_task_assignment(self, data: dict):
        """Handle a task assignment from chief."""
        self._stats["tasks_received"] += 1
        payload = data.get("payload", {})
        task_id = data.get("id", data.get("task_id", ""))
        origin = data.get("from", data.get("from_node", "unknown"))

        logger.info("Task assigned: %s from %s", task_id[:8], origin)

        # Record in evidence
        self.evidence.record_task_assigned(task_id, origin, payload)

        # Track task
        self._active_tasks[task_id] = {
            "id": task_id,
            "origin": origin,
            "payload": payload,
            "state": "assigned",
            "started_at": time.time(),
        }

        # Transition task state if available
        if self._task_state:
            try:
                from .task_state import TaskState
                self._task_state.transition(task_id, TaskState.IN_PROGRESS, "coordinator_agent")
            except Exception as e:
                logger.debug("Task state transition failed: %s", e)

        # Mirror task in Superset (fire-and-forget, non-blocking)
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, self._superset_create_task, task_id, payload, origin)

        # Execute task asynchronously
        asyncio.create_task(self._execute_task(task_id, payload, origin))

    async def _execute_task(self, task_id: str, payload: dict, origin: str):
        """Execute a task and report results."""
        t0 = time.monotonic()
        try:
            # Use worker manager for task execution
            from .worker import FleetWorkerManager
            mgr = FleetWorkerManager()
            result = await mgr.dispatch(self.node_id, payload.get("body", payload.get("prompt", "")))

            duration_ms = (time.monotonic() - t0) * 1000
            self._active_tasks.pop(task_id, None)

            if "error" in result:
                self._stats["tasks_failed"] += 1
                self.evidence.record_task_failed(task_id, result["error"])
                self._superset_update_task(task_id, "failed", result["error"])
                if self._task_state:
                    try:
                        from .task_state import TaskState
                        self._task_state.transition(task_id, TaskState.FAILED, result["error"])
                    except Exception:
                        pass
            else:
                self._stats["tasks_completed"] += 1
                self.evidence.record_task_completed(task_id, duration_ms)
                self._superset_update_task(task_id, "completed", f"Done in {duration_ms:.0f}ms")
                if self._task_state:
                    try:
                        from .task_state import TaskState
                        self._task_state.transition(task_id, TaskState.COMPLETED, "done")
                    except Exception:
                        pass

        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            self._active_tasks.pop(task_id, None)
            self._stats["tasks_failed"] += 1
            self.evidence.record_task_failed(task_id, str(e))
            self._superset_update_task(task_id, "failed", str(e)[:500])
            logger.error("Task %s execution error: %s", task_id[:8], e)

    # ── Self-Healing ──

    async def _handle_repair_request(self, data: dict):
        """Handle a repair request from fleet."""
        payload = data.get("payload", {})
        body = payload.get("body", "{}")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {}

        action = body.get("action", "")
        reporter = body.get("reporter", "unknown")

        logger.info("Repair request from %s: action=%s", reporter, action)

        # Check repair gate
        if self._repair_gate:
            result = self._repair_gate.check(
                target=self.node_id,
                action=action,
                node_id=self.node_id,
            )
            if not result.passed:
                logger.warning("Repair gate BLOCKED: %s", result.summary)
                self.evidence.record_self_heal(action, self.node_id, success=False)
                return

        # Execute repair
        success = await self._execute_repair(action)
        self.evidence.record_self_heal(action, self.node_id, success=success)

        if self._repair_gate:
            self._repair_gate.clear_repair(self.node_id)

    async def _execute_repair(self, action: str) -> bool:
        """Execute a local repair action."""
        import subprocess

        repo_dir = os.environ.get("FLEET_REPO_DIR", "$HOME/dev/er-simulator-superrepo")
        REPAIR_COMMANDS = {
            "restart_nats": f"pkill -f fleet_nerve_nats; sleep 2; cd {repo_dir} && nohup python3 tools/fleet_nerve_nats.py serve > /tmp/fleet-nerve.log 2>&1 &",
            "restart_daemon": f"pkill -f fleet_nerve; sleep 2; cd {repo_dir} && nohup python3 tools/fleet_nerve_nats.py serve > /tmp/fleet-nerve.log 2>&1 &",
            "reconnect_nats": "echo 'reconnect_nats: handled by coordinator agent'",
            "restart_eventbus": "pkill -f event_bus_server; sleep 1",
            "clear_dedup": "echo 'dedup cleared'",
        }

        cmd = REPAIR_COMMANDS.get(action)
        if not cmd:
            logger.warning("Unknown repair action: %s", action)
            return False

        # Special case: NATS reconnect is handled in-process
        if action == "reconnect_nats":
            await self._disconnect_nats()
            await self._connect_nats()
            return self._nats_conn is not None

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=30,
                ),
            )
            success = result.returncode == 0
            if not success:
                logger.warning("Repair %s failed: %s", action, result.stderr[:200])
            return success
        except Exception as e:
            logger.error("Repair %s error: %s", action, e)
            return False

    # ── Surgeon Requests ──

    async def _handle_surgeon_request(self, data: dict):
        """Handle a cross-examination or surgical request."""
        payload = data.get("payload", {})
        operation = payload.get("operation", "consensus")
        topic = payload.get("topic", payload.get("body", ""))
        sender = data.get("from", data.get("from_node", "unknown"))

        logger.info("Surgeon request from %s: %s on '%s'", sender, operation, topic[:60])

        if operation == "cardio_review":
            outcome = await self.surgeons.cardio_review(topic)
        elif operation == "neurologist_challenge":
            outcome = await self.surgeons.neurologist_challenge(topic)
        elif operation == "consensus":
            outcome = await self.surgeons.consensus(topic)
        else:
            logger.warning("Unknown surgeon operation: %s", operation)
            return

        # Record in evidence
        self.evidence.record_surgeon_outcome(outcome.to_dict())

        # Send result back to requester via NATS
        if self._nats_conn and sender != self.node_id:
            result_msg = {
                "type": "surgeon_result",
                "from": self.node_id,
                "to": sender,
                "payload": {
                    "operation": operation,
                    "topic": topic[:200],
                    "success": outcome.success,
                    "duration_ms": outcome.duration_ms,
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await self._nats_conn.publish(
                    f"fleet.{sender}.surgeon_result",
                    json.dumps(result_msg).encode(),
                )
            except Exception as e:
                logger.debug("Failed to send surgeon result to %s: %s", sender, e)

    # ── Sync Hold ──

    async def _handle_sync_hold(self, data: dict):
        """Handle a sync hold request — stop local work until released."""
        sender = data.get("from", "unknown")
        reason = data.get("payload", {}).get("reason", "no reason given")
        logger.warning("SYNC HOLD received from %s: %s", sender, reason)
        self.evidence.record(
            "coordinator_fleet_decision",
            f"SYNC HOLD from {sender}",
            {"reason": reason, "from": sender},
        )

    # ── Queue Flush ──

    async def _queue_flush_loop(self):
        """Periodically flush queued evidence and packets."""
        while self._running:
            try:
                self.evidence.flush_queue()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Queue flush error: %s", e)
            await asyncio.sleep(60)

    # ── Initialization Helpers ──

    def _init_gates(self):
        """Initialize invariance gates."""
        try:
            from .invariance_gates import FleetMergeGate, FleetRepairGate, FleetSendGate
            self._send_gate = FleetSendGate()
            self._repair_gate = FleetRepairGate()
            self._merge_gate = FleetMergeGate()
            logger.info("Invariance gates initialized")
        except Exception as e:
            logger.warning("Invariance gates unavailable: %s", e)

    def _init_task_state(self):
        """Initialize task state machine."""
        try:
            from .task_state import TaskStateMachine
            self._task_state = TaskStateMachine()
            logger.info("Task state machine initialized")
        except Exception as e:
            logger.debug("Task state machine unavailable: %s", e)

    # ── Superset Task Sync ──

    def _superset_create_task(self, task_id: str, payload: dict, origin: str):
        """Create a task in Superset mirroring a fleet task. Fire-and-forget."""
        try:
            title = payload.get("subject", payload.get("title", f"Fleet task {task_id[:8]}"))
            desc = payload.get("body", payload.get("prompt", ""))
            description = f"[fleet:{self.node_id}] from={origin}\n{desc[:500]}"
            result = self._superset.create_task(title=title, description=description)
            if isinstance(result, dict) and result.get("_available") is not False:
                # Extract Superset task ID from response
                ss_id = result.get("id", result.get("taskId", ""))
                if ss_id:
                    self._superset_task_map[task_id] = str(ss_id)
                    logger.info("Superset task created: %s -> %s", task_id[:8], str(ss_id)[:8])
                else:
                    logger.debug("Superset task created but no ID in response")
            else:
                logger.debug("Superset unavailable for task create")
        except Exception as e:
            logger.debug("Superset task create failed (non-fatal): %s", e)

    def _superset_update_task(self, task_id: str, status: str, detail: str = ""):
        """Update a Superset task status. Fire-and-forget."""
        ss_id = self._superset_task_map.get(task_id)
        if not ss_id:
            return
        try:
            updates: dict = {"status": status}
            if detail:
                updates["description"] = detail[:500]
            self._superset.update_task(ss_id, updates)
            logger.debug("Superset task %s updated: %s", ss_id[:8], status)
        except Exception as e:
            logger.debug("Superset task update failed (non-fatal): %s", e)

    # ── Status / Introspection ──

    def status(self) -> dict:
        """Return full coordinator agent status."""
        health = self.health.latest
        return {
            "node_id": self.node_id,
            "running": self._running,
            "uptime_s": time.time() - self._stats["started_at"] if self._stats["started_at"] else 0,
            "nats_connected": self._nats_conn is not None,
            "health": health.to_dict() if health else None,
            "surgeons": self.surgeons.get_availability(),
            "surgeons_available": self.surgeons.get_available_count(),
            "active_tasks": len(self._active_tasks),
            "evidence": self.evidence.stats,
            "superset": self._superset.stats,
            "superset_tracked_tasks": len(self._superset_task_map),
            "stats": dict(self._stats),
        }


# ── CLI Entry Point ──

async def _main():
    import argparse

    parser = argparse.ArgumentParser(description="Coordinator Agent daemon")
    parser.add_argument("--node-id", default=None, help="Node ID (default: auto-detect)")
    parser.add_argument("--heartbeat", type=float, default=30.0, help="Heartbeat interval (seconds)")
    parser.add_argument("--health-interval", type=float, default=30.0, help="Health check interval (seconds)")
    parser.add_argument("--nats-url", default=None, help="NATS URL")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [coordinator/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    agent = CoordinatorAgent(
        node_id=args.node_id,
        heartbeat_interval_s=args.heartbeat,
        health_check_interval_s=args.health_interval,
        nats_url=args.nats_url,
    )

    # Handle signals
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(agent.stop()))

    await agent.start()

    # Run until stopped
    try:
        while agent.running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if agent.running:
            await agent.stop()


if __name__ == "__main__":
    asyncio.run(_main())
