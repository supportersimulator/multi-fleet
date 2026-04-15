"""Fleet Power State Manager — 7-state model for fleet node lifecycle.

States (from chat design):
    asleep        → no heartbeat, no agent, requires WoL
    waking        → WoL sent, waiting for response
    warm-idle     → node awake, no active session
    session-hot   → IDE/Claude Code active, safe to route work
    busy-protected → active job + keepawake lock asserted
    cooldown      → recently finished, hold awake N minutes before parking
    parking       → transitioning to sleep

Transitions:
    asleep → waking (WoL/SSH)
    waking → warm-idle (node responds to ping/health)
    warm-idle → session-hot (IDE opens / Claude Code starts)
    session-hot → busy-protected (job starts, caffeinate asserted)
    busy-protected → cooldown (job finishes)
    cooldown → warm-idle (cooldown timer expires, more work available)
    cooldown → parking → asleep (no pending work)
    * → asleep (unexpected loss of connectivity)

Integration:
    - Fleet daemon (port 8855) reports state via /health
    - caffeinate controls sleep inhibition on macOS
    - GhostCheckpoint captures state before transitions
    - Fleet messages broadcast state changes to all nodes
"""

import json
import logging
import os
import signal
import subprocess
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.power-state")

STATE_FILE = Path("/tmp/fleet-power-state.json")
CAFFEINATE_PID = Path("/tmp/fleet-caffeinate.pid")
COOLDOWN_SECONDS = int(os.environ.get("FLEET_COOLDOWN_SECONDS", "300"))  # 5 min


class PowerState(str, Enum):
    ASLEEP = "asleep"
    WAKING = "waking"
    WARM_IDLE = "warm-idle"
    SESSION_HOT = "session-hot"
    BUSY_PROTECTED = "busy-protected"
    COOLDOWN = "cooldown"
    PARKING = "parking"


# Valid state transitions
_TRANSITIONS: dict[PowerState, set[PowerState]] = {
    PowerState.ASLEEP: {PowerState.WAKING},
    PowerState.WAKING: {PowerState.WARM_IDLE, PowerState.ASLEEP},
    PowerState.WARM_IDLE: {PowerState.SESSION_HOT, PowerState.PARKING, PowerState.ASLEEP},
    PowerState.SESSION_HOT: {PowerState.BUSY_PROTECTED, PowerState.WARM_IDLE, PowerState.ASLEEP},
    PowerState.BUSY_PROTECTED: {PowerState.COOLDOWN, PowerState.SESSION_HOT, PowerState.ASLEEP},
    PowerState.COOLDOWN: {PowerState.WARM_IDLE, PowerState.PARKING, PowerState.SESSION_HOT, PowerState.ASLEEP},
    PowerState.PARKING: {PowerState.ASLEEP, PowerState.WARM_IDLE},
}


