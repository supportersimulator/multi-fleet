"""fleet init — zero-config scaffolding for multi-fleet setup.

Creates config, detects environment, optionally installs deps and starts services.
Supports --yes for non-interactive (CI) usage.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Config defaults
CONFIG_DIR = Path.home() / ".multifleet"
CONFIG_PATH = CONFIG_DIR / "config.json"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "config" / "config.template.json"
DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
DEFAULT_DAEMON_PORT = 8855


def _detect_node_id() -> str:
    """Derive a node_id from hostname, sanitized for fleet use."""
    hostname = socket.gethostname().lower()
    # Strip .local suffix common on macOS
    if hostname.endswith(".local"):
        hostname = hostname[: -len(".local")]
    # Replace non-alphanumeric chars with dash
    node_id = "".join(c if c.isalnum() else "-" for c in hostname)
    # Collapse consecutive dashes and strip leading/trailing
    while "--" in node_id:
        node_id = node_id.replace("--", "-")
    return node_id.strip("-") or "node1"


def _detect_os() -> str:
    """Return 'macos' or 'linux' (or 'other')."""
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s == "linux":
        return "linux"
    return s


def _nats_installed() -> Optional[str]:
    """Return path to nats-server binary, or None."""
    return shutil.which("nats-server")


def _nats_running() -> bool:
    """Check if NATS is listening on 4222."""
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=1):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def _brew_available() -> bool:
    return shutil.which("brew") is not None


def _daemon_healthy(port: int = DEFAULT_DAEMON_PORT) -> bool:
    """Check if fleet daemon health endpoint responds."""
    try:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            return data.get("status") == "ok"
    except Exception:
        return False


def _confirm(prompt: str, default_yes: bool = True, auto_yes: bool = False) -> bool:
    """Interactive Y/n prompt. Returns True/False. auto_yes skips prompt."""
    if auto_yes:
        return True
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def generate_config(
    node_id: str,
    nats_server: str = DEFAULT_NATS_URL,
    daemon_port: int = DEFAULT_DAEMON_PORT,
) -> dict:
    """Generate a config dict from the template with sensible defaults.

    This is the pure-logic core, no I/O — easy to test.
    """
    # Load template if available, else use minimal defaults
    template = {}
    if TEMPLATE_PATH.exists():
        with open(TEMPLATE_PATH) as f:
            template = json.load(f)

    config = dict(template)
    config["node_id"] = node_id
    config["fleet_nerve_port"] = daemon_port

    # NATS settings
    nats = config.get("nats", {})
    nats["server"] = nats_server
    nats["cluster_name"] = nats.get("cluster_name", "fleet")
    config["nats"] = nats

    # Daemon settings
    daemon = config.get("daemon", {})
    daemon["auto_start"] = True
    log_dir = str(CONFIG_DIR / "logs")
    pid_dir = str(CONFIG_DIR / "run")
    daemon["log_dir"] = log_dir
    daemon["pid_dir"] = pid_dir
    config["daemon"] = daemon

    return config


def detect_environment() -> dict:
    """Detect the local environment and return a summary dict."""
    os_type = _detect_os()
    node_id = _detect_node_id()
    nats_bin = _nats_installed()
    nats_running = _nats_running()
    brew = _brew_available() if os_type == "macos" else False
    daemon_up = _daemon_healthy()
    config_exists = CONFIG_PATH.exists()

    return {
        "os": os_type,
        "node_id": node_id,
        "nats_installed": nats_bin is not None,
        "nats_bin": nats_bin,
        "nats_running": nats_running,
        "brew_available": brew,
        "daemon_running": daemon_up,
        "config_exists": config_exists,
        "config_path": str(CONFIG_PATH),
    }


def write_config(config: dict, path: Path = CONFIG_PATH) -> Path:
    """Write config dict to disk, creating directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path


