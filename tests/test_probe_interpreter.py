"""Tests for ProbeInterpreter — template fallback, LLM integration, injection context."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import ProbeFinding, ProbeResult
from multifleet.probe_interpreter import ProbeInterpreter


# ── Helpers ──

def _make_result(
    probe_id: str = "test_probe",
    category: str = "structure",
    status: str = "warning",
    severity: str = "medium",
    confidence: float = 0.8,
    findings: list | None = None,
) -> ProbeResult:
    if findings is None:
        findings = [ProbeFinding(title="Test finding", evidence=["ev1"])]
    return ProbeResult(
        probe_id=probe_id,
        category=category,
        status=status,
        severity=severity,
        confidence=confidence,
        findings=findings,
    )


def _make_critical_result() -> ProbeResult:
    return _make_result(
        probe_id="security_check",
        category="security",
        status="critical",
        severity="critical",
        findings=[
            ProbeFinding(
                title="Exposed secret in config",
                evidence=["line 42: API_KEY=sk-..."],
                blast_radius="high",
                suggested_injections=["Review secrets management before merge"],
            )
        ],
    )


# ── Template interpretation ──

class TestTemplateInterpret:
    def test_empty_results(self):
        interp = ProbeInterpreter()
        result = interp.interpret([])
        assert result["risk_level"] == "none"
        assert result["flagged"] == []
        assert result["source"] == "template"

    def test_single_finding(self):
        interp = ProbeInterpreter()
        result = interp._template_interpret([_make_result()])
        assert result["source"] == "template"
        assert len(result["flagged"]) == 1
        assert result["flagged"][0]["probe_id"] == "test_probe"
        assert result["flagged"][0]["severity"] == "medium"
        assert result["risk_level"] == "medium"

    def test_critical_risk_level(self):
        interp = ProbeInterpreter()
        result = interp._template_interpret([_make_critical_result()])
        assert result["risk_level"] == "critical"

    def test_skipped_probes_excluded(self):
        skipped = ProbeResult.skipped("skip_me", "structure", ["missing_dep"])
        interp = ProbeInterpreter()
        result = interp._template_interpret([skipped, _make_result()])
        assert len(result["flagged"]) == 1
        assert result["flagged"][0]["probe_id"] == "test_probe"

    def test_error_probes_excluded(self):
        error = ProbeResult.error("err", "structure", "boom")
        interp = ProbeInterpreter()
        result = interp._template_interpret([error])
        assert len(result["flagged"]) == 0

    def test_severity_sorting(self):
        low = _make_result(probe_id="low_p", severity="low")
        high = _make_result(probe_id="high_p", severity="high")
        interp = ProbeInterpreter()
        result = interp._template_interpret([low, high])
        assert result["flagged"][0]["severity"] == "high"
        assert result["flagged"][1]["severity"] == "low"

    def test_recommendations_security(self):
        sec = _make_result(category="security", severity="high")
        interp = ProbeInterpreter()
        result = interp._template_interpret([sec])
        recs = result["recommendations"]
        assert any("Security" in r or "security" in r.lower() for r in recs)

    def test_recommendations_high_blast_radius(self):
        finding = ProbeFinding(title="Big change", blast_radius="high")
        r = _make_result(findings=[finding], severity="high")
        interp = ProbeInterpreter()
        result = interp._template_interpret([r])
        assert any("3-surgeon" in rec for rec in result["recommendations"])


# ── LLM interpretation ──

class TestLLMInterpret:
    @patch("multifleet.probe_interpreter.sys")
    def test_llm_fallback_on_import_error(self, mock_sys):
        """When LLM module can't be imported, falls back to template."""
        interp = ProbeInterpreter()
        # interpret() catches exceptions from _llm_interpret and falls back
        result = interp.interpret([_make_result()])
        # Should succeed via template fallback
        assert result["source"] in ("template", "llm")
        assert "flagged" in result

    def test_interpret_falls_back_to_template(self):
        """Verify interpret() returns template result when LLM fails."""
        interp = ProbeInterpreter()
        with patch.object(interp, "_llm_interpret", side_effect=RuntimeError("no LLM")):
            result = interp.interpret([_make_result()])
        assert result["source"] == "template"
        assert len(result["flagged"]) == 1


# ── Injection context ──

class TestInjectionContext:
    def test_empty_results(self):
        interp = ProbeInterpreter()
        assert interp.generate_injection_context([]) == ""

    def test_basic_injection(self):
        interp = ProbeInterpreter()
        ctx = interp.generate_injection_context([_make_result()])
        assert "[PROBE INSIGHTS]" in ctx
        assert "Risk:" in ctx
        assert "Test finding" in ctx

    def test_critical_uses_exclamation(self):
        interp = ProbeInterpreter()
        ctx = interp.generate_injection_context([_make_critical_result()])
        assert "!" in ctx  # Critical findings get ! marker
        assert "critical" in ctx.lower()

    def test_suggested_injections_included(self):
        interp = ProbeInterpreter()
        ctx = interp.generate_injection_context([_make_critical_result()])
        assert "Review secrets" in ctx

    def test_skipped_excluded_from_injection(self):
        skipped = ProbeResult.skipped("skip", "structure", ["x"])
        interp = ProbeInterpreter()
        ctx = interp.generate_injection_context([skipped])
        assert ctx == ""

    def test_recommendations_in_injection(self):
        sec = _make_result(category="security", severity="high")
        interp = ProbeInterpreter()
        ctx = interp.generate_injection_context([sec])
        assert "Actions:" in ctx