class PowerStateManager:
    """Manages power state lifecycle for a fleet node.

    Integrates with:
    - caffeinate (macOS sleep inhibition)
    - Fleet daemon (/health endpoint)
    - GhostCheckpoint (state preservation on transitions)
    - Fleet messages (P7 broadcast on state changes)
    """

    def __init__(self, repo_root: str = "."):
        self._repo = Path(repo_root).resolve()
        self._node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._state = PowerState.WARM_IDLE
        self._state_since: float = time.time()
        self._cooldown_start: float = 0.0
        self._load_state()

    @property
    def state(self) -> PowerState:
        """Current power state."""
        # Auto-transition cooldown → warm-idle if timer expired
        if self._state == PowerState.COOLDOWN:
            if time.time() - self._cooldown_start >= COOLDOWN_SECONDS:
                self._transition(PowerState.WARM_IDLE, reason="cooldown-expired")
        return self._state

    @property
    def state_duration_s(self) -> float:
        """Seconds in current state."""
        return time.time() - self._state_since

    def transition(self, target: PowerState, reason: str = "") -> dict[str, Any]:
        """Request a state transition. Returns result dict.

        Validates transition is legal. Handles side effects:
        - busy-protected: starts caffeinate
        - cooldown: records cooldown start time
        - parking/asleep: stops caffeinate
        - session-hot: captures ghost checkpoint
        """
        return self._transition(target, reason)

    def _transition(self, target: PowerState, reason: str = "") -> dict[str, Any]:
        old = self._state
        if target == old:
            return {"action": "noop", "state": old.value, "reason": "already in target state"}

        # Validate transition
        allowed = _TRANSITIONS.get(old, set())
        # Always allow transition to ASLEEP (unexpected disconnection)
        if target not in allowed and target != PowerState.ASLEEP:
            return {
                "action": "rejected",
                "from": old.value,
                "to": target.value,
                "reason": f"Invalid transition: {old.value} -> {target.value}",
                "allowed": [s.value for s in allowed],
            }

        # Side effects
        if target == PowerState.BUSY_PROTECTED:
            self._start_caffeinate()
        elif target == PowerState.COOLDOWN:
            self._cooldown_start = time.time()
        elif target in (PowerState.PARKING, PowerState.ASLEEP, PowerState.WARM_IDLE):
            self._stop_caffeinate()
        elif target == PowerState.SESSION_HOT:
            self._capture_checkpoint(reason=f"session-hot: {reason}")

        self._state = target
        self._state_since = time.time()
        self._save_state()

        logger.info(
            "Power state: %s -> %s (reason: %s)",
            old.value, target.value, reason or "manual",
        )

        return {
            "action": "transitioned",
            "from": old.value,
            "to": target.value,
            "reason": reason,
            "caffeinate_active": self._is_caffeinate_running(),
        }

    # ── Caffeinate integration ──

    def _start_caffeinate(self) -> None:
        """Start caffeinate to prevent system + idle sleep."""
        if self._is_caffeinate_running():
            logger.debug("caffeinate already running")
            return

        try:
            proc = subprocess.Popen(
                ["caffeinate", "-s", "-i"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            CAFFEINATE_PID.write_text(str(proc.pid))
            logger.info("caffeinate started (PID %d) — system will not sleep", proc.pid)
        except FileNotFoundError:
            logger.warning("caffeinate not found (not macOS?)")
        except Exception as e:
            logger.error("Failed to start caffeinate: %s", e)

    def _stop_caffeinate(self) -> None:
        """Stop caffeinate, allowing system to sleep again."""
        if not CAFFEINATE_PID.exists():
            return
        try:
            pid = int(CAFFEINATE_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            CAFFEINATE_PID.unlink(missing_ok=True)
            logger.info("caffeinate stopped (PID %d)", pid)
        except ProcessLookupError:
            CAFFEINATE_PID.unlink(missing_ok=True)
            logger.debug("caffeinate process already gone")
        except Exception as e:
            logger.warning("Failed to stop caffeinate: %s", e)

    def _is_caffeinate_running(self) -> bool:
        """Check if our caffeinate process is alive."""
        if not CAFFEINATE_PID.exists():
            return False
        try:
            pid = int(CAFFEINATE_PID.read_text().strip())
            os.kill(pid, 0)  # signal 0 = check existence
            return True
        except (ProcessLookupError, ValueError):
            CAFFEINATE_PID.unlink(missing_ok=True)
            return False

    # ── Ghost checkpoint integration ──

    def _capture_checkpoint(self, reason: str = "") -> None:
        """Capture GhostCheckpoint on significant transitions."""
        try:
            from multifleet.ghost_checkpoint import GhostCheckpoint
            gc = GhostCheckpoint(repo_root=str(self._repo))
            cp = gc.capture(reason=reason or "power-state-transition")
            logger.info("Ghost checkpoint captured: %s", cp.checkpoint_id)
        except Exception as e:
            logger.warning("Ghost checkpoint failed (non-critical): %s", e)

    # ── State persistence ──

    def _save_state(self) -> None:
        try:
            data = {
                "state": self._state.value,
                "since": self._state_since,
                "node_id": self._node_id,
                "caffeinate_active": self._is_caffeinate_running(),
                "cooldown_start": self._cooldown_start,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error("Failed to save power state: %s", e)

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            self._state = PowerState(data.get("state", "warm-idle"))
            self._state_since = data.get("since", time.time())
            self._cooldown_start = data.get("cooldown_start", 0.0)

            # Validate caffeinate state matches reality
            if self._state == PowerState.BUSY_PROTECTED and not self._is_caffeinate_running():
                logger.warning("Was busy-protected but caffeinate died — re-asserting")
                self._start_caffeinate()
        except Exception as e:
            logger.debug("Could not restore power state: %s", e)

    def status(self) -> dict[str, Any]:
        """Full status dict for health endpoints."""
        current = self.state  # triggers auto-transitions
        return {
            "state": current.value,
            "since": self._state_since,
            "duration_s": round(self.state_duration_s, 1),
            "node_id": self._node_id,
            "caffeinate_active": self._is_caffeinate_running(),
            "cooldown_remaining_s": max(
                0,
                COOLDOWN_SECONDS - (time.time() - self._cooldown_start)
            ) if current == PowerState.COOLDOWN else 0,
        }


# ── CLI interface ──

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mgr = PowerStateManager()

    if len(sys.argv) < 2 or sys.argv[1] == "status":
        print(json.dumps(mgr.status(), indent=2))
    elif sys.argv[1] == "set":
        if len(sys.argv) < 3:
            print("Usage: python -m multifleet.power_state set <state> [reason]")
            sys.exit(1)
        target = PowerState(sys.argv[2])
        reason = sys.argv[3] if len(sys.argv) > 3 else ""
        result = mgr.transition(target, reason)
        print(json.dumps(result, indent=2))
    elif sys.argv[1] == "keepawake":
        # Direct caffeinate toggle
        if len(sys.argv) > 2 and sys.argv[2] == "stop":
            mgr._stop_caffeinate()
            print("Caffeinate stopped")
        else:
            mgr._start_caffeinate()
            print("Caffeinate started")
    else:
        print("Usage: python -m multifleet.power_state [status|set <state>|keepawake [stop]]")
