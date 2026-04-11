"""Fleet Health Probe — wraps FleetDoctor diagnostics as a probe.

Translates the 14-check FleetDoctor report into the uniform
ProbeResult contract for the Probe Engine pipeline.
"""

import logging

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.fleet_health")


def _doctor_check_to_finding(check) -> ProbeFinding:
    """Convert a doctor Check to a ProbeFinding."""
    return ProbeFinding(
        title=f"[{check.code}] {check.name}: {check.message}",
        evidence=[f"status={check.status}", f"latency={check.latency_ms:.0f}ms"],
        blast_radius="high" if check.status == "fail" else "low",
        suggested_injections=[f"Fix {check.name}"] if check.status == "fail" else [],
    )


def probe_fleet_health(context: dict) -> ProbeResult:
    """Run FleetDoctor and translate results to ProbeResult."""
    try:
        from multifleet.doctor import FleetDoctor
    except ImportError as e:
        return ProbeResult.error("fleet_health", "reliability", f"Cannot import FleetDoctor: {e}")

    try:
        doctor = FleetDoctor()
        report = doctor.run()
    except Exception as e:
        return ProbeResult.error("fleet_health", "reliability", f"Doctor run failed: {e}")

    # Convert failed/warned checks to findings
    findings: list[ProbeFinding] = []
    for check in report.checks:
        if check.status in ("fail", "warn"):
            findings.append(_doctor_check_to_finding(check))

    # Determine severity from doctor results
    if report.failed > 2:
        severity = "critical"
        status = "critical"
    elif report.failed > 0:
        severity = "high"
        status = "warning"
    elif report.warned > 0:
        severity = "medium"
        status = "warning"
    else:
        severity = "info"
        status = "ok"

    confidence = 0.95 if report.failed == 0 else 0.9

    return ProbeResult(
        probe_id="fleet_health",
        category="reliability",
        status=status,
        confidence=confidence,
        severity=severity,
        findings=findings,
    )
