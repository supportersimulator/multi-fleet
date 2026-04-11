"""Version and package structure tests for multifleet 5.0.0."""

import importlib
import pkgutil

import multifleet


def test_version_is_5():
    assert multifleet.__version__ == "5.0.0"


def test_version_string_format():
    parts = multifleet.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_core_modules_importable():
    """Key modules from every major feature area must import cleanly."""
    core = [
        "multifleet.config",
        "multifleet.nats_client",
        "multifleet.channels",
        "multifleet.discovery",
        "multifleet.security",
        "multifleet.synthesis",
        "multifleet.cross_rebuttal",
        "multifleet.liaison",
        "multifleet.hierarchy",
        "multifleet.invariance_gates",
        "multifleet.productive_wait",
        "multifleet.status_aggregator",
        "multifleet.event_stream",
        "multifleet.packets",
        "multifleet.fleet_race",
    ]
    for mod_name in core:
        mod = importlib.import_module(mod_name)
        assert mod is not None, f"Failed to import {mod_name}"


def test_module_count_at_least_20():
    """Package must have >= 20 Python modules (currently 49)."""
    pkg_path = multifleet.__path__
    modules = list(pkgutil.iter_modules(pkg_path))
    assert len(modules) >= 20, f"Only found {len(modules)} modules, expected >= 20"


def test_package_has_docstring():
    assert multifleet.__doc__ is not None
    assert "multifleet" in multifleet.__doc__.lower()
