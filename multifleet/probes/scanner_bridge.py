"""Scanner Bridge — registers code_scanner probes into the probe_engine registry.

Two probe systems exist (code_scanner + probe_engine). Rather than a massive
refactor, this bridge adapts code_scanner probes to the probe_engine interface,
giving us one unified registry with all probes available.

"Integrate, not duplicate." — 3-surgeon neurologist recommendation.
"""

import logging
from typing import Optional

from multifleet.probe_engine import (
    ProbeCategory,
    ProbeFinding as EngineFinding,
    ProbeResult as EngineResult,
    ProbeRegistry,
)

logger = logging.getLogger("fleet-nerve.probes.scanner-bridge")

# Map code_scanner probe IDs to probe_engine categories
_SCANNER_PROBE_MAP = {
    "mirror_drift": ProbeCategory.CONTEXT_QUALITY,
    "hidden_dependency": ProbeCategory.STRUCTURE,
    "stale_context": ProbeCategory.CONTEXT_QUALITY,
    "blast_radius": ProbeCategory.STRUCTURE,
    "injection_prioritizer": ProbeCategory.AGENT_READINESS,
    "security_surface": ProbeCategory.SECURITY,
}


def _adapt_scanner_result(scanner_result) -> EngineResult:
    """Convert a code_scanner ProbeResult to a probe_engine ProbeResult."""
    findings = []
    for f in scanner_result.findings:
        findings.append(EngineFinding(
            title=f.title,
            evidence=f.evidence_refs if hasattr(f, 'evidence_refs') else [],
            blast_radius=getattr(f, 'blast_radius', 'low'),
            suggested_injections=getattr(f, 'suggested_actions', []),
            file_paths=[f.file_path] if hasattr(f, 'file_path') and f.file_path else [],
        ))

    # Map scanner status to engine status
    status_map = {"ok": "ok", "warn": "warning", "error": "error", "skipped": "skipped"}
    status = status_map.get(scanner_result.status, scanner_result.status)

    # Determine severity from findings
    severity = "info"
    if findings:
        blast_levels = [f.blast_radius for f in findings]
        if "high" in blast_levels:
            severity = "high"
        elif "medium" in blast_levels:
            severity = "medium"
        else:
            severity = "low"

    return EngineResult(
        probe_id=f"scanner_{scanner_result.probe_id}",
        category=_SCANNER_PROBE_MAP.get(
            scanner_result.probe_id, ProbeCategory.STRUCTURE
        ).value,
        status=status,
        confidence=scanner_result.confidence,
        severity=severity,
        findings=findings,
        missing_inputs=scanner_result.missing_inputs,
        duration_ms=scanner_result.duration_ms,
    )


def _make_scanner_wrapper(scanner_probe_fn, probe_id: str):
    """Create a probe_engine-compatible wrapper for a code_scanner probe."""
    def wrapper(context: dict) -> EngineResult:
        try:
            from multifleet.code_scanner.probe_contract import ProbeContext
            ctx = ProbeContext(
                repo_path=context.get("repo_root", "."),
                branch=context.get("branch", ""),
                changed_files=context.get("changed_files", []),
                task_description=context.get("task_intent", ""),
                node_id=context.get("node_id", ""),
            )
            scanner_result = scanner_probe_fn(ctx)
            return _adapt_scanner_result(scanner_result)
        except Exception as e:
            return EngineResult.error(
                f"scanner_{probe_id}",
                _SCANNER_PROBE_MAP.get(probe_id, ProbeCategory.STRUCTURE).value,
                str(e),
            )
    wrapper.__name__ = f"scanner_{probe_id}"
    return wrapper


def register_scanner_probes(registry: ProbeRegistry) -> int:
    """Register all code_scanner probes into the probe_engine registry.

    Returns count of probes successfully registered.
    """
    registered = 0

    try:
        from multifleet.code_scanner import create_engine as create_scanner
        scanner = create_scanner()

        for probe_id, entry in scanner._probes.items():
            category = _SCANNER_PROBE_MAP.get(probe_id, ProbeCategory.STRUCTURE)
            wrapper = _make_scanner_wrapper(entry["fn"], probe_id)

            registry.register(
                probe_id=f"scanner_{probe_id}",
                fn=wrapper,
                category=category,
                description=f"[bridged] {entry.get('category', probe_id)}",
                triggers=["manual", "commit"],
            )
            registered += 1
            logger.debug("Bridged scanner probe: %s -> scanner_%s", probe_id, probe_id)

    except ImportError as e:
        logger.debug("code_scanner not available for bridging: %s", e)
    except Exception as e:
        logger.warning("Scanner bridge failed: %s", e)

    if registered:
        logger.info("Scanner bridge registered %d code_scanner probes", registered)
    return registered
