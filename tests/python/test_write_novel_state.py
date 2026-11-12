"""novel-state initialization keeps existing files and honours the project path."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "plugins/write/skills/novel-state"


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


init_novel_state = load_script("init_novel_state", SKILL / "scripts/init_novel_state.py")

EXISTING = {
    "bible/voice.md": "# Voice Guide\n\nThe author's own voice rules.\n",
    "bible/rules.md": "# Story Rules\n\nNo magic after dusk.\n",
    "bible/characters/mara.md": "# Mara\n\nA ferrywoman.\n",
    "story/synopsis.md": "# Synopsis\n\nThe river remembers.\n",
    "story/outline.md": "# Outline\n\n## Act 1\n- Chapter 1: The crossing\n",
}
CREATED = ("timeline/master.md", "timeline/events.json")


class InitNovelStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.project = base / "novel-project"
        self.novel = self.project / "Work/novel"
        for rel, text in EXISTING.items():
            path = self.novel / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        # Run from somewhere else: the helper must write under its argument, not the cwd.
        self.elsewhere = base / "elsewhere"
        self.elsewhere.mkdir()
        previous = Path.cwd()
        os.chdir(self.elsewhere)
        self.addCleanup(os.chdir, previous)

    def _assert_initialized(self):
        for rel, text in EXISTING.items():
            with self.subTest(kept=rel):
                self.assertEqual((self.novel / rel).read_text(encoding="utf-8"), text)
        for rel in CREATED:
            with self.subTest(created=rel):
                self.assertTrue((self.novel / rel).is_file())
        events = json.loads((self.novel / "timeline/events.json").read_text(encoding="utf-8"))
        self.assertEqual(events["events"], [])
        self.assertTrue((self.novel / "state").is_dir())
        self.assertEqual(list(self.elsewhere.iterdir()), [])  # nothing written in the cwd

    def test_existing_bible_and_story_files_are_kept(self):
        with contextlib.redirect_stdout(io.StringIO()):
            init_novel_state.init_novel_state(str(self.project))
        self._assert_initialized()

    def test_the_command_line_path_argument_is_honoured(self):
        with mock.patch.object(sys, "argv", ["init_novel_state.py", str(self.project)]), \
                contextlib.redirect_stdout(io.StringIO()):
            init_novel_state.main()
        self._assert_initialized()

    def test_a_second_run_changes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            init_novel_state.init_novel_state(str(self.project))
            before = {p: p.read_bytes() for p in self.novel.rglob("*") if p.is_file()}
            init_novel_state.init_novel_state(str(self.project))
        after = {p: p.read_bytes() for p in self.novel.rglob("*") if p.is_file()}
        self.assertEqual(after, before)


class NovelStateSkillTextTests(unittest.TestCase):
    def test_initialization_uses_the_helper_and_never_overwrites(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("\n## Initialization", 1)[1].split("\n## ", 1)[0]
        self.assertIn("init_novel_state.py", section)
        self.assertNotRegex(section, r"cat\s+>|>\s*Work/novel")  # no shell redirect that replaces a file
        self.assertNotIn("Manual initialization", section)
        self.assertRegex(section, r"(?i)existing files")


if __name__ == "__main__":
    unittest.main()
