"""Verify every .py module in multifleet/ can be imported without errors.

Tests use importlib to dynamically import each module. Modules that require
external services (NATS, Redis, etc.) are expected to define their dependencies
at function-call time, not at import time -- so imports should always succeed.
"""

import importlib
import sys
from pathlib import Path

import pytest

MULTIFLEET_DIR = Path(__file__).parent.parent / "multifleet"

# Ensure the package root is importable
sys.path.insert(0, str(MULTIFLEET_DIR.parent))

# Collect all .py modules (skip __pycache__, __init__, __main__)
_SKIP = {"__init__", "__main__"}
_ALL_MODULES = sorted(
    f.stem
    for f in MULTIFLEET_DIR.glob("*.py")
    if f.stem not in _SKIP and not f.stem.startswith(".")
)


class TestModuleImports:
    """Each module in multifleet/ should import cleanly."""

    @pytest.mark.parametrize("mod", _ALL_MODULES)
    def test_import(self, mod):
        """Import multifleet.{mod} and verify no ImportError or SyntaxError."""
        try:
            importlib.import_module(f"multifleet.{mod}")
        except ModuleNotFoundError as exc:
            # Allow skip if a third-party dep is missing in this env
            pytest.skip(f"Optional dependency missing: {exc}")

    def test_package_init(self):
        """The multifleet package itself imports."""
        importlib.import_module("multifleet")

    def test_package_has_version(self):
        """multifleet.__version__ is defined."""
        pkg = importlib.import_module("multifleet")
        assert hasattr(pkg, "__version__")
        assert isinstance(pkg.__version__, str)
        assert len(pkg.__version__) >= 3  # e.g. "1.0"

    def test_module_count_minimum(self):
        """At least 30 modules in multifleet/."""
        assert len(_ALL_MODULES) >= 30, (
            f"Only {len(_ALL_MODULES)} modules, expected >=30"
        )

    def test_no_duplicate_modules(self):
        """No two .py files share the same stem."""
        all_stems = [f.stem for f in MULTIFLEET_DIR.glob("*.py")]
        assert len(all_stems) == len(set(all_stems)), "Duplicate module names found"

    def test_all_modules_are_python(self):
        """Every non-directory file in multifleet/ with .py extension is valid Python."""
        for f in MULTIFLEET_DIR.glob("*.py"):
            if f.stem in _SKIP:
                continue
            # compile() catches SyntaxError without executing
            source = f.read_text()
            compile(source, str(f), "exec")

    def test_probes_subpackage(self):
        """If multifleet/probes/ exists, its __init__ imports."""
        probes_dir = MULTIFLEET_DIR / "probes"
        if probes_dir.is_dir() and (probes_dir / "__init__.py").exists():
            importlib.import_module("multifleet.probes")

    @pytest.mark.parametrize("mod", _ALL_MODULES)
    def test_module_has_no_syntax_errors(self, mod):
        """Each module compiles without SyntaxError."""
        src = (MULTIFLEET_DIR / f"{mod}.py").read_text()
        compile(src, f"multifleet/{mod}.py", "exec")
