#!/usr/bin/env python3
"""
Fleet Remote Worker — orchestrates claude-session-driver workers across machines.

Solves: autonomous fleet coordination WITHOUT disrupting human typing.
Workers run in detached tmux sessions, invisible to the user.

Architecture:
  - Local workers: tmux sessions on this machine
  - Remote workers: SSH → tmux sessions on peer machines
  - Results: read via event stream, never injected into user's active session
  - User sees summaries only when they run fleet-check

Usage:
  from multifleet.worker import FleetWorkerManager
  mgr = FleetWorkerManager()
  result = await mgr.dispatch("mac2", "Run tests and report failures")
  print(result)  # Worker's response text
"""

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent

# Session-driver scripts location
SCRIPTS_DIR = None
for candidate in Path.home().glob(".claude/plugins/cache/*/claude-session-driver/*/scripts"):
    if (candidate / "launch-worker.sh").exists():
        SCRIPTS_DIR = candidate
        break


def _find_scripts() -> Path:
    """Find session-driver scripts directory."""
    if SCRIPTS_DIR:
        return SCRIPTS_DIR
    raise RuntimeError("claude-session-driver scripts not found. Install the plugin.")


def _get_config() -> dict:
    """Load fleet config."""
    cfg_path = REPO_ROOT / ".multifleet" / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text())
    return {"nodes": {}, "chief": {}}


def _get_node_id() -> str:
    return os.environ.get("MULTIFLEET_NODE_ID", socket.gethostname().split(".")[0].lower())


