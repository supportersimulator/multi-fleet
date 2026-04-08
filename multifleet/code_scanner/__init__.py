"""ContextDNA Probe Engine -- continuous code reality scanning.

Inspired by GhostScan's architecture: many narrow probes + central registry
+ normalized output. Feeds evidence ledger, EventBus, 3-surgeons gates.

Cost tiers control when probes run:
  cheap    -- file save, sub-second (path checks, regex)
  medium   -- commit time, up to 5s (import analysis, dependency scan)
  expensive -- idle/session end, unbounded (full repo scan, git log analysis)
"""

import importlib
import logging
import pkgutil
import time
from typing import Callable, Optional

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner")

# Cost tier ordering for filtering
_COST_ORDER = {"cheap": 0, "medium": 1, "expensive": 2}


class ProbeEngine:
    """Central probe registry and executor. GhostScan pattern."""

    def __init__(self):
        self._probes: dict[str, dict] = {}
        self._stats = {
            "total_runs": 0,
            "total_findings": 0,
            "total_errors": 0,
            "last_scan_at": 0.0,
        }

    def register(self, probe_id: str, probe_fn: Callable[[ProbeContext], ProbeResult],
                 category: str, cost: str = "cheap") -> None:
        """Register a probe function.

        Args:
            probe_id: Unique identifier.
            probe_fn: Callable(ProbeContext) -> ProbeResult.
            category: Grouping label (structure, context_quality, security, etc.).
            cost: "cheap", "medium", or "expensive" -- controls when probe runs.
        """
        if cost not in _COST_ORDER:
            raise ValueError(f"Invalid cost '{cost}', must be one of: {list(_COST_ORDER)}")
        if probe_id in self._probes:
            logger.warning("probe_overwrite id=%s", probe_id)
        self._probes[probe_id] = {
            "fn": probe_fn,
            "category": category,
            "cost": cost,
        }
        logger.debug("probe_registered id=%s category=%s cost=%s", probe_id, category, cost)

    def discover_probes(self) -> int:
        """Auto-discover probes from the probes/ subpackage.

        Each module must define a `register(engine)` function.
        Returns count of newly discovered modules.
        """
        from multifleet.code_scanner import probes as probes_pkg
        count = 0
        for importer, modname, ispkg in pkgutil.iter_modules(probes_pkg.__path__):
            if modname.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"multifleet.code_scanner.probes.{modname}")
                if hasattr(mod, "register"):
                    mod.register(self)
                    count += 1
                    logger.debug("probe_discovered module=%s", modname)
                else:
                    logger.warning("probe_module_no_register module=%s", modname)
            except Exception as e:
                logger.error("probe_discover_error module=%s error=%s", modname, e)
        return count

    def _run_one_internal(self, probe_id: str, entry: dict,
                          context: ProbeContext) -> ProbeResult:
        """Execute a single probe with timing and error handling."""
        t0 = time.monotonic()
        try:
            result = entry["fn"](context)
            if not isinstance(result, ProbeResult):
                result = ProbeResult.error(
                    probe_id, f"Probe returned {type(result).__name__}, expected ProbeResult"
                )
            result.duration_ms = (time.monotonic() - t0) * 1000
            self._stats["total_runs"] += 1
            self._stats["total_findings"] += len(result.findings)
            if result.status == "error":
                self._stats["total_errors"] += 1
            return result
        except Exception as e:
            logger.error("probe_error id=%s error=%s", probe_id, e)
            self._stats["total_runs"] += 1
            self._stats["total_errors"] += 1
            result = ProbeResult.error(probe_id, str(e))
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

    def run_all(self, context: ProbeContext,
                cost_limit: str = "expensive") -> list[ProbeResult]:
        """Run all probes up to cost limit."""
        max_cost = _COST_ORDER.get(cost_limit, 2)
        results = []
        for probe_id, entry in self._probes.items():
            if _COST_ORDER[entry["cost"]] <= max_cost:
                results.append(self._run_one_internal(probe_id, entry, context))
        self._stats["last_scan_at"] = time.time()
        logger.info(
            "scan_complete cost_limit=%s probes=%d findings=%d",
            cost_limit, len(results),
            sum(len(r.findings) for r in results),
        )
        return results

    def run_one(self, probe_id: str, context: ProbeContext) -> ProbeResult:
        """Run a single probe by ID."""
        entry = self._probes.get(probe_id)
        if not entry:
            return ProbeResult.error(probe_id, f"Probe '{probe_id}' not registered")
        result = self._run_one_internal(probe_id, entry, context)
        self._stats["last_scan_at"] = time.time()
        return result

    def run_cheap(self, context: ProbeContext) -> list[ProbeResult]:
        """Only cheap probes (sub-second, safe for file-save triggers)."""
        return self.run_all(context, cost_limit="cheap")

    def run_medium(self, context: ProbeContext) -> list[ProbeResult]:
        """Cheap + medium probes (commit-time budget)."""
        return self.run_all(context, cost_limit="medium")

    def list_probes(self) -> list[dict]:
        """Return metadata for all registered probes."""
        return [
            {"probe_id": pid, "category": e["category"], "cost": e["cost"]}
            for pid, e in self._probes.items()
        ]

    @property
    def stats(self) -> dict:
        return dict(self._stats)


def create_engine(auto_discover: bool = True) -> ProbeEngine:
    """Factory: create engine with all built-in probes registered."""
    engine = ProbeEngine()
    if auto_discover:
        engine.discover_probes()
    return engine
