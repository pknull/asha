"""init_skill.py output passes the repository's own plugin validator (#119)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_SKILL = REPO_ROOT / "plugins/session/skills/skill-creator/scripts/init_skill.py"


def _frontmatter(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    data = yaml.safe_load(text[4:end])
    assert isinstance(data, dict), data
    return data


def _scrubbed_env() -> dict[str, str]:
    env = dict(os.environ)
    # lib/install.sh honours MARKET_ROOT over its own location.
    env.pop("MARKET_ROOT", None)
    return env


class InitSkillTemplateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "asha"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _copy_validator_inputs(self):
        """Copy only what tests/validate-plugins.sh reads into a scratch repo."""
        ignore = shutil.ignore_patterns("__pycache__", "node_modules")
        (self.root / "tests").mkdir()
        shutil.copy2(REPO_ROOT / "tests/validate-plugins.sh", self.root / "tests")
        shutil.copy2(REPO_ROOT / "namespaces.json", self.root)
        shutil.copytree(REPO_ROOT / "plugins", self.root / "plugins", symlinks=True, ignore=ignore)
        shutil.copytree(REPO_ROOT / "harnesses", self.root / "harnesses", symlinks=True, ignore=ignore)
        (self.root / "lib").mkdir()
        for script in (REPO_ROOT / "lib").glob("*.sh"):
            shutil.copy2(script, self.root / "lib")

    def _init(self, skill_name: str, path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(INIT_SKILL), skill_name, "--path", str(path)],
            capture_output=True, text=True, env=_scrubbed_env(), timeout=60,
        )

    def test_new_plugin_skills_pass_validate_plugins(self):
        self._copy_validator_inputs()
        created = {
            # Identity namespace and a mapped one (panel -> panel-system).
            "session": ("probe-widget", "session-probe-widget"),
            "panel": ("probe-gadget", "panel-system-probe-gadget"),
        }
        for plugin, (directory, _) in created.items():
            result = self._init(directory, self.root / "plugins" / plugin / "skills")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for plugin, (directory, expected_name) in created.items():
            data = _frontmatter(self.root / "plugins" / plugin / "skills" / directory / "SKILL.md")
            self.assertEqual(data["name"], expected_name)
            self.assertIsInstance(data["description"], str)
            self.assertIn("Use when", data["description"])

        validation = subprocess.run(
            ["bash", str(self.root / "tests/validate-plugins.sh")],
            capture_output=True, text=True, env=_scrubbed_env(), timeout=300,
        )
        self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

    def test_skill_outside_a_plugin_keeps_its_directory_name(self):
        result = self._init("loose-skill", self.root / "skills")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = _frontmatter(self.root / "skills" / "loose-skill" / "SKILL.md")
        self.assertEqual(data["name"], "loose-skill")
        self.assertIsInstance(data["description"], str)


if __name__ == "__main__":
    unittest.main()
