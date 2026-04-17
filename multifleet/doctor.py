#!/usr/bin/env python3
"""
Fleet Doctor — structured diagnostics for multi-fleet health.

Runs 14 diagnostic checks and reports structured results with codes.
Inspired by 3-surgeons' doctor command — provides machine-readable
health status for fleet operations.

Usage:
  from multifleet.doctor import FleetDoctor
  doctor = FleetDoctor()
  report = doctor.run()
  print(report.summary())

  # Or CLI:
  python3 -m multifleet.doctor
  python3 -m multifleet.doctor --json
"""

import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.doctor")


@dataclass
class Check:
    """Result of a single diagnostic check."""
    code: str           # e.g., "FD-001"
    name: str           # e.g., "daemon_running"
    status: str         # "pass", "fail", "warn", "skip"
    message: str        # human-readable detail
    latency_ms: float = 0.0
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"code": self.code, "name": self.name, "status": self.status,
             "message": self.message, "latency_ms": round(self.latency_ms, 1)}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class DoctorReport:
    """Complete diagnostic report."""
    checks: list[Check] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: float = 0.0
    node_id: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def warned(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def healthy(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        lines = [f"Fleet Doctor — {self.node_id}"]
        lines.append(f"{'=' * 40}")
        for c in self.checks:
            icon = {"pass": "+", "fail": "X", "warn": "!", "skip": "-"}[c.status]
            lines.append(f"  [{icon}] {c.code} {c.name}: {c.message}")
        lines.append(f"{'=' * 40}")
        lines.append(f"  {self.passed} passed, {self.failed} failed, {self.warned} warnings")
        lines.append(f"  Total: {self.duration_ms:.0f}ms")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "healthy": self.healthy,
            "passed": self.passed,
            "failed": self.failed,
            "warned": self.warned,
            "duration_ms": round(self.duration_ms, 1),
            "checks": [c.to_dict() for c in self.checks],
        }


DAEMON_URL = "http://127.0.0.1:8855"
NATS_PORT = 4222
from ._paths import REPO_ROOT, get_git_root

_SUPERREPO = get_git_root()


# TODO: Migrate these checks to ProbeResult contract (see probes/fleet_health.py)
# The fleet_health probe already wraps doctor — future: doctor becomes a thin wrapper
# that runs fleet_health + other health probes via the probe engine.
# Checks to migrate: FD-001 through FD-014 (14 total).

class FleetDoctor:
    """Run structured diagnostic checks on fleet health."""

    def __init__(self):
        self.node_id = os.environ.get(
            "MULTIFLEET_NODE_ID",
            socket.gethostname().split(".")[0].lower()
        )

    def run(self) -> DoctorReport:
        """Run all diagnostic checks and return report."""
        report = DoctorReport(started_at=time.time(), node_id=self.node_id)

        checks = [
            self._check_daemon,
            self._check_nats,
            self._check_peers,
            self._check_ssh_agent,
            self._check_keychain_keys,
            self._check_git_status,
            self._check_config,
            self._check_packet_registry,
            self._check_superset,
            self._check_surgeons_neurologist,
            self._check_surgeons_cardiologist,
            self._check_message_store,
            self._check_fleet_messages,
            self._check_launchagents,
            self._check_memory_subsystems,
        ]

        for check_fn in checks:
            t0 = time.monotonic()
            try:
                result = check_fn()
                result.latency_ms = (time.monotonic() - t0) * 1000
            except Exception as e:
                result = Check(
                    code="FD-ERR", name=check_fn.__name__,
                    status="fail", message=f"Check crashed: {e}",
                    latency_ms=(time.monotonic() - t0) * 1000
                )
            report.checks.append(result)

        report.duration_ms = (time.time() - report.started_at) * 1000
        return report

    def _check_daemon(self) -> Check:
        """FD-001: Fleet daemon running and responsive."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            uptime = data.get("uptime_s", 0)
            return Check("FD-001", "daemon_running", "pass",
                         f"Up {uptime}s, {data.get('activeSessions', 0)} sessions",
                         detail={"uptime_s": uptime, "sessions": data.get("activeSessions", 0)})
        except Exception as e:
            return Check("FD-001", "daemon_running", "fail", f"Not responding: {e}")

    def _check_nats(self) -> Check:
        """FD-002: NATS server reachable on port 4222."""
        try:
            s = socket.create_connection(("127.0.0.1", NATS_PORT), timeout=2)
            info = s.recv(1024).decode("utf-8", errors="replace")
            s.close()
            if "INFO" in info:
                return Check("FD-002", "nats_server", "pass", "NATS server responding")
            return Check("FD-002", "nats_server", "warn", "Port open but no INFO received")
        except Exception as e:
            return Check("FD-002", "nats_server", "fail", f"Not reachable: {e}")

    def _check_peers(self) -> Check:
        """FD-003: At least one peer visible."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            peers = data.get("peers", {})
            count = len(peers)
            if count >= 2:
                return Check("FD-003", "peers_visible", "pass", f"{count} peers online")
            elif count == 1:
                return Check("FD-003", "peers_visible", "warn", "Only 1 peer visible")
            return Check("FD-003", "peers_visible", "fail", "No peers visible")
        except Exception:
            return Check("FD-003", "peers_visible", "skip", "Daemon not available")

    def _check_ssh_agent(self) -> Check:
        """FD-004: SSH agent has loaded keys."""
        try:
            r = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                count = len(r.stdout.strip().splitlines())
                return Check("FD-004", "ssh_agent", "pass", f"{count} key(s) loaded")
            return Check("FD-004", "ssh_agent", "warn", "No keys loaded (ssh-add -l empty)")
        except Exception as e:
            return Check("FD-004", "ssh_agent", "fail", f"SSH agent check failed: {e}")

    def _check_keychain_keys(self) -> Check:
        """FD-005: Required API keys in Keychain."""
        keys_needed = [
            ("fleet-nerve", "Context_DNA_OPENAI"),
            ("fleet-nerve", "Superset_contextdna_key"),
        ]
        found = 0
        missing = []
        for svc, acct in keys_needed:
            try:
                r = subprocess.run(
                    ["security", "find-generic-password", "-s", svc, "-a", acct, "-w"],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    found += 1
                else:
                    missing.append(acct)
            except Exception:
                missing.append(acct)

        if found == len(keys_needed):
            return Check("FD-005", "keychain_keys", "pass", f"{found}/{len(keys_needed)} keys present")
        return Check("FD-005", "keychain_keys", "warn",
                     f"{found}/{len(keys_needed)} keys (missing: {', '.join(missing)})",
                     detail={"missing": missing})

    def _check_git_status(self) -> Check:
        """FD-006: Git repo clean and on expected branch."""
        try:
            r = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                               text=True, timeout=5, cwd=str(_SUPERREPO))
            dirty = len(r.stdout.strip().splitlines()) if r.stdout.strip() else 0
            branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True,
                                    text=True, timeout=3, cwd=str(_SUPERREPO)).stdout.strip()
            if dirty == 0:
                return Check("FD-006", "git_status", "pass", f"Clean on {branch}")
            return Check("FD-006", "git_status", "warn", f"{dirty} changed files on {branch}",
                         detail={"dirty_files": dirty, "branch": branch})
        except Exception as e:
            return Check("FD-006", "git_status", "fail", f"Git check failed: {e}")

    def _check_config(self) -> Check:
        """FD-007: Fleet config exists and has valid structure."""
        cfg_path = _SUPERREPO / ".multifleet" / "config.json"
        if not cfg_path.exists():
            return Check("FD-007", "config_file", "fail", "No .multifleet/config.json")
        try:
            cfg = json.loads(cfg_path.read_text())
            nodes = len(cfg.get("nodes", {}))
            return Check("FD-007", "config_file", "pass", f"Valid config with {nodes} nodes")
        except Exception as e:
            return Check("FD-007", "config_file", "fail", f"Invalid config: {e}")

    def _check_packet_registry(self) -> Check:
        """FD-008: Packet registry loaded with types."""
        try:
            from multifleet.packet_registry import PacketRegistry
            reg = PacketRegistry()
            types = reg.list_types()
            count = len(types)
            if count > 0:
                return Check("FD-008", "packet_registry", "pass",
                             f"{count} types registered", detail={"types": types})
            return Check("FD-008", "packet_registry", "warn", "No types registered")
        except ImportError:
            return Check("FD-008", "packet_registry", "skip", "packet_registry not available")
        except Exception as e:
            return Check("FD-008", "packet_registry", "fail", f"Registry error: {e}")

    def _check_superset(self) -> Check:
        """FD-009: Superset bridge configured and reachable."""
        try:
            from multifleet.superset_bridge import SupersetBridge
            bridge = SupersetBridge()
            if not bridge._api_key:
                return Check("FD-009", "superset_bridge", "warn", "No API key configured")
            return Check("FD-009", "superset_bridge", "pass", "Bridge configured with API key")
        except ImportError:
            return Check("FD-009", "superset_bridge", "skip", "superset_bridge not available")
        except Exception as e:
            return Check("FD-009", "superset_bridge", "fail", f"Bridge error: {e}")

    def _check_surgeons_neurologist(self) -> Check:
        """FD-010: Neurologist (local LLM) reachable."""
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:5044/v1/models", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return Check("FD-010", "neurologist", "pass", "Local LLM responding")
        except Exception:
            return Check("FD-010", "neurologist", "warn", "Local LLM not reachable (non-blocking)")

    def _check_surgeons_cardiologist(self) -> Check:
        """FD-011: Cardiologist API key available."""
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "fleet-nerve",
                 "-a", "Context_DNA_OPENAI", "-w"],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return Check("FD-011", "cardiologist_key", "pass", "OpenAI key in Keychain")
            # Try OPENAI_API_KEY keychain
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "OPENAI_API_KEY", "-w"],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return Check("FD-011", "cardiologist_key", "pass", "OpenAI key in Keychain (legacy)")
            return Check("FD-011", "cardiologist_key", "warn", "No OpenAI key found")
        except Exception:
            return Check("FD-011", "cardiologist_key", "warn", "Keychain check failed")

    def _check_message_store(self) -> Check:
        """FD-012: Message store accessible."""
        db_path = Path.home() / ".fleet-nerve" / "messages.db"
        if not db_path.exists():
            return Check("FD-012", "message_store", "fail", "messages.db not found")
        size_mb = db_path.stat().st_size / (1024 * 1024)
        # Check WAL size
        wal_path = db_path.with_suffix(".db-wal")
        wal_mb = wal_path.stat().st_size / (1024 * 1024) if wal_path.exists() else 0
        if wal_mb > 10:
            return Check("FD-012", "message_store", "warn",
                         f"DB {size_mb:.1f}MB, WAL {wal_mb:.1f}MB (large — consider checkpoint)",
                         detail={"db_mb": round(size_mb, 1), "wal_mb": round(wal_mb, 1)})
        return Check("FD-012", "message_store", "pass",
                     f"DB {size_mb:.1f}MB, WAL {wal_mb:.1f}MB",
                     detail={"db_mb": round(size_mb, 1), "wal_mb": round(wal_mb, 1)})

    def _check_fleet_messages(self) -> Check:
        """FD-013: Fleet message inbox is clean (no unprocessed messages)."""
        inbox = _SUPERREPO / ".fleet-messages" / self.node_id
        if not inbox.exists():
            return Check("FD-013", "fleet_inbox", "warn", f"No inbox dir for {self.node_id}")
        msgs = [f for f in inbox.iterdir() if f.suffix == ".md" and f.name != ".gitkeep"]
        if msgs:
            return Check("FD-013", "fleet_inbox", "warn",
                         f"{len(msgs)} unprocessed message(s)",
                         detail={"files": [f.name for f in msgs[:5]]})
        return Check("FD-013", "fleet_inbox", "pass", "Inbox clean")

    def _check_launchagents(self) -> Check:
        """FD-014: Required LaunchAgents loaded."""
        required = [
            "io.contextdna.fleet-nats",
            "io.contextdna.fleet-inbox-watcher",
        ]
        loaded = []
        missing = []
        for label in required:
            try:
                r = subprocess.run(
                    ["launchctl", "list", label],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0:
                    loaded.append(label)
                else:
                    missing.append(label)
            except Exception:
                missing.append(label)

        if not missing:
            return Check("FD-014", "launchagents", "pass", f"{len(loaded)} agents loaded")
        return Check("FD-014", "launchagents", "warn",
                     f"{len(loaded)} loaded, {len(missing)} missing: {', '.join(missing)}",
                     detail={"missing": missing})

    def _check_memory_subsystems(self) -> Check:
        """FD-015: Context DNA memory subsystem health (memory/comprehensive_health_monitor.py)."""
        try:
            from memory.comprehensive_health_monitor import run_all_checks
            health = run_all_checks()
            healthy = health.healthy_count
            degraded = health.degraded_count
            down = health.down_count
            total = healthy + degraded + down + health.unknown_count

            if down > 0:
                return Check(
                    "FD-015", "memory_subsystems", "fail",
                    f"{down}/{total} memory subsystems DOWN, {degraded} degraded",
                    detail={"healthy": healthy, "degraded": degraded, "down": down,
                            "alerts": health.alerts[:5]},
                )
            if degraded > 0:
                return Check(
                    "FD-015", "memory_subsystems", "warn",
                    f"{degraded}/{total} memory subsystems degraded, {healthy} healthy",
                    detail={"healthy": healthy, "degraded": degraded},
                )
            return Check(
                "FD-015", "memory_subsystems", "pass",
                f"{healthy}/{total} memory subsystems healthy",
            )
        except ImportError:
            return Check("FD-015", "memory_subsystems", "skip",
                         "memory.comprehensive_health_monitor not available")
        except Exception as e:
            return Check("FD-015", "memory_subsystems", "warn",
                         f"Health check error: {e}")


if __name__ == "__main__":
    import sys
    doctor = FleetDoctor()
    report = doctor.run()
    if "--json" in sys.argv:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())
    sys.exit(0 if report.healthy else 1)