def run_init(auto_yes: bool = False, node_id_override: Optional[str] = None) -> dict:
    """Run the full interactive init flow. Returns summary dict.

    This is the main entry point called by the CLI handler.
    """
    results = {
        "config_created": False,
        "nats_installed": False,
        "nats_started": False,
        "daemon_started": False,
        "errors": [],
    }

    # --- Step 1: Detect environment ---
    print("\n=== Fleet Init ===\n")
    env = detect_environment()

    print(f"  OS:            {env['os']}")
    print(f"  Node ID:       {env['node_id']}")
    print(f"  NATS installed:{' yes (' + env['nats_bin'] + ')' if env['nats_installed'] else ' no'}")
    print(f"  NATS running:  {'yes' if env['nats_running'] else 'no'}")
    print(f"  Daemon running:{'yes' if env['daemon_running'] else 'no'}")
    print(f"  Config exists: {'yes (' + env['config_path'] + ')' if env['config_exists'] else 'no'}")
    print()

    node_id = node_id_override or env["node_id"]

    # --- Step 2: Create config ---
    if env["config_exists"]:
        if _confirm(f"Config already exists at {env['config_path']}. Overwrite?",
                    default_yes=False, auto_yes=False):
            # Even with --yes, don't overwrite existing config by default
            config = generate_config(node_id)
            write_config(config)
            results["config_created"] = True
            print(f"  Config overwritten: {env['config_path']}")
        else:
            print(f"  Keeping existing config: {env['config_path']}")
    else:
        config = generate_config(node_id)
        write_config(config)
        results["config_created"] = True
        print(f"  Config created: {env['config_path']}")

    # Create subdirectories
    for subdir in ["logs", "run", "data"]:
        (CONFIG_DIR / subdir).mkdir(parents=True, exist_ok=True)
    print(f"  Directories created: {CONFIG_DIR}/{{logs,run,data}}")

    # --- Step 3: Install NATS (optional) ---
    if not env["nats_installed"]:
        if env["os"] == "macos" and env["brew_available"]:
            if _confirm("NATS not installed. Install via Homebrew?", auto_yes=auto_yes):
                try:
                    print("  Installing nats-server via brew...")
                    subprocess.run(
                        ["brew", "install", "nats-server"],
                        check=True, capture_output=True, text=True,
                    )
                    results["nats_installed"] = True
                    print("  NATS installed successfully")
                except subprocess.CalledProcessError as exc:
                    msg = f"brew install failed: {exc.stderr[:200]}"
                    results["errors"].append(msg)
                    print(f"  WARN: {msg}")
            else:
                print("  Skipping NATS install")
        elif env["os"] == "linux":
            print("  NATS not installed. Install manually:")
            print("    curl -sf https://binaries.nats.dev/nats-io/nats-server/v2@latest | sh")
            print("    sudo mv nats-server /usr/local/bin/")
        else:
            print("  NATS not installed. See https://docs.nats.io/running-a-nats-service/introduction/installation")
    else:
        results["nats_installed"] = True
        print(f"  NATS already installed: {env['nats_bin']}")

    # --- Step 4: Start services ---
    # Start NATS if not running
    nats_bin = _nats_installed()
    if nats_bin and not _nats_running():
        if _confirm("Start NATS server?", auto_yes=auto_yes):
            try:
                log_path = CONFIG_DIR / "logs" / "nats.log"
                subprocess.Popen(
                    [nats_bin, "-p", "4222", "--log", str(log_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Brief wait for startup
                time.sleep(0.5)
                if _nats_running():
                    results["nats_started"] = True
                    print(f"  NATS started (log: {log_path})")
                else:
                    results["errors"].append("NATS started but not responding on 4222")
                    print("  WARN: NATS process started but not responding yet")
            except Exception as exc:
                msg = f"Failed to start NATS: {exc}"
                results["errors"].append(msg)
                print(f"  WARN: {msg}")
        else:
            print("  Skipping NATS start")
    elif _nats_running():
        results["nats_started"] = True
        print("  NATS already running")

    # Start fleet daemon
    if not env["daemon_running"]:
        if _confirm("Start fleet daemon?", auto_yes=auto_yes):
            try:
                daemon_env = os.environ.copy()
                daemon_env["MULTIFLEET_NODE_ID"] = node_id
                if _nats_running():
                    daemon_env["NATS_URL"] = DEFAULT_NATS_URL

                # Find the fleet_nerve_nats.py or use the module
                daemon_cmd = [
                    sys.executable, "-m", "multifleet.daemon",
                ]
                # Try module first, fall back to script
                try:
                    subprocess.Popen(
                        daemon_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=daemon_env,
                    )
                except Exception:
                    # Fallback: look for fleet_nerve_nats.py in tools/
                    tools_script = Path(__file__).resolve().parent.parent.parent / "tools" / "fleet_nerve_nats.py"
                    if tools_script.exists():
                        subprocess.Popen(
                            [sys.executable, str(tools_script), "serve"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            env=daemon_env,
                        )

                time.sleep(1)
                if _daemon_healthy():
                    results["daemon_started"] = True
                    print(f"  Fleet daemon started (node: {node_id})")
                else:
                    print("  WARN: Daemon process started but health check not responding yet")
                    results["errors"].append("Daemon started but health check failed")
            except Exception as exc:
                msg = f"Failed to start daemon: {exc}"
                results["errors"].append(msg)
                print(f"  WARN: {msg}")
        else:
            print("  Skipping daemon start")
    else:
        results["daemon_started"] = True
        print("  Fleet daemon already running")

    # --- Step 5: Print summary ---
    print("\n=== Setup Complete ===\n")
    print(f"  Node ID:    {node_id}")
    print(f"  Config:     {CONFIG_PATH}")
    print(f"  NATS:       {'running' if _nats_running() else 'not running'}")
    print(f"  Daemon:     {'healthy' if _daemon_healthy() else 'not running'}")

    if results["errors"]:
        print(f"\n  Warnings ({len(results['errors'])}):")
        for e in results["errors"]:
            print(f"    - {e}")

    print("\n  Next steps:")
    print("    fleet status          # Check fleet health")
    print("    fleet doctor          # Run diagnostics")
    if not _nats_running():
        print("    nats-server &         # Start NATS server")
    print("    fleet send mac2 'hi'  # Send a message to another node")
    print()
    print("  To add more nodes, run 'fleet init' on each machine.")
    print("  Edit ~/.multifleet/config.json to configure SSH tunnels for remote peers.")
    print()

    results["node_id"] = node_id
    results["config_path"] = str(CONFIG_PATH)
    return results
