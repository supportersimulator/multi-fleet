"""Probe-to-Surgeon Intensity Mapping — probes determine surgical intensity.

Maps probe scan results to the 3-Surgeon intensity levels:
  light    — all clear, no significant findings
  standard — medium-severity findings, non-blocking
  full     — blocking findings, critical status, high blast radius

This module can be used standalone or wired into the acceptance gate
to enforce probe-driven surgical rigor.
"""

import logging
from typing import Union

logger = logging.getLogger("fleet-nerve.probe-intensity")

# Intensity levels ordered by rigor
INTENSITY_LEVELS = ("light", "standard", "full")
INTENSITY_ORDER = {name: idx for idx, name in enumerate(INTENSITY_LEVELS)}


def determine_intensity(
    probe_results: list,
    high_severity_threshold: int = 1,
    blocking_threshold: int = 1,
) -> str:
    """Map probe results to surgeon intensity level.

    Works with both code_scanner.ProbeResult and probe_engine.ProbeResult,
    as well as plain dicts.

    Args:
        probe_results: List of ProbeResult objects or dicts.
        high_severity_threshold: Number of high-severity findings that trigger "standard".
        blocking_threshold: Number of high-blast-radius findings that trigger "full".

    Returns:
        "light", "standard", or "full"
    """
    high_severity_count = 0
    blocking_count = 0
    has_critical_status = False

    for result in probe_results:
        # Extract status
        status = _get_attr(result, "status", "ok")
        if status in ("critical", "error"):
            has_critical_status = True

        # Extract severity (probe_engine style)
        severity = _get_attr(result, "severity", "")
        if severity in ("high", "critical"):
            high_severity_count += 1

        # Check findings
        findings = _get_attr(result, "findings", [])
        for finding in findings:
            blast = _get_attr(finding, "blast_radius", "low")
            sev = _get_attr(finding, "severity", "low")
            if blast == "high":
                blocking_count += 1
            if sev in ("high", "critical"):
                high_severity_count += 1

    # Determine intensity
    if has_critical_status or blocking_count >= blocking_threshold:
        return "full"
    if high_severity_count >= high_severity_threshold:
        return "standard"
    return "light"


def escalate_intensity(current: str, minimum: str) -> str:
    """Ensure intensity is at least the minimum level.

    Example: escalate_intensity("light", "standard") → "standard"
             escalate_intensity("full", "standard") → "full"
    """
    current_idx = INTENSITY_ORDER.get(current, 0)
    minimum_idx = INTENSITY_ORDER.get(minimum, 0)
    if minimum_idx > current_idx:
        return minimum
    return current


def intensity_for_acceptance(
    probe_results: list,
    epistemic_packet: dict = None,
) -> dict:
    """Compute intensity with full context for acceptance gate wiring.

    Returns a dict suitable for passing to AcceptanceGate.evaluate():
        {
            "surgeon_intensity": "light"|"standard"|"full",
            "probe_summary": {...},
            "requires_resolution": bool,
        }
    """
    intensity = determine_intensity(probe_results)

    # Epistemic packet may override to stricter intensity
    if epistemic_packet:
        packet_intensity = epistemic_packet.get("required_surgeon_intensity", "light")
        intensity = escalate_intensity(intensity, packet_intensity)

    total_findings = sum(
        len(_get_attr(r, "findings", [])) for r in probe_results
    )
    blocking = sum(
        1 for r in probe_results
        for f in _get_attr(r, "findings", [])
        if _get_attr(f, "blast_radius", "low") == "high"
    )

    return {
        "surgeon_intensity": intensity,
        "probe_summary": {
            "total_probes": len(probe_results),
            "total_findings": total_findings,
            "blocking_findings": blocking,
        },
        "requires_resolution": intensity in ("standard", "full"),
    }


def _get_attr(obj: Union[object, dict], key: str, default=None):
    """Get attribute from object or dict transparently."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)
