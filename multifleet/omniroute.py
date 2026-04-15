"""OmniRoute Orchestrator — thin adapter layer for model failover.

Architecture (from chat design):
    Claude Code → claude-code-router → OmniRoute → actual providers

This module provides:
1. Automatic 429/rate-limit detection (not just daily cap)
2. Seamless model switching with GhostCheckpoint state preservation
3. Provider health tracking and intelligent failover
4. Fleet-wide failover coordination via P7 git channel

Does NOT:
- Replace claude-code-router or OmniRoute — sits between them
- Handle Anthropic Remote Control (that stays on Claude Code itself)
- Own invariance/memory (GhostAgent owns that via hooks)

Provider fallback tiers (from chat):
    Tier 1: Anthropic subscription (claude-code-router)
    Tier 2: Anthropic API key
    Tier 3: DeepSeek (cheap, frontier-class)
    Tier 4: OpenRouter (broadest coverage)
    Tier 5: Local MLX (Qwen3-4B, free but weakest)
"""

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("fleet-nerve.omniroute")

# Rate limit detection window
RATE_LIMIT_WINDOW_S = 300  # 5 minutes
MAX_429S_BEFORE_FAILOVER = 3  # 3 rate limits in window → auto-failover
STATE_FILE = Path("/tmp/omniroute-state.json")
HISTORY_FILE = Path("/tmp/omniroute-history.jsonl")


class ProviderTier(IntEnum):
    """Provider priority — lower is better."""
    SUBSCRIPTION = 1   # Claude Code subscription (free with Max plan)
    ANTHROPIC_API = 2  # Anthropic API key (pay-per-use)
    DEEPSEEK = 3       # DeepSeek V3 ($0.28/1M input)
    OPENROUTER = 4     # OpenRouter (any model)
    LOCAL_MLX = 5      # Local Qwen3-4B (free, weakest)


@dataclass
class ProviderHealth:
    """Health state for a single provider."""
    tier: ProviderTier
    name: str
    available: bool = True
    consecutive_failures: int = 0
    rate_limit_hits: list[float] = field(default_factory=list)
    last_success: float = 0.0
    last_failure: float = 0.0
    last_error: str = ""
    total_requests: int = 0
    total_failures: int = 0

    @property
    def rate_limited(self) -> bool:
        """True if too many 429s in the detection window."""
        now = time.time()
        recent = [t for t in self.rate_limit_hits if now - t < RATE_LIMIT_WINDOW_S]
        self.rate_limit_hits = recent  # prune old entries
        return len(recent) >= MAX_429S_BEFORE_FAILOVER

    @property
    def healthy(self) -> bool:
        """True if provider is available and not rate-limited."""
        return self.available and not self.rate_limited and self.consecutive_failures < 5

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_success = time.time()
        self.total_requests += 1
        self.available = True

    def record_failure(self, error: str, is_rate_limit: bool = False) -> None:
        self.consecutive_failures += 1
        self.last_failure = time.time()
        self.last_error = error
        self.total_failures += 1
        self.total_requests += 1
        if is_rate_limit:
            self.rate_limit_hits.append(time.time())

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "name": self.name,
            "available": self.available,
            "healthy": self.healthy,
            "rate_limited": self.rate_limited,
            "consecutive_failures": self.consecutive_failures,
            "last_success": self.last_success,
            "last_failure": self.last_failure,
            "last_error": self.last_error,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
        }


@dataclass
class FailoverEvent:
    """Record of a model failover for observability."""
    timestamp: str
    from_provider: str
    to_provider: str
    reason: str
    node_id: str
    checkpoint_id: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "from": self.from_provider,
            "to": self.to_provider,
            "reason": self.reason,
            "node_id": self.node_id,
            "checkpoint_id": self.checkpoint_id,
        }


