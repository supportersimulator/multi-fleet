"""Probe Interpreter — uses local LLM to generate human-readable insights from probe findings.

Takes raw ProbeResult data and produces natural language summaries,
risk assessments, and action recommendations.

Uses the LLM priority queue at P4 BACKGROUND priority (non-blocking).
Falls back to template-based interpretation when LLM is unavailable.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional

from multifleet.probe_engine import ProbeResult

logger = logging.getLogger("fleet-nerve.probe-interpreter")

# Severity ordering for display
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class ProbeInterpreter:
    """Interprets probe results into human-readable insights.

    Tries LLM-powered interpretation first (richer), falls back to
    template-based when LLM is unavailable. Both produce the same
    output contract.
    """

    def __init__(self, llm_timeout_s: float = 15.0):
        self._llm_timeout_s = llm_timeout_s

    def interpret(self, results: list[ProbeResult]) -> dict:
        """Generate human-readable interpretation of probe results.

        Returns:
            dict with keys: summary, risk_level, flagged (list of dicts),
            recommendations (list of str), source ("llm" or "template"),
            generated_at (ISO timestamp).
        """
        if not results:
            return self._empty_result()

        # Try LLM first, fall back to template
        try:
            return self._llm_interpret(results)
        except Exception as e:
            logger.debug("LLM interpret unavailable (%s), using template", e)
            return self._template_interpret(results)

    def _empty_result(self) -> dict:
        return {
            "summary": "No probe results to interpret.",
            "risk_level": "none",
            "flagged": [],
            "recommendations": [],
            "source": "template",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _template_interpret(self, results: list[ProbeResult]) -> dict:
        """Template-based interpretation when LLM unavailable."""
        flagged = []
        for r in results:
            if r.status in ("error", "skipped") or not r.findings:
                continue
            for f in r.findings:
                flagged.append({
                    "probe_id": r.probe_id,
                    "title": f.title,
                    "severity": r.severity,
                    "blast_radius": f.blast_radius,
                    "category": r.category,
                    "evidence_count": len(f.evidence),
                })

        # Sort by severity
        flagged.sort(key=lambda x: _SEVERITY_ORDER.get(x["severity"], 5))

        # Build summary
        risk_level = self._compute_risk_level(results)
        total_findings = sum(len(r.findings) for r in results if r.status not in ("error", "skipped"))
        active_count = sum(1 for r in results if r.status not in ("error", "skipped"))

        if not flagged:
            summary = f"{active_count} probes ran, no issues flagged."
        else:
            top_items = ", ".join(
                f"{f['title'][:40]} ({f['severity'].upper()})"
                for f in flagged[:3]
            )
            summary = f"{len(flagged)} finding(s) from {active_count} probes: {top_items}"

        recommendations = self._template_recommendations(flagged)

        return {
            "summary": summary,
            "risk_level": risk_level,
            "flagged": flagged,
            "recommendations": recommendations,
            "source": "template",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _llm_interpret(self, results: list[ProbeResult]) -> dict:
        """LLM-powered interpretation for rich insights."""
        # Import here to avoid hard dependency on memory module
        try:
            from memory.llm_priority_queue import Priority, llm_generate
        except ImportError:
            return self._template_interpret(results)

        # Build compact prompt from results
        probe_lines = []
        for r in results:
            if r.status in ("error", "skipped"):
                continue
            for f in r.findings:
                probe_lines.append(
                    f"- [{r.severity.upper()}] {r.probe_id}/{r.category}: "
                    f"{f.title} (blast_radius={f.blast_radius}, "
                    f"evidence={len(f.evidence)})"
                )
        if not probe_lines:
            return self._template_interpret(results)

        findings_text = "\n".join(probe_lines[:15])  # Cap to avoid token overflow

        system_prompt = (
            "You are a fleet diagnostics interpreter. Given probe findings, "
            "produce a concise JSON object with:\n"
            '  "summary": one-sentence human-readable overview,\n'
            '  "risk_assessment": paragraph on risk and urgency,\n'
            '  "recommendations": array of 1-3 action items (strings)\n'
            "Output ONLY valid JSON. No markdown fences."
        )
        user_prompt = f"Probe findings:\n{findings_text}"

        raw = llm_generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            priority=Priority.BACKGROUND,
            profile="extract",
            caller="probe_interpreter",
            timeout_s=self._llm_timeout_s,
        )

        if not raw:
            raise RuntimeError("LLM returned empty response")

        # Parse LLM JSON response
        import json
        try:
            # Strip markdown fences if present
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # LLM produced non-JSON — use as plain text summary
            parsed = {"summary": raw[:300], "recommendations": []}

        # Merge template data for structured fields
        template = self._template_interpret(results)
        return {
            "summary": parsed.get("summary", template["summary"]),
            "risk_level": template["risk_level"],
            "risk_assessment": parsed.get("risk_assessment", ""),
            "flagged": template["flagged"],
            "recommendations": parsed.get("recommendations", template["recommendations"]),
            "source": "llm",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def generate_injection_context(self, results: list[ProbeResult]) -> str:
        """Generate context snippet for webhook injection.

        Returns a concise markdown-style summary suitable for systemMessage.
        Uses template path only (no LLM) — must be fast and deterministic.
        """
        if not results:
            return ""

        active = [r for r in results if r.status not in ("error", "skipped") and r.findings]
        if not active:
            return ""

        # Sort by severity
        active.sort(key=lambda r: _SEVERITY_ORDER.get(r.severity, 5))

        lines = ["[PROBE INSIGHTS]"]
        risk = self._compute_risk_level(results)
        lines.append(f"Risk: {risk.upper()}")

        for r in active[:5]:
            for f in r.findings[:2]:
                marker = "!" if r.severity in ("critical", "high") else "-"
                lines.append(f"  {marker} {f.title} [{r.severity}]")
                if f.suggested_injections:
                    lines.append(f"    → {f.suggested_injections[0][:80]}")

        recommendations = self._template_recommendations(
            [{"severity": r.severity, "blast_radius": f.blast_radius,
              "title": f.title, "category": r.category}
             for r in active for f in r.findings]
        )
        if recommendations:
            lines.append("  Actions:")
            for rec in recommendations[:2]:
                lines.append(f"    - {rec}")

        return "\n".join(lines)

    def _compute_risk_level(self, results: list[ProbeResult]) -> str:
        """Compute overall risk level from probe results."""
        has_critical = any(r.severity == "critical" for r in results if r.status not in ("error", "skipped"))
        has_high = any(r.severity == "high" for r in results if r.status not in ("error", "skipped"))
        has_high_blast = any(
            f.blast_radius == "high"
            for r in results if r.status not in ("error", "skipped")
            for f in r.findings
        )

        if has_critical or has_high_blast:
            return "critical"
        if has_high:
            return "high"

        has_medium = any(r.severity == "medium" for r in results if r.status not in ("error", "skipped"))
        if has_medium:
            return "medium"
        return "low"

    def _template_recommendations(self, flagged: list[dict]) -> list[str]:
        """Generate action recommendations from flagged findings."""
        recs = []
        severities = {f["severity"] for f in flagged}

        if "critical" in severities:
            recs.append("Investigate critical findings before proceeding with changes")
        if any(f.get("blast_radius") == "high" for f in flagged):
            recs.append("High blast-radius findings require 3-surgeon review")

        categories = {f.get("category", "") for f in flagged}
        if "security" in categories:
            recs.append("Security probe flagged — review before merge")
        if "context_quality" in categories:
            recs.append("Context quality degradation detected — check stale docs/mirrors")
        if "reliability" in categories:
            recs.append("Reliability issues found — check flaky tests or repeated failures")

        return recs[:3]
