"""Coordinator Health Check Subsystem — monitors local service health.

Probes local services (Redis, LLM, webhook, fleet daemon, NATS, surgeons)
on a configurable interval. Reports health state to the coordinator agent
for fleet-wide heartbeats and self-healing decisions.

Every check degrades gracefully: a failed probe records the failure but
never crashes the health loop. Zero silent failures — every exception
increments a counter and logs.
"""

import asyncio
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("fleet-nerve.coordinator-health")


@dataclass
class ServiceHealth:
    """Health state for a single local service."""
    name: str
    healthy: bool
    latency_ms: int = 0
    last_check: float = 0.0
    last_healthy: float = 0.0
    consecutive_failures: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "latency_ms": self.latency_ms,
            "last_check": self.last_check,
            "last_healthy": self.last_healthy,
            "consecutive_failures": self.consecutive_failures,
            "error": self.error,
        }


@dataclass
class LocalHealthSnapshot:
    """Aggregated health snapshot of all local services."""
    node_id: str
    timestamp: float
    services: dict = field(default_factory=dict)  # name -> ServiceHealth
    overall_healthy: bool = True
    degraded_services: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "timestamp": self.timestamp,
            "overall_healthy": self.overall_healthy,
            "degraded_services": self.degraded_services,
            "services": {k: v.to_dict() for k, v in self.services.items()},
        }