class OmniRouteOrchestrator:
    """Thin adapter layer orchestrating model failover across providers.

    Responsibilities:
    - Track provider health across the 5-tier fallback chain
    - Detect 429/rate-limit errors and trigger automatic failover
    - Coordinate with GhostCheckpoint for state preservation on switch
    - Notify fleet of failover events via P7 git channel
    - Integrate with caffeinate/keepawake during failover transitions

    Does NOT directly proxy requests — that's claude-code-router/OmniRoute's job.
    This module makes the DECISION of when to switch and coordinates the transition.
    """

    def __init__(self, repo_root: str = "."):
        self._repo = Path(repo_root).resolve()
        self._node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._providers: dict[ProviderTier, ProviderHealth] = {}
        self._active_tier = ProviderTier.SUBSCRIPTION
        self._failover_history: list[FailoverEvent] = []
        self._init_providers()
        self._load_state()

    def _init_providers(self) -> None:
        """Initialize provider health trackers."""
        self._providers = {
            ProviderTier.SUBSCRIPTION: ProviderHealth(
                tier=ProviderTier.SUBSCRIPTION,
                name="anthropic-subscription",
            ),
            ProviderTier.ANTHROPIC_API: ProviderHealth(
                tier=ProviderTier.ANTHROPIC_API,
                name="anthropic-api",
                available=bool(os.environ.get("ANTHROPIC_API_KEY")),
            ),
            ProviderTier.DEEPSEEK: ProviderHealth(
                tier=ProviderTier.DEEPSEEK,
                name="deepseek",
                available=bool(
                    os.environ.get("Context_DNA_Deepseek")
                    or os.environ.get("DEEPSEEK_API_KEY")
                ),
            ),
            ProviderTier.OPENROUTER: ProviderHealth(
                tier=ProviderTier.OPENROUTER,
                name="openrouter",
                available=bool(os.environ.get("OPENROUTER_API_KEY")),
            ),
            ProviderTier.LOCAL_MLX: ProviderHealth(
                tier=ProviderTier.LOCAL_MLX,
                name="local-mlx",
                available=self._check_local_mlx(),
            ),
        }

    def _check_local_mlx(self) -> bool:
        """Check if local MLX server is running."""
        try:
            req = Request("http://127.0.0.1:5044/v1/models", method="GET")
            resp = urlopen(req, timeout=2)
            return resp.status == 200
        except Exception:
            return False

    # ── Core failover logic ──

    def detect_and_handle_error(
        self,
        error_code: int,
        error_message: str,
        provider_tier: Optional[ProviderTier] = None,
    ) -> dict[str, Any]:
        """Detect error type and trigger failover if needed.

        Args:
            error_code: HTTP status code (429, 500, 503, etc.)
            error_message: Error details
            provider_tier: Which provider hit the error (None = active)

        Returns:
            Dict with keys: action, from_tier, to_tier, checkpoint_id, details
        """
        tier = provider_tier or self._active_tier
        provider = self._providers.get(tier)
        if not provider:
            return {"action": "error", "details": f"Unknown tier: {tier}"}

        is_rate_limit = error_code == 429 or "rate" in error_message.lower()
        is_quota = (
            "quota" in error_message.lower()
            or "limit" in error_message.lower()
            or "exceeded" in error_message.lower()
            or "capacity" in error_message.lower()
        )
        is_overloaded = error_code == 529 or "overloaded" in error_message.lower()
        is_server_error = 500 <= error_code < 600

        provider.record_failure(
            error=f"{error_code}: {error_message[:200]}",
            is_rate_limit=is_rate_limit,
        )

        # Determine if failover is needed
        should_failover = False
        reason = ""

        if is_rate_limit and provider.rate_limited:
            should_failover = True
            reason = f"Rate limited ({len(provider.rate_limit_hits)} hits in {RATE_LIMIT_WINDOW_S}s)"
        elif is_quota:
            should_failover = True
            reason = f"Quota/limit exhausted: {error_message[:100]}"
        elif is_overloaded:
            should_failover = True
            reason = f"Provider overloaded (529)"
        elif is_server_error and provider.consecutive_failures >= 3:
            should_failover = True
            reason = f"Consecutive server errors ({provider.consecutive_failures})"
        elif provider.consecutive_failures >= 5:
            should_failover = True
            reason = f"Too many consecutive failures ({provider.consecutive_failures})"

        if not should_failover:
            # Transient error — retry with same provider
            return {
                "action": "retry",
                "tier": tier.value,
                "provider": provider.name,
                "consecutive_failures": provider.consecutive_failures,
                "details": f"Transient error, retry recommended",
            }

        # Find next healthy provider
        next_tier = self._find_next_healthy(tier)
        if next_tier is None:
            return {
                "action": "exhausted",
                "details": "All providers unhealthy — manual intervention needed",
                "provider_states": {
                    t.name: p.to_dict() for t, p in self._providers.items()
                },
            }

        # Execute failover
        return self._execute_failover(tier, next_tier, reason)

    def _find_next_healthy(self, current: ProviderTier) -> Optional[ProviderTier]:
        """Find next healthy provider after current tier."""
        for tier in sorted(self._providers.keys()):
            if tier > current:
                provider = self._providers[tier]
                # Re-check local MLX availability dynamically
                if tier == ProviderTier.LOCAL_MLX:
                    provider.available = self._check_local_mlx()
                if provider.healthy:
                    return tier
        return None

    def _execute_failover(
        self,
        from_tier: ProviderTier,
        to_tier: ProviderTier,
        reason: str,
    ) -> dict[str, Any]:
        """Execute model failover with checkpoint and fleet notification.

        Steps:
        1. Capture GhostCheckpoint (state preservation)
        2. Assert keepawake (prevent sleep during transition)
        3. Switch active provider
        4. Notify fleet via P7 git channel
        5. Record event history
        """
        from_provider = self._providers[from_tier]
        to_provider = self._providers[to_tier]
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        checkpoint_id = ""

        # 1. Capture checkpoint before switch
        try:
            from multifleet.model_swap import ModelSwapCheckpoint
            swapper = ModelSwapCheckpoint(repo_root=str(self._repo))
            cp = swapper.capture(
                reason=f"failover-{from_provider.name}-to-{to_provider.name}",
                source_model=from_provider.name,
                target_model=to_provider.name,
            )
            checkpoint_id = cp.checkpoint_id
            logger.info("Checkpoint captured before failover: %s", checkpoint_id)
        except Exception as e:
            logger.warning("Checkpoint capture failed (continuing): %s", e)

        # 2. Assert keepawake during transition
        try:
            self._assert_keepawake()
        except Exception as e:
            logger.debug("Keepawake assertion failed (non-critical): %s", e)

        # 3. Switch active provider
        old_tier = self._active_tier
        self._active_tier = to_tier
        logger.warning(
            "MODEL FAILOVER: %s (tier %d) -> %s (tier %d) — reason: %s",
            from_provider.name, from_tier.value,
            to_provider.name, to_tier.value,
            reason,
        )

        # 4. Record event
        event = FailoverEvent(
            timestamp=ts,
            from_provider=from_provider.name,
            to_provider=to_provider.name,
            reason=reason,
            node_id=self._node_id,
            checkpoint_id=checkpoint_id,
        )
        self._failover_history.append(event)
        self._append_history(event)

        # 5. Fleet notification (non-blocking)
        try:
            self._notify_fleet(event)
        except Exception as e:
            logger.warning("Fleet notification failed: %s", e)

        # 6. macOS notification
        try:
            self._macos_notify(
                f"Model failover: {from_provider.name} -> {to_provider.name}",
                reason,
            )
        except Exception:
            pass

        # 7. Save state
        self._save_state()

        return {
            "action": "failover",
            "from_tier": from_tier.value,
            "from_provider": from_provider.name,
            "to_tier": to_tier.value,
            "to_provider": to_provider.name,
            "reason": reason,
            "checkpoint_id": checkpoint_id,
            "env_changes": self._get_env_changes(to_tier),
        }

    def _get_env_changes(self, tier: ProviderTier) -> dict[str, str]:
        """Return environment variable changes needed for the target tier."""
        if tier == ProviderTier.SUBSCRIPTION:
            return {"action": "unset ANTHROPIC_BASE_URL (use subscription directly)"}
        elif tier == ProviderTier.ANTHROPIC_API:
            return {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
        elif tier == ProviderTier.DEEPSEEK:
            return {
                "ANTHROPIC_BASE_URL": "http://localhost:3456",
                "note": "claude-code-router routes to DeepSeek",
            }
        elif tier == ProviderTier.OPENROUTER:
            return {
                "ANTHROPIC_BASE_URL": "http://localhost:20128/v1",
                "note": "OmniRoute gateway with OpenRouter backend",
            }
        elif tier == ProviderTier.LOCAL_MLX:
            return {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:5044/v1",
                "note": "Local Qwen3-4B (limited capability)",
            }
        return {}

    def record_success(self, tier: Optional[ProviderTier] = None) -> None:
        """Record a successful request to reset failure counters."""
        t = tier or self._active_tier
        provider = self._providers.get(t)
        if provider:
            provider.record_success()

    # ── Provider recovery ──

    def attempt_recovery(self, tier: ProviderTier) -> bool:
        """Try to recover a previously failed provider.

        Returns True if provider appears healthy again.
        """
        provider = self._providers.get(tier)
        if not provider:
            return False

        healthy = False
        if tier == ProviderTier.SUBSCRIPTION:
            # Check if subscription is usable (try a minimal request)
            healthy = self._probe_anthropic_subscription()
        elif tier == ProviderTier.ANTHROPIC_API:
            healthy = bool(os.environ.get("ANTHROPIC_API_KEY"))
        elif tier == ProviderTier.DEEPSEEK:
            healthy = bool(
                os.environ.get("Context_DNA_Deepseek")
                or os.environ.get("DEEPSEEK_API_KEY")
            )
        elif tier == ProviderTier.LOCAL_MLX:
            healthy = self._check_local_mlx()

        if healthy:
            provider.consecutive_failures = 0
            provider.available = True
            provider.rate_limit_hits.clear()
            logger.info("Provider recovered: %s", provider.name)

            # If recovered provider is better than current, suggest upgrade
            if tier < self._active_tier:
                logger.info(
                    "Better provider available: %s (tier %d) vs current %s (tier %d)",
                    provider.name, tier.value,
                    self._providers[self._active_tier].name, self._active_tier.value,
                )
        return healthy

    def _probe_anthropic_subscription(self) -> bool:
        """Quick check if Anthropic subscription is responding."""
        try:
            # Check if claude-code-router is running and healthy
            req = Request("http://127.0.0.1:3456/health", method="GET")
            resp = urlopen(req, timeout=3)
            return resp.status == 200
        except Exception:
            return True  # Assume subscription works if router isn't running

    # ── Keepawake integration ──

    def _assert_keepawake(self) -> None:
        """Assert caffeinate keepawake during model transitions."""
        keepalive_script = self._repo / "scripts" / "fleet-keepalive.sh"
        if keepalive_script.exists():
            subprocess.run(
                ["bash", str(keepalive_script), "start"],
                capture_output=True, timeout=10,
            )
            logger.info("Keepawake asserted for model transition")

    # ── Fleet coordination ──

    def _notify_fleet(self, event: FailoverEvent) -> None:
        """Write failover event to .fleet-messages/all/ (P7 git channel)."""
        msg_dir = self._repo / ".fleet-messages" / "all"
        msg_dir.mkdir(parents=True, exist_ok=True)

        filename_ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        msg_path = msg_dir / f"{filename_ts}-failover-{self._node_id}.md"

        content = (
            f"---\n"
            f"from: {self._node_id}\n"
            f"to: all\n"
            f"subject: Model failover — {event.from_provider} to {event.to_provider}\n"
            f"timestamp: {event.timestamp}\n"
            f"priority: high\n"
            f"type: failover-event\n"
            f"---\n\n"
            f"## Model Failover on {self._node_id}\n\n"
            f"- **From:** {event.from_provider} (tier {self._providers.get(ProviderTier(int(event.from_provider.split('-')[0]) if event.from_provider[0].isdigit() else 1), ProviderHealth(ProviderTier.SUBSCRIPTION, '')).tier.value})\n"
            f"- **To:** {event.to_provider}\n"
            f"- **Reason:** {event.reason}\n"
            f"- **Checkpoint:** {event.checkpoint_id or 'none'}\n\n"
            f"All fleet nodes should be aware of degraded capability on this node.\n"
        )
        msg_path.write_text(content)
        logger.info("Fleet failover notification written: %s", msg_path.name)

    def _macos_notify(self, title: str, message: str) -> None:
        """macOS notification (non-blocking, best-effort)."""
        subprocess.Popen(
            [
                "osascript", "-e",
                f'display notification "{message}" '
                f'with title "OmniRoute" subtitle "{title}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # ── State persistence ──

    def _save_state(self) -> None:
        """Persist current state to disk."""
        try:
            state = {
                "active_tier": self._active_tier.value,
                "node_id": self._node_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "providers": {
                    t.name: p.to_dict() for t, p in self._providers.items()
                },
            }
            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            logger.error("Failed to save state: %s", e)

    def _load_state(self) -> None:
        """Restore state from disk if available."""
        if not STATE_FILE.exists():
            return
        try:
            state = json.loads(STATE_FILE.read_text())
            saved_tier = state.get("active_tier", 1)
            self._active_tier = ProviderTier(saved_tier)
            logger.info("Restored OmniRoute state: active tier %d", saved_tier)
        except Exception as e:
            logger.debug("Could not restore state: %s", e)

    def _append_history(self, event: FailoverEvent) -> None:
        """Append failover event to history log."""
        try:
            with open(HISTORY_FILE, "a") as f:
                f.write(json.dumps(event.to_dict()) + "\n")
            # Prune to last 100 entries
            if HISTORY_FILE.exists():
                lines = HISTORY_FILE.read_text().splitlines()
                if len(lines) > 100:
                    HISTORY_FILE.write_text("\n".join(lines[-100:]) + "\n")
        except Exception:
            pass

    # ── Status / diagnostics ──

    def status(self) -> dict[str, Any]:
        """Full orchestrator status for health endpoints."""
        # Re-check dynamic providers
        self._providers[ProviderTier.LOCAL_MLX].available = self._check_local_mlx()

        return {
            "active_tier": self._active_tier.value,
            "active_provider": self._providers[self._active_tier].name,
            "node_id": self._node_id,
            "providers": {
                t.name: p.to_dict() for t, p in sorted(self._providers.items())
            },
            "failover_count": len(self._failover_history),
            "last_failover": (
                self._failover_history[-1].to_dict()
                if self._failover_history else None
            ),
        }

    def get_active_provider(self) -> str:
        """Return name of currently active provider."""
        return self._providers[self._active_tier].name

    def get_active_tier(self) -> ProviderTier:
        """Return currently active tier."""
        return self._active_tier

    def reset_provider(self, tier: ProviderTier) -> bool:
        """Manually reset a provider's health state."""
        provider = self._providers.get(tier)
        if not provider:
            return False
        provider.consecutive_failures = 0
        provider.rate_limit_hits.clear()
        provider.available = True
        provider.last_error = ""
        self._save_state()
        return True


# ── CLI interface ──

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    orch = OmniRouteOrchestrator()

    if len(sys.argv) < 2 or sys.argv[1] == "status":
        s = orch.status()
        print(json.dumps(s, indent=2))
    elif sys.argv[1] == "test-failover":
        result = orch.detect_and_handle_error(429, "Rate limit exceeded")
        print(json.dumps(result, indent=2))
    elif sys.argv[1] == "recover":
        tier = ProviderTier(int(sys.argv[2])) if len(sys.argv) > 2 else ProviderTier.SUBSCRIPTION
        ok = orch.attempt_recovery(tier)
        print(f"Recovery {'succeeded' if ok else 'failed'} for tier {tier.value}")
    else:
        print("Usage: python -m multifleet.omniroute [status|test-failover|recover [tier]]")
