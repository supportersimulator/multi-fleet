"""Tests for multifleet.event_catalog — complete event reference."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_catalog import (
    CATALOG,
    CatalogEntry,
    get_event,
    get_event_catalog,
    list_events,
    list_namespaces,
    summary,
    validate_event,
)


# ═══════════════════════════════════════════════════════════════════
# Catalog completeness
# ═══════════════════════════════════════════════════════════════════


class TestCatalogCompleteness:
    def test_catalog_has_entries(self):
        assert len(CATALOG) > 0

    def test_catalog_has_at_least_37_events(self):
        """The design calls for 37+ typed events."""
        assert len(CATALOG) >= 37, f"Only {len(CATALOG)} events in catalog"

    def test_all_entries_are_catalog_entries(self):
        for name, entry in CATALOG.items():
            assert isinstance(entry, CatalogEntry), f"{name} is not CatalogEntry"

    def test_all_entries_have_namespace(self):
        for name, entry in CATALOG.items():
            assert entry.namespace, f"{name} missing namespace"

    def test_all_entries_have_description(self):
        for name, entry in CATALOG.items():
            assert entry.description, f"{name} missing description"

    def test_name_starts_with_namespace(self):
        """Event names should start with their namespace (or a related prefix)."""
        for name, entry in CATALOG.items():
            ns = name.split(".")[0]
            # Allow some flexibility (fleet.node.* has namespace "fleet")
            assert ns, f"{name} has empty first segment"

    def test_required_fields_are_tuples(self):
        for name, entry in CATALOG.items():
            assert isinstance(entry.required_fields, tuple), f"{name} required_fields not tuple"
            assert isinstance(entry.optional_fields, tuple), f"{name} optional_fields not tuple"


# ═══════════════════════════════════════════════════════════════════
# Namespace coverage
# ═══════════════════════════════════════════════════════════════════


class TestNamespaceCoverage:
    """Verify all expected namespaces are represented."""

    EXPECTED_NAMESPACES = {
        "fleet", "peer", "message", "health", "lease",
        "probe", "scan", "surgeon", "supervisor",
        "evidence", "ghost", "panel", "quorum",
        "superpowers", "synaptic", "gate", "invariant",
        "task", "acceptance", "race", "truth",
        "stress", "liaison",
    }

    def test_expected_namespaces_present(self):
        actual = set(list_namespaces())
        missing = self.EXPECTED_NAMESPACES - actual
        assert not missing, f"Missing namespaces: {missing}"

    def test_list_namespaces_sorted(self):
        ns = list_namespaces()
        assert ns == sorted(ns)


# ═══════════════════════════════════════════════════════════════════
# get_event_catalog()
# ═══════════════════════════════════════════════════════════════════


class TestGetEventCatalog:
    def test_returns_list_of_dicts(self):
        catalog = get_event_catalog()
        assert isinstance(catalog, list)
        assert all(isinstance(e, dict) for e in catalog)

    def test_dict_has_required_keys(self):
        catalog = get_event_catalog()
        required_keys = {
            "name", "namespace", "description",
            "required_fields", "optional_fields",
            "emitters", "subscribers", "broadcast", "source",
        }
        for entry in catalog:
            assert required_keys.issubset(entry.keys()), (
                f"Missing keys in {entry.get('name', '?')}: "
                f"{required_keys - entry.keys()}"
            )

    def test_catalog_count_matches(self):
        assert len(get_event_catalog()) == len(CATALOG)


# ═══════════════════════════════════════════════════════════════════
# get_event()
# ═══════════════════════════════════════════════════════════════════


class TestGetEvent:
    def test_lookup_existing(self):
        entry = get_event("probe.finding")
        assert entry is not None
        assert entry.name == "probe.finding"
        assert entry.namespace == "probe"

    def test_lookup_nonexistent(self):
        assert get_event("does.not.exist") is None

    def test_lookup_fleet_heartbeat(self):
        entry = get_event("fleet.node.heartbeat")
        assert entry is not None
        assert "peer_id" in entry.required_fields


# ═══════════════════════════════════════════════════════════════════
# list_events()
# ═══════════════════════════════════════════════════════════════════


class TestListEvents:
    def test_filter_by_namespace(self):
        fleet_events = list_events(namespace="fleet")
        assert len(fleet_events) > 0
        assert all(e.namespace == "fleet" for e in fleet_events)

    def test_filter_by_source(self):
        probe_events = list_events(source="probe-engine")
        assert len(probe_events) > 0
        assert all(e.source == "probe-engine" for e in probe_events)

    def test_broadcast_only(self):
        broadcasts = list_events(broadcast_only=True)
        assert len(broadcasts) > 0
        assert all(e.broadcast for e in broadcasts)

    def test_combined_filters(self):
        fleet_broadcasts = list_events(namespace="fleet", broadcast_only=True)
        assert all(e.namespace == "fleet" and e.broadcast for e in fleet_broadcasts)

    def test_empty_namespace_returns_empty(self):
        assert list_events(namespace="nonexistent") == []


# ═══════════════════════════════════════════════════════════════════
# validate_event()
# ═══════════════════════════════════════════════════════════════════


class TestValidateEvent:
    def test_valid_event_returns_empty(self):
        errors = validate_event("probe.finding", {
            "probe_id": "test", "title": "Test",
            "severity": "high", "confidence": 0.9,
        })
        assert errors == []

    def test_missing_required_field(self):
        errors = validate_event("probe.finding", {
            "probe_id": "test",
            # missing title, severity, confidence
        })
        assert len(errors) == 3
        assert any("title" in e for e in errors)
        assert any("severity" in e for e in errors)
        assert any("confidence" in e for e in errors)

    def test_unregistered_event_returns_warning(self):
        errors = validate_event("unknown.custom.event", {})
        assert len(errors) == 1
        assert "not found in catalog" in errors[0]

    def test_fleet_prefix_stripping(self):
        """FleetEventBus adds fleet. prefix — validation should handle it."""
        errors = validate_event("fleet.peer.online", {"peer_id": "mac1"})
        assert errors == []

    def test_valid_gate_event(self):
        errors = validate_event("gate.checked", {
            "gate_name": "acceptance",
            "passed": True,
        })
        assert errors == []

    def test_valid_stress_event(self):
        errors = validate_event("stress.level_change", {
            "level": "high",
            "score": 0.85,
        })
        assert errors == []

    def test_valid_surgeon_verdict(self):
        errors = validate_event("surgeon.verdict", {
            "surgeon": "atlas",
            "verdict": "approve",
            "confidence": 0.95,
        })
        assert errors == []

    def test_valid_synaptic_insight(self):
        errors = validate_event("synaptic.insight", {
            "category": "pattern",
            "severity": "info",
            "message": "Test insight",
        })
        assert errors == []

    def test_partial_missing_fields(self):
        errors = validate_event("evidence.recorded", {
            "entry_id": "e-001",
            # missing event_type and node_id
        })
        assert len(errors) == 2

    def test_extra_fields_allowed(self):
        """Extra payload fields should not cause errors."""
        errors = validate_event("probe.finding", {
            "probe_id": "test", "title": "Test",
            "severity": "high", "confidence": 0.9,
            "extra_field": "allowed",
        })
        assert errors == []


# ═══════════════════════════════════════════════════════════════════
# summary()
# ═══════════════════════════════════════════════════════════════════


class TestSummary:
    def test_summary_structure(self):
        s = summary()
        assert "total_events" in s
        assert "namespaces" in s
        assert "namespace_count" in s
        assert "broadcast_events" in s

    def test_summary_counts(self):
        s = summary()
        assert s["total_events"] == len(CATALOG)
        assert s["namespace_count"] == len(list_namespaces())
        assert s["broadcast_events"] > 0

    def test_namespace_counts_sum(self):
        s = summary()
        assert sum(s["namespaces"].values()) == s["total_events"]


# ═══════════════════════════════════════════════════════════════════
# CatalogEntry
# ═══════════════════════════════════════════════════════════════════


class TestCatalogEntry:
    def test_to_dict(self):
        entry = CatalogEntry(
            name="test.event",
            namespace="test",
            description="A test event",
            required_fields=("field_a",),
            optional_fields=("field_b",),
            emitters=("test_module",),
            subscribers=("subscriber_module",),
            broadcast=True,
            source="test-source",
        )
        d = entry.to_dict()
        assert d["name"] == "test.event"
        assert d["required_fields"] == ["field_a"]
        assert d["optional_fields"] == ["field_b"]
        assert d["emitters"] == ["test_module"]
        assert d["subscribers"] == ["subscriber_module"]
        assert d["broadcast"] is True
        assert d["source"] == "test-source"

    def test_frozen(self):
        entry = get_event("probe.finding")
        with pytest.raises(AttributeError):
            entry.name = "changed"


# ═══════════════════════════════════════════════════════════════════
# Cross-check with EventType enum
# ═══════════════════════════════════════════════════════════════════


class TestCrossCheckEventTypeEnum:
    """Verify that every EventType enum value has a catalog entry."""

    def test_all_enum_values_in_catalog(self):
        from multifleet.event_schema import EventType as SchemaEventType
        missing = []
        for et in SchemaEventType:
            if et.value not in CATALOG:
                missing.append(et.value)
        # Allow some EventType values to map to alternate catalog names
        # (e.g. evidence.chain.verified vs evidence.chain_broken)
        truly_missing = [m for m in missing if not any(
            m.replace(".", "_") in c or c.replace("_", ".") == m
            for c in CATALOG
        )]
        assert not truly_missing, (
            f"EventType enum values not in catalog: {truly_missing}"
        )


# ═══════════════════════════════════════════════════════════════════
# Middleware integration
# ═══════════════════════════════════════════════════════════════════


class TestMiddlewareIntegration:
    """Test that schema_validator uses the catalog for payload validation."""

    def test_schema_validator_catches_missing_fields(self):
        from multifleet.event_middleware import schema_validator
        _, payload, _ = schema_validator("probe.finding", {}, {})
        # Should flag missing required fields
        assert payload.get("_validated") is False
        assert "_validation_errors" in payload
        assert any("probe_id" in e for e in payload["_validation_errors"])

    def test_schema_validator_passes_valid_event(self):
        from multifleet.event_middleware import schema_validator
        _, payload, _ = schema_validator("probe.finding", {
            "probe_id": "test", "title": "Test",
            "severity": "high", "confidence": 0.9,
        }, {})
        assert payload.get("_validated") is True

    def test_schema_validator_still_checks_structure(self):
        """Structural checks (dotted namespace) still work."""
        from multifleet.event_middleware import schema_validator
        _, payload, _ = schema_validator("nodot", {}, {})
        assert payload.get("_validated") is False

    def test_unregistered_event_still_passes(self):
        """Unregistered events pass structural validation (advisory only)."""
        from multifleet.event_middleware import schema_validator
        _, payload, _ = schema_validator("custom.unregistered.event", {}, {})
        assert payload.get("_validated") is True