class HealthChecker:
    """Probes local services and produces health snapshots.

    Services checked:
    - redis: TCP connect to 127.0.0.1:6379
    - llm: HTTP GET to MLX server (port 5044)
    - webhook: HTTP GET to webhook endpoint (port 8888)
    - fleet_daemon: HTTP GET to fleet daemon /health (port 8855)
    - nats: TCP connect to NATS (port 4222)
    - surgeon_cardiologist: API key present
    - surgeon_neurologist: Ollama or MLX reachable
    """

    DEFAULT_SERVICES = (
        "redis", "llm", "webhook", "fleet_daemon",
        "nats", "surgeon_cardiologist", "surgeon_neurologist",
    )

    def __init__(
        self,
        node_id: str,
        check_interval_s: float = 30.0,
        services: tuple = None,
        redis_port: int = 6379,
        llm_port: int = 5044,
        webhook_port: int = 8888,
        daemon_port: int = 8855,
        nats_port: int = 4222,
    ):
        self.node_id = node_id
        self.check_interval_s = check_interval_s
        self._services_to_check = services or self.DEFAULT_SERVICES
        self._redis_port = redis_port
        self._llm_port = llm_port
        self._webhook_port = webhook_port
        self._daemon_port = daemon_port
        self._nats_port = nats_port

        # State: service_name -> ServiceHealth
        self._state: dict[str, ServiceHealth] = {}
        self._check_task: Optional[asyncio.Task] = None
        self._stats = {"checks_run": 0, "errors": 0}

    async def check_all(self) -> LocalHealthSnapshot:
        """Run all health checks and return a snapshot."""
        self._stats["checks_run"] += 1
        now = time.time()

        for svc in self._services_to_check:
            checker = getattr(self, f"_check_{svc}", None)
            if checker is None:
                continue
            prev = self._state.get(svc, ServiceHealth(name=svc, healthy=False))
            try:
                result = await checker()
                result.last_check = now
                if result.healthy:
                    result.last_healthy = now
                    result.consecutive_failures = 0
                else:
                    result.last_healthy = prev.last_healthy
                    result.consecutive_failures = prev.consecutive_failures + 1
            except Exception as e:
                self._stats["errors"] += 1
                logger.warning("Health check %s raised: %s", svc, e)
                result = ServiceHealth(
                    name=svc,
                    healthy=False,
                    last_check=now,
                    last_healthy=prev.last_healthy,
                    consecutive_failures=prev.consecutive_failures + 1,
                    error=str(e),
                )
            self._state[svc] = result

        degraded = [
            s.name for s in self._state.values()
            if not s.healthy
        ]
        overall = len(degraded) == 0

        return LocalHealthSnapshot(
            node_id=self.node_id,
            timestamp=now,
            services=dict(self._state),
            overall_healthy=overall,
            degraded_services=degraded,
        )

    async def start(self):
        """Start periodic health check loop."""
        if self._check_task is None or self._check_task.done():
            self._check_task = asyncio.create_task(self._check_loop())
            logger.info("Health checker started (interval=%.0fs)", self.check_interval_s)

    async def stop(self):
        """Stop the health check loop."""
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
            logger.info("Health checker stopped")

    @property
    def latest(self) -> Optional[LocalHealthSnapshot]:
        """Return last snapshot without running checks."""
        if not self._state:
            return None
        degraded = [s.name for s in self._state.values() if not s.healthy]
        ts = max((s.last_check for s in self._state.values()), default=0.0)
        return LocalHealthSnapshot(
            node_id=self.node_id,
            timestamp=ts,
            services=dict(self._state),
            overall_healthy=len(degraded) == 0,
            degraded_services=degraded,
        )

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── Internal check loop ──

    async def _check_loop(self):
        """Background loop running health checks."""
        while True:
            try:
                snapshot = await self.check_all()
                if not snapshot.overall_healthy:
                    logger.warning(
                        "Health degraded: %s",
                        ", ".join(snapshot.degraded_services),
                    )
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._stats["errors"] += 1
                logger.error("Health check loop error: %s", e)
            await asyncio.sleep(self.check_interval_s)

    # ── Individual service checks ──

    async def _check_redis(self) -> ServiceHealth:
        """TCP connect to Redis."""
        return await self._tcp_probe("redis", "127.0.0.1", self._redis_port)

    async def _check_llm(self) -> ServiceHealth:
        """HTTP GET to MLX LLM server."""
        return await self._http_probe("llm", f"http://127.0.0.1:{self._llm_port}/v1/models")

    async def _check_webhook(self) -> ServiceHealth:
        """HTTP GET to webhook/markdown server."""
        return await self._http_probe("webhook", f"http://127.0.0.1:{self._webhook_port}/health")

    async def _check_fleet_daemon(self) -> ServiceHealth:
        """HTTP GET to fleet daemon health."""
        return await self._http_probe("fleet_daemon", f"http://127.0.0.1:{self._daemon_port}/health")

    async def _check_nats(self) -> ServiceHealth:
        """TCP connect to NATS."""
        return await self._tcp_probe("nats", "127.0.0.1", self._nats_port)

    async def _check_surgeon_cardiologist(self) -> ServiceHealth:
        """Check if cardiologist API key is available."""
        t0 = time.time()
        has_key = bool(
            os.environ.get("Context_DNA_OPENAI")
            or os.environ.get("Context_DNA_Deepseek")
            or os.environ.get("OPENAI_API_KEY")
        )
        elapsed = int((time.time() - t0) * 1000)
        return ServiceHealth(
            name="surgeon_cardiologist",
            healthy=has_key,
            latency_ms=elapsed,
            error="" if has_key else "No API key found",
        )

    async def _check_surgeon_neurologist(self) -> ServiceHealth:
        """Check Ollama or MLX for neurologist."""
        # Try Ollama first
        result = await self._http_probe(
            "surgeon_neurologist", "http://localhost:11434/api/tags", timeout=2,
        )
        if result.healthy:
            return result
        # Fallback to MLX
        return await self._http_probe(
            "surgeon_neurologist", f"http://127.0.0.1:{self._llm_port}/v1/models", timeout=2,
        )

    # ── Probe helpers ──

    async def _tcp_probe(self, name: str, host: str, port: int, timeout: float = 3.0) -> ServiceHealth:
        """Probe a TCP port."""
        t0 = time.time()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            elapsed = int((time.time() - t0) * 1000)
            return ServiceHealth(name=name, healthy=True, latency_ms=elapsed)
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            return ServiceHealth(
                name=name, healthy=False, latency_ms=elapsed,
                error=f"{host}:{port} unreachable: {e}",
            )

    async def _http_probe(self, name: str, url: str, timeout: float = 3.0) -> ServiceHealth:
        """Probe an HTTP endpoint (runs in executor to avoid blocking)."""
        loop = asyncio.get_event_loop()
        t0 = time.time()
        try:
            def _do_request():
                req = urllib.request.Request(url, method="GET")
                resp = urllib.request.urlopen(req, timeout=timeout)
                return resp.status

            status = await asyncio.wait_for(
                loop.run_in_executor(None, _do_request),
                timeout=timeout + 1,
            )
            elapsed = int((time.time() - t0) * 1000)
            healthy = 200 <= status < 300
            return ServiceHealth(
                name=name, healthy=healthy, latency_ms=elapsed,
                error="" if healthy else f"HTTP {status}",
            )
        except Exception as e:
            elapsed = int((time.time() - t0) * 1000)
            return ServiceHealth(
                name=name, healthy=False, latency_ms=elapsed,
                error=str(e),
            )
