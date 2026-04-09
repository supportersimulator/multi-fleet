"""Tests for MkDocs documentation site infrastructure."""

import re
from pathlib import Path

import pytest

MULTI_FLEET_ROOT = Path(__file__).parent.parent


def _parse_yaml_simple(text: str) -> dict:
    """Minimal YAML-like parser for mkdocs.yml — avoids PyYAML dependency.

    Extracts site_name, site_url, and nav section names.  Not a full parser.
    """
    result: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        # Top-level scalar keys
        for key in ("site_name", "site_url", "site_description"):
            if stripped.startswith(f"{key}:"):
                result[key] = stripped.split(":", 1)[1].strip()
    return result


def _extract_nav_sections(text: str) -> list[str]:
    """Extract top-level nav section names from mkdocs.yml text."""
    sections = []
    in_nav = False
    for line in text.splitlines():
        if line.strip().startswith("nav:"):
            in_nav = True
            continue
        if in_nav:
            # Top-level nav items are indented exactly 2 spaces with '- '
            if re.match(r"^  - ", line):
                # "  - Home: index.md" or "  - Getting Started:"
                match = re.match(r"^  - ([^:]+):", line)
                if match:
                    sections.append(match.group(1).strip())
            elif line.strip() and not line.startswith(" "):
                # Exited nav block
                break
    return sections


def _extract_nav_files(text: str) -> list[str]:
    """Extract all .md file references from nav block."""
    files = []
    in_nav = False
    for line in text.splitlines():
        if line.strip().startswith("nav:"):
            in_nav = True
            continue
        if in_nav:
            if line.strip() and not line.startswith(" "):
                break
            # Match any "Key: file.md" pattern
            match = re.search(r":\s+(\S+\.md)\s*$", line)
            if match:
                files.append(match.group(1))
    return files


class TestMkDocsConfig:
    """Verify mkdocs.yml exists and is well-formed."""

    def test_mkdocs_yml_exists(self):
        config_path = MULTI_FLEET_ROOT / "mkdocs.yml"
        assert config_path.exists(), "mkdocs.yml must exist at multi-fleet root"

    def test_mkdocs_yml_has_site_config(self):
        config_path = MULTI_FLEET_ROOT / "mkdocs.yml"
        parsed = _parse_yaml_simple(config_path.read_text())
        assert parsed.get("site_name") == "Multi-Fleet"
        assert parsed.get("site_url") == "https://multi-fleet.dev"

    def test_nav_structure_complete(self):
        """Nav must include Home, Getting Started, Architecture, API Reference, Pricing."""
        config_path = MULTI_FLEET_ROOT / "mkdocs.yml"
        sections = _extract_nav_sections(config_path.read_text())
        required = {"Home", "Getting Started", "Architecture", "API Reference", "Pricing"}
        found = set(sections)
        assert required.issubset(found), (
            f"Missing nav sections: {required - found}"
        )


class TestDocsContent:
    """Verify all required docs files exist."""

    REQUIRED_FILES = [
        "docs/index.md",
        "docs/getting-started/install.md",
        "docs/getting-started/quickstart.md",
        "docs/getting-started/config.md",
        "docs/architecture/overview.md",
        "docs/architecture/layers.md",
        "docs/architecture/probes.md",
        "docs/api/mcp-tools.md",
        "docs/api/skills.md",
        "docs/api/packets.md",
        "docs/pricing.md",
    ]

    @pytest.mark.parametrize("rel_path", REQUIRED_FILES)
    def test_doc_file_exists(self, rel_path):
        full_path = MULTI_FLEET_ROOT / rel_path
        assert full_path.exists(), f"Required doc file missing: {rel_path}"

    @pytest.mark.parametrize("rel_path", REQUIRED_FILES)
    def test_doc_file_not_empty(self, rel_path):
        full_path = MULTI_FLEET_ROOT / rel_path
        content = full_path.read_text()
        assert len(content.strip()) > 50, f"Doc file too short: {rel_path}"


class TestNavReferencesExist:
    """Every file referenced in mkdocs.yml nav must exist in docs/."""

    def test_all_nav_files_exist(self):
        config_path = MULTI_FLEET_ROOT / "mkdocs.yml"
        nav_files = _extract_nav_files(config_path.read_text())
        docs_dir = MULTI_FLEET_ROOT / "docs"

        missing = []
        for rel_path in nav_files:
            if not (docs_dir / rel_path).exists():
                missing.append(rel_path)

        assert not missing, f"Nav references missing files: {missing}"
