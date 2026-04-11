"""Tests for probe.finding event emission from ProbeEngine."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.probe_engine import (
    ProbeEngine, ProbeRegistry, ProbeResult, ProbeCategory, ProbeFinding,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ok_probe(context):
    return ProbeResult(
        probe_id="test-probe",
        category="structure",
        status="ok",
        confidence=0.95,
        severity="info",
        findings=[ProbeFinding(title="All good")],
    )


def _failing_probe(context):
    raise RuntimeError("probe exploded")


# ── Test 1: Event emitted on successful probe ───────────────────────────────


class TestProbeEventEmission:
    def test_emits_event_on_successful_probe(self):
        """After a probe runs successfully, fleet.probe.finding is emitted."""
        registry = ProbeRegistry()
        registry.register("ok-probe", _ok_probe, ProbeCategory.STRUCTURE)
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.eventbus.FleetEventBus") as MockBus:
            mock_instance = MagicMock()
            MockBus.return_value = mock_instance

            results = engine.scan("all")

            assert len(results) == 1
            assert results[0].status == "ok"
            mock_instance.emit.assert_called_once()
            call_args = mock_instance.emit.call_args
            assert call_args[0][0] == "probe.finding"
            assert call_args[0][1]["probe_id"] == "test-probe"


# ── Test 2: Event emitted on errored probe ───────────────────────────────────


    def test_emits_event_on_errored_probe(self):
        """Even when a probe errors, the error result is emitted."""
        registry = ProbeRegistry()
        registry.register("fail-probe", _failing_probe, ProbeCategory.RELIABILITY)
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.eventbus.FleetEventBus") as MockBus:
            mock_instance = MagicMock()
            MockBus.return_value = mock_instance

            results = engine.scan("all")

            assert len(results) == 1
            assert results[0].status == "error"
            mock_instance.emit.assert_called_once()
            payload = mock_instance.emit.call_args[0][1]
            assert payload["status"] == "error"


# ── Test 3: EventBus import failure doesn't break probe ─────────────────────


    def test_eventbus_import_failure_doesnt_break_probe(self):
        """If FleetEventBus can't be imported, probe still returns results."""
        registry = ProbeRegistry()
        registry.register("ok-probe", _ok_probe, ProbeCategory.STRUCTURE)
        engine = ProbeEngine(registry=registry)

        with patch.dict("sys.modules", {"multifleet.eventbus": None}):
            # Force ImportError on FleetEventBus
            with patch(
                "multifleet.probe_engine.ProbeRegistry._run_probe",
                wraps=registry._run_probe,
            ):
                # Run directly to test the _emit_probe_finding path
                result = registry._run_probe("ok-probe", {
                    "fn": _ok_probe,
                    "category": ProbeCategory.STRUCTURE,
                }, {})
                assert result.status == "ok"


# ── Test 4: Multiple probes each emit an event ──────────────────────────────


    def test_multiple_probes_each_emit_event(self):
        """Each probe in a scan emits its own event."""
        def probe_a(ctx):
            return ProbeResult("a", "structure", "ok", 0.9, "info")

        def probe_b(ctx):
            return ProbeResult("b", "change", "warning", 0.6, "medium",
                               findings=[ProbeFinding(title="drift detected")])

        registry = ProbeRegistry()
        registry.register("probe-a", probe_a, ProbeCategory.STRUCTURE)
        registry.register("probe-b", probe_b, ProbeCategory.CHANGE)
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.eventbus.FleetEventBus") as MockBus:
            mock_instance = MagicMock()
            MockBus.return_value = mock_instance

            results = engine.scan("all")

            assert len(results) == 2
            assert mock_instance.emit.call_count == 2
            emitted_ids = {c[0][1]["probe_id"] for c in mock_instance.emit.call_args_list}
            assert emitted_ids == {"a", "b"}


# ── Test 5: Event payload matches ProbeResult.to_dict() ─────────────────────


    def test_event_payload_matches_to_dict(self):
        """The emitted payload is exactly ProbeResult.to_dict()."""
        registry = ProbeRegistry()
        registry.register("ok-probe", _ok_probe, ProbeCategory.STRUCTURE)
        engine = ProbeEngine(registry=registry)

        with patch("multifleet.eventbus.FleetEventBus") as MockBus:
            mock_instance = MagicMock()
            MockBus.return_value = mock_instance

            results = engine.scan("all")
            result = results[0]

            emitted_payload = mock_instance.emit.call_args[0][1]
            expected = result.to_dict()
            # Compare key fields (duration_ms may differ slightly)
            assert emitted_payload["probe_id"] == expected["probe_id"]
            assert emitted_payload["status"] == expected["status"]
            assert emitted_payload["category"] == expected["category"]
            assert emitted_payload["confidence"] == expected["confidence"]
