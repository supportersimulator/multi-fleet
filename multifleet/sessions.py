"""
Session management and injection for fleet messaging.

Extracted from nats_client.py — contains:
- SessionManager: session detection, seed injection, keystroke triggers,
  Claude --resume injection, notifications, VS Code state detection
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .nats_client import FleetNerveNATS

logger = logging.getLogger("fleet_nats")

from ._paths import REPO_ROOT

SEED_DIR = "/tmp"

# UUID v4 format: 8-4-4-4-12 hex chars = 36 chars total
_UUID_LEN = 36


def normalize_session_id(sid: str, active_sessions: list | None = None) -> str:
    """Ensure session ID is full UUID format.

    If already a full UUID (>=36 chars), return as-is.
    If truncated, try to resolve from active sessions list.
    Returns the original string if no match found (graceful degradation).

    Args:
        sid: Session ID, possibly truncated.
        active_sessions: List of session dicts with 'sessionId' key.
            If None, attempts to detect active sessions automatically.
    """
    if not sid or len(sid) >= _UUID_LEN:
        return sid

    # Try to find matching full ID from provided sessions
    if active_sessions is not None:
        matches = [
            s.get("sessionId", "")
            for s in active_sessions
            if s.get("sessionId", "").startswith(sid)
        ]
        if len(matches) == 1:
            return matches[0]
        return sid  # ambiguous or no match — return original

    # Auto-detect: scan PID session files for a match
    sessions_dir = Path.home() / ".claude" / "sessions"
    if sessions_dir.exists():
        for pid_file in sessions_dir.glob("*.json"):
            try:
                pid = int(pid_file.stem)
                os.kill(pid, 0)
                data = json.loads(pid_file.read_text())
                full_id = data.get("sessionId", "")
                if full_id.startswith(sid):
                    return full_id
            except (OSError, ValueError, json.JSONDecodeError):
                pass

    # Fallback: scan JSONL filenames
    projects_dir = Path.home() / ".claude" / "projects"
    if projects_dir.exists():
        cutoff = time.time() - 1800
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                try:
                    if jsonl.stat().st_mtime > cutoff and jsonl.stem.startswith(sid):
                        return jsonl.stem
                except OSError:
                    pass

    return sid  # no match — return original


def display_session_id(sid: str) -> str:
    """Truncate session ID for human-readable display (12 chars)."""
    return sid[:12] if sid else "?"


class SessionManager:
    """Session detection, injection, and notification manager.

    Delegates to the parent FleetNerveNATS (``nerve``) for node identity,
    event loop, and message sending.  All other state is local.
    """

    _last_keystroke = 0.0  # Class-level rate limit: max 1 keystroke per 30s

    def __init__(self, nerve: FleetNerveNATS):
        self._nerve = nerve

    # ── Formatting / Resume ──

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
                except (OSError, ValueError, json.JSONDecodeError):
                    pass

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
            session_id: Full session UUID (or truncated — will be normalized)
            message: Pre-formatted message text

        Returns:
            True if injection succeeded, False otherwise.
            Never logs message content -- only type and delivery status.
        """
        # Ensure we have full UUID for --resume
        session_id = normalize_session_id(session_id)
        display_id = display_session_id(session_id)
        try:
            cmd = ["claude", "--resume", session_id, "--message", message]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f"resume-inject: success session={display_id}")
                return True
            logger.warning(f"resume-inject: failed rc={result.returncode} session={display_id}")
            return False
        except Exception as e:
            logger.warning(f"resume-inject: error session={display_id}: {e}")
            return False

    # ── Seed / Injection ──

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
        return os.path.join(SEED_DIR, f"fleet-seed-{self._nerve.node_id}.md")

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

    # ── Keystroke / Notification ──

    def _keystroke_trigger(self, summary: str):
        """Focus VS Code and type a trigger prompt so the hook fires automatically.

        Uses osascript to simulate user input. Only fires if VS Code is running
        and no Claude agent is mid-response (checks for idle session).
        Rate-limited: max 1 keystroke per 30s to prevent spam when multiple messages arrive.
        """
        # Rate limit -- don't spam keystrokes
        now = time.time()
        if now - SessionManager._last_keystroke < 30:
            logger.debug(f"Keystroke throttled ({int(30 - (now - SessionManager._last_keystroke))}s remaining)")
            return
        SessionManager._last_keystroke = now
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

        # Capture nerve reference for use in nested closure
        nerve = self._nerve

        def _send_result(response: str, status: str = "completed"):
            """Send task_result back to the dispatching node."""
            if nerve._loop and sender != nerve.node_id:
                result_msg = {
                    "type": "task_result", "from": nerve.node_id, "to": sender,
                    "payload": {"dispatch_id": dispatch_id, "status": status, "response": response[:2000]},
                    "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                }
                asyncio.run_coroutine_threadsafe(
                    nerve.send_with_fallback(sender, result_msg), nerve._loop)

        # Prefer session-driver worker (runs in tmux, never disrupts user)
        try:
            from .worker import FleetWorkerManager
            mgr = FleetWorkerManager()
            worker_name = f"fleet-task-{task_id}"
            worker = mgr.launch_worker(nerve.node_id, worker_name)
            if "error" not in worker:
                # Send task in background thread (don't block message handler)
                def _run_task():
                    try:
                        response = mgr.send_task(worker_name, prompt, timeout=timeout_s)
                        mgr.stop_worker(worker_name)
                        logger.info(f"Worker {worker_name} completed: {response[:100]}...")
                        # Chain advancement: report completion
                        if chain_id:
                            nerve._chain_step_done(chain_id, sender, True, response[:2000])
                        _send_result(response, "completed")
                    except Exception as e:
                        logger.warning(f"Worker {worker_name} failed: {e}")
                        if chain_id:
                            nerve._chain_step_done(chain_id, sender, False, str(e))
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

    # ── Utilities ──

    def _notify(self, message: str):
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "Fleet Nerve" sound name "Glass"'],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    def _find_claude(self) -> Optional[str]:
        for path in ["/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
            if os.path.exists(path):
                return path
        return None

    # ── VS Code / Session Detection ──

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
        except Exception:
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
                        "sessionId": data.get("sessionId", pid_file.stem),
                        "pid": pid,
                        "kind": data.get("kind", "?"),
                        "entrypoint": data.get("entrypoint", "?"),
                    })
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
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
                            sid = jsonl.stem
                            # Update idle_s on existing session or add new one
                            existing = next((s for s in sessions
                                             if s["sessionId"] == sid
                                             or s["sessionId"].startswith(sid[:12])), None)
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
                    except OSError:
                        pass
        return sessions