class FleetWorkerManager:
    """Manage fleet workers via session-driver, locally and remotely."""

    def __init__(self):
        self.scripts = _find_scripts()
        self.config = _get_config()
        self.node_id = _get_node_id()
        self.active_workers = {}  # worker_name → {session_id, target, started_at}
        self._results_dir = Path("/tmp/fleet-worker-results")
        self._results_dir.mkdir(exist_ok=True)

    def _peer_ssh(self, target: str) -> Optional[str]:
        """Get ssh user@ip for a target node. None if local."""
        if target == self.node_id:
            return None
        node = self.config.get("nodes", {}).get(target, {})
        user = node.get("user", os.environ.get("USER", ""))
        ip = node.get("host", "")
        if not ip:
            return None
        return f"{user}@{ip}"

    def _run(self, cmd: list, timeout: int = 30, ssh_target: str = None) -> subprocess.CompletedProcess:
        """Run a command locally or via SSH."""
        if ssh_target:
            ssh_cmd = [
                "ssh", "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                ssh_target,
                " ".join(cmd),
            ]
            return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def launch_worker(self, target: str, worker_name: str, project_dir: str = None) -> dict:
        """Launch a worker session on target node.

        Returns: {"session_id": "...", "tmux_name": "...", "target": "..."}
        """
        if not project_dir:
            project_dir = str(REPO_ROOT)

        ssh = self._peer_ssh(target)
        scripts = str(self.scripts)

        # For remote: scripts must exist on the remote machine too
        if ssh:
            # Use the remote machine's plugin path
            cmd = [
                "bash", "-c",
                f'SCRIPTS=$(find ~/.claude/plugins/cache -path "*/claude-session-driver/*/scripts/launch-worker.sh" | head -1 | xargs dirname); '
                f'"$SCRIPTS/launch-worker.sh" {worker_name} {project_dir}'
            ]
        else:
            cmd = [str(self.scripts / "launch-worker.sh"), worker_name, project_dir]

        result = self._run(cmd, timeout=45, ssh_target=ssh)
        if result.returncode != 0:
            return {"error": result.stderr.strip(), "target": target}

        try:
            data = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON: {result.stdout[:200]}", "target": target}

        worker = {
            "session_id": data.get("session_id", ""),
            "tmux_name": data.get("tmux_name", worker_name),
            "target": target,
            "started_at": time.time(),
        }
        self.active_workers[worker_name] = worker
        return worker

    def send_task(self, worker_name: str, prompt: str, timeout: int = 300) -> str:
        """Send a prompt to a worker and wait for response.

        Returns the worker's text response.
        """
        worker = self.active_workers.get(worker_name)
        if not worker:
            return f"Error: worker '{worker_name}' not found"

        ssh = self._peer_ssh(worker["target"])

        if ssh:
            cmd = [
                "bash", "-c",
                f'SCRIPTS=$(find ~/.claude/plugins/cache -path "*/claude-session-driver/*/scripts/converse.sh" | head -1 | xargs dirname); '
                f'"$SCRIPTS/converse.sh" {worker["tmux_name"]} {worker["session_id"]} "{prompt}" {timeout}'
            ]
        else:
            cmd = [
                str(self.scripts / "converse.sh"),
                worker["tmux_name"],
                worker["session_id"],
                prompt,
                str(timeout),
            ]

        result = self._run(cmd, timeout=timeout + 30, ssh_target=ssh)
        response = result.stdout.strip() if result.returncode == 0 else f"Error: {result.stderr.strip()}"

        # Save result
        result_file = self._results_dir / f"{worker_name}-{int(time.time())}.json"
        result_file.write_text(json.dumps({
            "worker": worker_name,
            "target": worker["target"],
            "prompt": prompt[:200],
            "response": response[:2000],
            "timestamp": time.time(),
        }, indent=2))

        return response

    def stop_worker(self, worker_name: str) -> bool:
        """Stop a worker session."""
        worker = self.active_workers.get(worker_name)
        if not worker:
            return False

        ssh = self._peer_ssh(worker["target"])

        if ssh:
            cmd = [
                "bash", "-c",
                f'SCRIPTS=$(find ~/.claude/plugins/cache -path "*/claude-session-driver/*/scripts/stop-worker.sh" | head -1 | xargs dirname); '
                f'"$SCRIPTS/stop-worker.sh" {worker["tmux_name"]} {worker["session_id"]}'
            ]
        else:
            cmd = [
                str(self.scripts / "stop-worker.sh"),
                worker["tmux_name"],
                worker["session_id"],
            ]

        result = self._run(cmd, timeout=15, ssh_target=ssh)
        del self.active_workers[worker_name]
        return result.returncode == 0

    def list_workers(self) -> list:
        """List active workers."""
        return [
            {**w, "age_s": int(time.time() - w["started_at"])}
            for w in self.active_workers.values()
        ]

    def get_recent_results(self, limit: int = 5) -> list:
        """Get recent worker results for fleet-check display."""
        results = []
        for f in sorted(self._results_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                results.append(json.loads(f.read_text()))
            except Exception:
                pass
        return results

    async def dispatch(self, target: str, task: str, timeout: int = 300) -> dict:
        """Full dispatch: launch worker → send task → get result → stop worker.

        This is the main entry point for fleet task coordination.
        Returns: {"target": "...", "response": "...", "duration_s": N}
        """
        worker_name = f"fleet-{target}-{int(time.time()) % 10000}"
        start = time.time()

        # Launch
        worker = self.launch_worker(target, worker_name)
        if "error" in worker:
            return {"target": target, "error": worker["error"], "duration_s": 0}

        # Send task and get response
        response = self.send_task(worker_name, task, timeout=timeout)

        # Stop
        self.stop_worker(worker_name)

        return {
            "target": target,
            "response": response,
            "duration_s": int(time.time() - start),
        }


# ── CLI for quick testing ──
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: fleet_remote_worker.py <target> <task>")
        print("  target: mac1, mac2, mac3 (or 'local')")
        print("  task: prompt to send to the worker")
        sys.exit(1)

    target = sys.argv[1]
    task = " ".join(sys.argv[2:])

    mgr = FleetWorkerManager()
    result = asyncio.run(mgr.dispatch(target if target != "local" else mgr.node_id, task))
    print(json.dumps(result, indent=2))
