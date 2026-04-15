"""Coordinator Surgeon Proxy — manages local 3-Surgeons sessions.

Proxies cross-examination requests, tracks surgical outcomes,
manages surgeon availability, and escalates when surgeons are unavailable.

Composes with the surgery_bridge (memory/surgery_bridge.py) for actual
LLM calls and with the evidence_ledger for recording outcomes.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet-nerve.coordinator-surgeon")


@dataclass
class SurgeonAvailability:
    """Availability state for a single surgeon."""
    name: str  # atlas, cardiologist, neurologist
    available: bool = False
    last_probe: float = 0.0
    last_success: float = 0.0
    consecutive_failures: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "last_probe": self.last_probe,
            "last_success": self.last_success,
            "consecutive_failures": self.consecutive_failures,
            "error": self.error,
        }


@dataclass
class SurgicalOutcome:
    """Result of a surgical operation (cross-exam, cardio-review, etc.)."""
    operation: str
    topic: str
    surgeons_involved: list
    result: dict
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "topic": self.topic,
            "surgeons_involved": self.surgeons_involved,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
            "error": self.error,
            "result_keys": list(self.result.keys()) if self.result else [],
        }


class SurgeonProxy:
    """Manages local 3-Surgeons availability and proxies requests.

    Responsibilities:
    1. Probe surgeon availability (API keys, endpoints)
    2. Proxy cross-examination and cardio-review requests
    3. Track surgical outcomes
    4. Escalate when surgeons unavailable
    """

    SURGEON_NAMES = ("atlas", "cardiologist", "neurologist")

    def __init__(self, node_id: str, probe_interval_s: float = 60.0):
        self.node_id = node_id
        self.probe_interval_s = probe_interval_s
        self._availability: dict[str, SurgeonAvailability] = {
            name: SurgeonAvailability(name=name) for name in self.SURGEON_NAMES
        }
        self._outcomes: list[SurgicalOutcome] = []
        self._max_outcomes = 100
        self._probe_task: Optional[asyncio.Task] = None
        self._bridge = None
        self._stats = {
            "operations_run": 0,
            "operations_success": 0,
            "operations_failed": 0,
            "probes_run": 0,
        }

    def _get_bridge(self):
        """Lazy-load surgery bridge."""
        if self._bridge is not None:
            return self._bridge
        try:
            from memory.surgery_bridge import (
                bridge_status,
                cardio_review,
                consensus,
                neurologist_challenge,
            )
            self._bridge = {
                "cardio_review": cardio_review,
                "neurologist_challenge": neurologist_challenge,
                "consensus": consensus,
                "bridge_status": bridge_status,
            }
            return self._bridge
        except ImportError:
            logger.debug("Surgery bridge not available (memory package not in path)")
            return None

    async def probe_availability(self) -> dict[str, SurgeonAvailability]:
        """Probe all surgeon availability."""
        self._stats["probes_run"] += 1
        now = time.time()

        # Atlas (this node's Claude session) is always considered available
        atlas = self._availability["atlas"]
        atlas.available = True
        atlas.last_probe = now
        atlas.last_success = now

        # Cardiologist: check API key
        import os
        has_cardio_key = bool(
            os.environ.get("Context_DNA_OPENAI")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("OPENAI_API_KEY")
        )
        cardio = self._availability["cardiologist"]
        cardio.last_probe = now
        if has_cardio_key:
            cardio.available = True
            cardio.last_success = now
            cardio.consecutive_failures = 0
            cardio.error = ""
        else:
            cardio.available = False
            cardio.consecutive_failures += 1
            cardio.error = "No API key"

        # Neurologist: probe Ollama or MLX
        neuro = self._availability["neurologist"]
        neuro.last_probe = now
        neuro_ok = await self._probe_neurologist()
        if neuro_ok:
            neuro.available = True
            neuro.last_success = now
            neuro.consecutive_failures = 0
            neuro.error = ""
        else:
            neuro.available = False
            neuro.consecutive_failures += 1
            neuro.error = "Ollama/MLX unreachable"

        return dict(self._availability)

    async def start(self):
        """Start periodic availability probing."""
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())
            logger.info("Surgeon proxy started (probe interval=%.0fs)", self.probe_interval_s)

    async def stop(self):
        """Stop the probe loop."""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass
            logger.info("Surgeon proxy stopped")

    async def cardio_review(self, topic: str, **kwargs) -> SurgicalOutcome:
        """Proxy a cardio-review request to the surgery bridge."""
        return await self._run_operation("cardio_review", topic, ["cardiologist"], **kwargs)

    async def neurologist_challenge(self, topic: str, **kwargs) -> SurgicalOutcome:
        """Proxy a neurologist-challenge request."""
        return await self._run_operation("neurologist_challenge", topic, ["neurologist"], **kwargs)

    async def consensus(self, claim: str, **kwargs) -> SurgicalOutcome:
        """Proxy a consensus request (all surgeons)."""
        return await self._run_operation(
            "consensus", claim, list(self.SURGEON_NAMES), **kwargs,
        )

    def get_availability(self) -> dict:
        """Return current surgeon availability."""
        return {k: v.to_dict() for k, v in self._availability.items()}

    def get_available_count(self) -> int:
        """Return number of available surgeons."""
        return sum(1 for v in self._availability.values() if v.available)

    def recent_outcomes(self, limit: int = 10) -> list[dict]:
        """Return recent surgical outcomes."""
        return [o.to_dict() for o in self._outcomes[-limit:]]

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── Internal ──

    async def _probe_loop(self):
        """Background loop probing surgeon availability."""
        while True:
            try:
                await self.probe_availability()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Surgeon probe error: %s", e)
            await asyncio.sleep(self.probe_interval_s)

    async def _probe_neurologist(self) -> bool:
        """Check if Ollama or MLX is reachable."""
        import urllib.request
        loop = asyncio.get_event_loop()
        for url in ("http://localhost:11434/api/tags", "http://127.0.0.1:5044/v1/models"):
            try:
                def _probe(u=url):
                    req = urllib.request.urlopen(u, timeout=2)
                    return req.status == 200
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, _probe),
                    timeout=3,
                )
                if ok:
                    return True
            except Exception:
                continue
        return False

    async def _run_operation(
        self, operation: str, topic: str, surgeons: list, **kwargs,
    ) -> SurgicalOutcome:
        """Run a surgical operation via the bridge."""
        self._stats["operations_run"] += 1
        t0 = time.monotonic()

        bridge = self._get_bridge()
        if bridge is None:
            outcome = SurgicalOutcome(
                operation=operation,
                topic=topic,
                surgeons_involved=surgeons,
                result={},
                success=False,
                error="Surgery bridge unavailable",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
            self._stats["operations_failed"] += 1
            self._record_outcome(outcome)
            return outcome

        fn = bridge.get(operation)
        if fn is None:
            outcome = SurgicalOutcome(
                operation=operation,
                topic=topic,
                surgeons_involved=surgeons,
                result={},
                success=False,
                error=f"Unknown operation: {operation}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
            self._stats["operations_failed"] += 1
            self._record_outcome(outcome)
            return outcome

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: fn(topic, **kwargs))
            duration_ms = (time.monotonic() - t0) * 1000
            success = isinstance(result, dict) and result.get("status") == "ok"
            outcome = SurgicalOutcome(
                operation=operation,
                topic=topic,
                surgeons_involved=surgeons,
                result=result if isinstance(result, dict) else {"raw": str(result)},
                success=success,
                duration_ms=duration_ms,
                error="" if success else str(result.get("output", "")) if isinstance(result, dict) else "",
            )
            if success:
                self._stats["operations_success"] += 1
            else:
                self._stats["operations_failed"] += 1
        except Exception as e:
            duration_ms = (time.monotonic() - t0) * 1000
            outcome = SurgicalOutcome(
                operation=operation,
                topic=topic,
                surgeons_involved=surgeons,
                result={},
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )
            self._stats["operations_failed"] += 1
            logger.error("Surgical operation %s failed: %s", operation, e)

        self._record_outcome(outcome)
        return outcome

    def _record_outcome(self, outcome: SurgicalOutcome):
        """Record an outcome, evicting old entries if over limit."""
        self._outcomes.append(outcome)
        if len(self._outcomes) > self._max_outcomes:
            self._outcomes = self._outcomes[-self._max_outcomes:]
