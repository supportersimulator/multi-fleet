"""Fleet Onboarding Wizard — guides new users through setup.

Checks what's configured, what's missing, and provides step-by-step
instructions to get a new machine fully operational in the fleet.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.onboarding")

REPO_ROOT = Path(__file__).parent.parent
_SUPERREPO = REPO_ROOT.parent if (REPO_ROOT.parent / ".git").exists() else REPO_ROOT
DAEMON_URL = "http://127.0.0.1:8855"
NATS_PORT = 4222
CONFIG_PATH = REPO_ROOT / ".multifleet" / "config.json"


def _step(name: str, passed: bool, required: bool, message: str,
          fix: str = "") -> dict:
    return {
        "name": name, "passed": passed, "required": required,
        "message": message, "fix": fix,
    }


class OnboardingWizard:
    """Run onboarding checks and render a setup guide."""

    def __init__(self, node_id: str = ""):
        import re
        raw = node_id or os.environ.get(
            "MULTIFLEET_NODE_ID",
            socket.gethostname().split(".")[0].lower(),
        )
        # Sanitize: keep only alphanumeric, dash, underscore. Fallback to hostname.
        if raw and not re.match(r'^[a-z][a-z0-9_-]{0,31}$', raw):
            logger.warning("node_id '%s' contains unsafe characters — sanitizing", raw)
            sanitized = re.sub(r'[^a-z0-9_-]', '', raw.lower())[:32]
            self.node_id = sanitized if sanitized else socket.gethostname().split(".")[0].lower()
        else:
            self.node_id = raw

    # ── Public API ──

    def check_all(self) -> dict:
        """Run all onboarding checks and return status."""
        checks = [
            self._check_python(),
            self._check_nats(),
            self._check_daemon(),
            self._check_keychain(),
            self._check_superset(),
            self._check_config(),
            self._check_ssh(),
            self._check_probes(),
            self._check_surgeons(),
            self._check_skills(),
        ]
        return {
            "node_id": self.node_id,
            "completed": [c for c in checks if c["passed"]],
            "remaining": [c for c in checks if not c["passed"]],
            "ready": all(c["passed"] for c in checks if c["required"]),
            "progress_pct": round(
                sum(1 for c in checks if c["passed"]) / len(checks) * 100, 1
            ),
        }

    def render_guide(self) -> str:
        """Render step-by-step setup guide as markdown."""
        status = self.check_all()
        lines = [
            f"# Fleet Onboarding — {status['node_id']}",
            f"Progress: {status['progress_pct']:.0f}% "
            f"({len(status['completed'])}/{len(status['completed']) + len(status['remaining'])})",
            "",
        ]
        if status["ready"]:
            lines.append("All required checks passed. Node is fleet-ready.")
        else:
            lines.append("## Remaining Steps\n")
            for i, step in enumerate(status["remaining"], 1):
                req = " **(required)**" if step["required"] else ""
                lines.append(f"### {i}. {step['name']}{req}")
                lines.append(f"{step['message']}")
                if step.get("fix"):
                    lines.append(f"\n```bash\n{step['fix']}\n```")
                lines.append("")

        if status["completed"]:
            lines.append("## Completed\n")
            for step in status["completed"]:
                lines.append(f"- [x] {step['name']}: {step['message']}")
        return "\n".join(lines)

    # ── Individual checks ──

    def _check_python(self) -> dict:
        py = shutil.which("python3")
        if not py:
            return _step("python", False, True, "python3 not found in PATH",
                         "brew install python@3.11  # or your package manager")
        try:
            ver = subprocess.check_output(
                [py, "--version"], stderr=subprocess.STDOUT, timeout=5
            ).decode().strip()
        except Exception as e:
            return _step("python", False, True, f"python3 check failed: {e}")
        return _step("python", True, True, ver)

    def _check_nats(self) -> dict:
        try:
            s = socket.create_connection(("127.0.0.1", NATS_PORT), timeout=2)
            s.close()
            return _step("nats", True, True, f"NATS reachable on port {NATS_PORT}")
        except OSError:
            return _step(
                "nats", False, True,
                f"Cannot connect to NATS on port {NATS_PORT}",
                "brew install nats-server && nats-server -p 4222 &",
            )

    def _check_daemon(self) -> dict:
        try:
            import urllib.request
            req = urllib.request.Request(f"{DAEMON_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            return _step("daemon", True, True,
                         f"Daemon running — uptime {data.get('uptime_s', 0):.0f}s")
        except Exception:
            return _step(
                "daemon", False, True,
                "Fleet daemon not responding on port 8855",
                "cd multi-fleet && python3 -m multifleet serve",
            )

    def _check_keychain(self) -> dict:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "fleet-nerve-hmac"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return _step("keychain", True, True, "HMAC key found in Keychain")
            return _step(
                "keychain", False, True,
                "HMAC key not in Keychain",
                'security add-generic-password -a "$USER" -s fleet-nerve-hmac '
                '-w "$(openssl rand -hex 32)" -U',
            )
        except FileNotFoundError:
            return _step("keychain", False, True,
                         "security command not found (non-macOS?)",
                         "export FLEET_HMAC_KEY=$(openssl rand -hex 32)")

    def _check_superset(self) -> dict:
        try:
            from multifleet.superset_bridge import SupersetBridge
            bridge = SupersetBridge()
            if bridge._api_key:
                return _step("superset", True, False,
                             "Superset API key configured")
            return _step("superset", False, False,
                         "Superset API key not set",
                         "Store key in Keychain or set SUPERSET_API_KEY env var")
        except ImportError:
            return _step("superset", False, False,
                         "superset_bridge not importable — optional")

    def _check_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text())
                nodes = list(cfg.get("nodes", {}).keys())
                return _step("config", True, True,
                             f"Config found — nodes: {', '.join(nodes) or 'none'}")
            except (json.JSONDecodeError, OSError) as e:
                return _step("config", False, True, f"Config parse error: {e}")
        return _step(
            "config", False, True,
            ".multifleet/config.json not found",
            "mkdir -p .multifleet && cp codex-config.toml.example .multifleet/config.json",
        )

    def _check_ssh(self) -> dict:
        result = subprocess.run(
            ["ssh-add", "-l"], capture_output=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            count = len(result.stdout.decode().strip().splitlines())
            return _step("ssh", True, False,
                         f"{count} SSH key(s) loaded in agent")
        return _step(
            "ssh", False, False,
            "No SSH keys in agent",
            "ssh-add --apple-use-keychain ~/.ssh/id_ed25519",
        )

    def _check_probes(self) -> dict:
        probes_dir = REPO_ROOT / "multifleet" / "probes"
        if probes_dir.is_dir():
            count = len(list(probes_dir.glob("*.py")))
            return _step("probes", True, False,
                         f"{count} probe modules found")
        return _step("probes", False, False, "Probes directory not found")

    def _check_surgeons(self) -> dict:
        try:
            result = subprocess.run(
                ["claude", "mcp", "list"], capture_output=True, timeout=10,
            )
            output = result.stdout.decode() if result.returncode == 0 else ""
            if "3-surgeons" in output:
                return _step("surgeons", True, False,
                             "3-surgeons MCP plugin detected")
            return _step("surgeons", False, False,
                         "3-surgeons plugin not in MCP list",
                         "Install the 3-surgeons Claude Code plugin")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return _step("surgeons", False, False,
                         "Could not verify 3-surgeons plugin")

    def _check_skills(self) -> dict:
        skills_dir = REPO_ROOT / "skills"
        if skills_dir.is_dir():
            count = len(list(skills_dir.iterdir()))
            return _step("skills", True, False,
                         f"{count} skill(s) installed")
        return _step("skills", False, False, "Skills directory not found")


# ── CLI entry point ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fleet Onboarding Wizard")
    parser.add_argument("--node-id", default="", help="Override node ID")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    wiz = OnboardingWizard(node_id=args.node_id)
    if args.json:
        print(json.dumps(wiz.check_all(), indent=2))
    else:
        print(wiz.render_guide())


if __name__ == "__main__":
    main()
