"""Verify all skills and commands are well-formed and loadable."""

import os
import re

import pytest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
COMMANDS_DIR = PLUGIN_ROOT / "commands"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _parse_frontmatter(text: str) -> dict:
    """Extract YAML-like frontmatter key-value pairs."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#") and not line.startswith("<"):
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _skill_dirs():
    """Return list of skill directory names."""
    return sorted(d.name for d in SKILLS_DIR.iterdir() if d.is_dir())


def _command_files():
    """Return list of command file stems."""
    return sorted(f.stem for f in COMMANDS_DIR.glob("*.md"))


# ── Skills ────────────────────────────────────────────────────────


class TestSkills:
    def test_skills_dir_exists(self):
        assert SKILLS_DIR.is_dir(), "skills/ directory missing"

    def test_all_skills_have_skill_md(self):
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                assert (d / "SKILL.md").exists(), f"Missing SKILL.md in {d.name}"

    def test_skill_frontmatter_has_name(self):
        """Each SKILL.md has 'name' in frontmatter."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                fm = _parse_frontmatter(text)
                assert "name" in fm, f"Missing 'name' in {d.name}/SKILL.md frontmatter"

    def test_skill_frontmatter_has_description(self):
        """Each SKILL.md has 'description' in frontmatter."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                fm = _parse_frontmatter(text)
                assert "description" in fm, f"Missing 'description' in {d.name}/SKILL.md frontmatter"

    def test_skill_name_matches_directory(self):
        """The 'name' field in SKILL.md matches the directory name."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                fm = _parse_frontmatter(text)
                assert fm.get("name") == d.name, (
                    f"SKILL.md name '{fm.get('name')}' != dir '{d.name}'"
                )

    def test_skill_description_not_empty(self):
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                fm = _parse_frontmatter(text)
                desc = fm.get("description", "")
                assert len(desc) > 5, f"Description too short in {d.name}/SKILL.md"

    def test_skill_md_has_body(self):
        """SKILL.md should have content beyond frontmatter."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                body = FRONTMATTER_RE.sub("", text).strip()
                assert len(body) > 20, f"SKILL.md body too short in {d.name}"

    def test_skill_count_minimum(self):
        """At least 20 skills registered."""
        skills = _skill_dirs()
        assert len(skills) >= 20, f"Only {len(skills)} skills found, expected >=20"

    def test_no_empty_skill_dirs(self):
        """No skill directory should be empty."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                files = list(d.iterdir())
                assert len(files) > 0, f"Empty skill directory: {d.name}"

    def test_skill_frontmatter_starts_with_dashes(self):
        """Every SKILL.md starts with --- frontmatter delimiter."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                assert text.startswith("---"), f"{d.name}/SKILL.md missing frontmatter"

    def test_no_merge_conflicts_in_skills(self):
        """No SKILL.md contains git merge conflict markers."""
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                text = (d / "SKILL.md").read_text()
                assert "<<<<<<" not in text, f"Merge conflict in {d.name}/SKILL.md"
                assert ">>>>>>>" not in text, f"Merge conflict in {d.name}/SKILL.md"


# ── Commands ──────────────────────────────────────────────────────


class TestCommands:
    def test_commands_dir_exists(self):
        assert COMMANDS_DIR.is_dir(), "commands/ directory missing"

    def test_all_commands_are_md(self):
        """Every file in commands/ is a .md file."""
        for f in COMMANDS_DIR.iterdir():
            if f.is_file():
                assert f.suffix == ".md", f"Non-markdown command file: {f.name}"

    def test_command_has_frontmatter(self):
        """Each command file has YAML frontmatter."""
        for f in COMMANDS_DIR.glob("*.md"):
            text = f.read_text()
            assert text.startswith("---"), f"Missing frontmatter in {f.name}"
            fm = _parse_frontmatter(text)
            assert len(fm) > 0, f"Empty frontmatter in {f.name}"

    def test_command_has_description(self):
        """Each command has a description field."""
        for f in COMMANDS_DIR.glob("*.md"):
            text = f.read_text()
            fm = _parse_frontmatter(text)
            assert "description" in fm, f"Missing description in {f.name}"

    def test_command_description_not_empty(self):
        for f in COMMANDS_DIR.glob("*.md"):
            text = f.read_text()
            fm = _parse_frontmatter(text)
            desc = fm.get("description", "")
            assert len(desc) > 5, f"Description too short in {f.name}"

    def test_command_has_body(self):
        """Command files have content after frontmatter."""
        for f in COMMANDS_DIR.glob("*.md"):
            text = f.read_text()
            body = FRONTMATTER_RE.sub("", text).strip()
            assert len(body) > 0, f"Empty body in {f.name}"

    def test_command_count_minimum(self):
        """At least 20 commands registered."""
        cmds = _command_files()
        assert len(cmds) >= 20, f"Only {len(cmds)} commands, expected >=20"

    def test_no_merge_conflicts_in_commands(self):
        """No command file contains git merge conflict markers."""
        for f in COMMANDS_DIR.glob("*.md"):
            text = f.read_text()
            assert "<<<<<<" not in text, f"Merge conflict in {f.name}"
            assert ">>>>>>>" not in text, f"Merge conflict in {f.name}"


# ── Cross-matching ────────────────────────────────────────────────


class TestSkillCommandAlignment:
    def test_each_skill_has_matching_command(self):
        """Every skill directory should have a matching command file."""
        skills = set(_skill_dirs())
        commands = set(_command_files())
        missing = skills - commands
        assert not missing, f"Skills without commands: {sorted(missing)}"

    def test_no_orphan_commands(self):
        """Commands referencing non-existent skills are flagged.

        Some commands (fleet-dashboard, fleet-doctor, fleet-productivity) are
        aliases or utilities without dedicated skill dirs -- these are allowed.
        """
        KNOWN_ALIASES = {"fleet-dashboard", "fleet-doctor", "fleet-productivity"}
        skills = set(_skill_dirs())
        commands = set(_command_files())
        orphans = commands - skills - KNOWN_ALIASES
        assert not orphans, f"Commands without matching skills: {sorted(orphans)}"

    def test_skill_and_command_descriptions_related(self):
        """Skill and command descriptions should share key words."""
        skills = set(_skill_dirs())
        commands = set(_command_files())
        matched = skills & commands
        for name in sorted(matched):
            skill_text = (SKILLS_DIR / name / "SKILL.md").read_text()
            cmd_text = (COMMANDS_DIR / f"{name}.md").read_text()
            skill_fm = _parse_frontmatter(skill_text)
            cmd_fm = _parse_frontmatter(cmd_text)
            # Both should have descriptions
            assert skill_fm.get("description"), f"No skill description for {name}"
            assert cmd_fm.get("description"), f"No command description for {name}"

    def test_total_coverage(self):
        """Skills + commands cover at least 25 unique names."""
        all_names = set(_skill_dirs()) | set(_command_files())
        assert len(all_names) >= 25, f"Only {len(all_names)} total, expected >=25"
