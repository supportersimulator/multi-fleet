"""Tests for MemoryScope bridge — Python memory sources to TS canonical scopes."""

import pytest
from multifleet.memory_scope import (
    MemoryScope,
    normalize_scope,
    map_python_source,
    get_scope_for_query,
)


class TestMemoryScope:
    def test_enum_values_match_ts(self):
        """Enum values must match TS MemoryScope type exactly."""
        assert [s.value for s in MemoryScope] == [
            "core", "profile", "workspace", "session", "task",
        ]

    def test_enum_is_str(self):
        assert isinstance(MemoryScope.CORE, str)
        assert MemoryScope.CORE == "core"


class TestNormalizeScope:
    @pytest.mark.parametrize("scope,expected", [
        ("core", MemoryScope.CORE),
        ("profile", MemoryScope.PROFILE),
        ("workspace", MemoryScope.WORKSPACE),
        ("session", MemoryScope.SESSION),
        ("task", MemoryScope.TASK),
    ])
    def test_canonical_passthrough(self, scope, expected):
        assert normalize_scope(scope) == expected

    @pytest.mark.parametrize("legacy,expected", [
        ("global", MemoryScope.CORE),
        ("project", MemoryScope.WORKSPACE),
    ])
    def test_legacy_mapping(self, legacy, expected):
        """Legacy scopes map same as TS scope-utils.ts LEGACY_MAP."""
        assert normalize_scope(legacy) == expected

    def test_invalid_returns_none(self):
        assert normalize_scope("bogus") is None
        assert normalize_scope("") is None


class TestMapPythonSource:
    @pytest.mark.parametrize("source,expected", [
        ("professor", MemoryScope.CORE),
        ("brain", MemoryScope.CORE),
        ("gold_mining", MemoryScope.CORE),
        ("query", MemoryScope.WORKSPACE),
        ("semantic_search", MemoryScope.WORKSPACE),
        ("session_historian", MemoryScope.SESSION),
        ("anticipation_engine", MemoryScope.SESSION),
        ("ab_autonomous", MemoryScope.TASK),
        ("synaptic_agent", MemoryScope.TASK),
        ("auto_capture", MemoryScope.PROFILE),
    ])
    def test_known_sources(self, source, expected):
        assert map_python_source(source) == expected

    def test_unknown_defaults_to_workspace(self):
        assert map_python_source("unknown_module") == MemoryScope.WORKSPACE


class TestGetScopeForQuery:
    @pytest.mark.parametrize("query_type,expected", [
        ("professor", MemoryScope.CORE),
        ("architecture", MemoryScope.CORE),
        ("search", MemoryScope.WORKSPACE),
        ("session", MemoryScope.SESSION),
        ("task", MemoryScope.TASK),
    ])
    def test_known_query_types(self, query_type, expected):
        assert get_scope_for_query(query_type) == expected

    def test_unknown_defaults_to_workspace(self):
        assert get_scope_for_query("unknown") == MemoryScope.WORKSPACE


class TestPanelBridgeIntegration:
    """Verify panel_engine_bridge manifests include memoryScope."""

    def test_manifests_have_scope(self):
        from multifleet.panel_engine_bridge import PanelEngineBridge
        bridge = PanelEngineBridge(ide_panel_server=None, panel_bus_bridge=None)
        manifests = bridge.generate_manifests()
        for m in manifests:
            assert "memoryScope" in m, f"{m['panelId']} missing memoryScope"
            assert m["memoryScope"] in [s.value for s in MemoryScope], (
                f"{m['panelId']} has invalid scope: {m['memoryScope']}"
            )
