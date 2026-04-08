"""Tests that validate architecture documentation stays in sync with codebase."""

import os
from pathlib import Path

import pytest

MULTI_FLEET = Path(__file__).resolve().parent.parent
MULTIFLEET_PKG = MULTI_FLEET / "multifleet"
TESTS_DIR = MULTI_FLEET / "tests"
SKILLS_DIR = MULTI_FLEET / "skills"


def _count_py_modules(root: Path) -> int:
    """Count .py files excluding __pycache__."""
    return sum(
        1
        for f in root.rglob("*.py")
        if "__pycache__" not in str(f)
    )


class TestModuleCount:
    """Ensure module count stays above documented threshold."""

    def test_module_count_at_least_90(self):
        """The codebase should have at least 90 non-test Python modules."""
        count = _count_py_modules(MULTIFLEET_PKG)
        assert count >= 90, (
            f"Expected >= 90 modules in multifleet/, found {count}. "
            "If modules were intentionally removed, update ARCHITECTURE.md."
        )


class TestTestFileCount:
    """Ensure test coverage breadth stays above documented threshold."""

    def test_test_file_count_at_least_90(self):
        """The test suite should have at least 90 test files."""
        test_files = list(TESTS_DIR.glob("test_*.py"))
        count = len(test_files)
        assert count >= 90, (
            f"Expected >= 90 test files, found {count}. "
            "If tests were consolidated, update README.md."
        )


class TestSkillCount:
    """Ensure skill count stays above documented threshold."""

    def test_skill_count_at_least_25(self):
        """The plugin should have at least 25 skills."""
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills/ directory not found")
        skill_dirs = [
            d for d in SKILLS_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        count = len(skill_dirs)
        assert count >= 25, (
            f"Expected >= 25 skills, found {count}. "
            "If skills were removed, update README.md."
        )


class TestArchitectureMdExists:
    """Ensure ARCHITECTURE.md exists and has substance."""

    def test_architecture_md_exists(self):
        """ARCHITECTURE.md must exist in the multi-fleet root."""
        arch_path = MULTI_FLEET / "ARCHITECTURE.md"
        assert arch_path.is_file(), (
            f"ARCHITECTURE.md not found at {arch_path}"
        )

    def test_architecture_md_has_content(self):
        """ARCHITECTURE.md should be a substantial document."""
        arch_path = MULTI_FLEET / "ARCHITECTURE.md"
        if not arch_path.is_file():
            pytest.skip("ARCHITECTURE.md not found")
        content = arch_path.read_text()
        lines = content.strip().splitlines()
        assert len(lines) >= 100, (
            f"ARCHITECTURE.md has only {len(lines)} lines, expected >= 100"
        )


class TestReadmeMentionsFeatures:
    """Ensure README.md documents all major features."""

    @pytest.fixture
    def readme_content(self):
        readme_path = MULTI_FLEET / "README.md"
        assert readme_path.is_file(), "README.md not found"
        return readme_path.read_text().lower()

    @pytest.mark.parametrize("feature", [
        "probe engine",
        "chief synthesis",
        "ide bridge",
        "evidence ledger",
        "rebuttal",
        "liaison",
        "invariance",
        "architecture.md",
    ])
    def test_readme_mentions_feature(self, readme_content, feature):
        """README.md should mention all major system features."""
        assert feature in readme_content, (
            f"README.md does not mention '{feature}'"
        )
