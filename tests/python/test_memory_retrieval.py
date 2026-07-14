#!/usr/bin/env python3
"""Narrow tests for issue #8/#9 recall retrieval and nudging."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import memory_nudge
import memory_retrieval
import recall_bench


class RetrievalTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="asha_recall_"))
        self.memory = self.root / "memory"
        self.learnings = self.root / "learnings"
        self.memory.mkdir()
        self.learnings.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _memory_file(self, description: str):
        (self.memory / "MEMORY.md").write_text(
            "- [Disk case](reference_disk_case.md) - diagnosed failure runbook\n",
            encoding="utf-8",
        )
        (self.memory / "reference_disk_case.md").write_text(
            f"---\ndescription: {description}\ntype: reference\n---\nBODY MUST NOT BE INDEXED\n",
            encoding="utf-8",
        )

    def test_frontmatter_description_break_flips_fixture_to_miss(self):
        self._memory_file("zephyrquartz controller saturation recovery")
        fixture = [{"q": "zephyrquartz controller saturation", "expect": "reference_disk_case"}]
        entries = memory_retrieval.build_entries([self.memory], self.learnings)
        first = recall_bench.run_benchmark(fixture, entries, k=5, prior={})
        self.assertTrue(first["cases"][0]["hit"])

        self._memory_file("generic diagnosed failure runbook")
        entries = memory_retrieval.build_entries([self.memory], self.learnings)
        second = recall_bench.run_benchmark(fixture, entries, k=5, prior={})
        self.assertFalse(second["cases"][0]["hit"])

    def test_memory_body_is_not_indexed(self):
        self._memory_file("generic runbook")
        entries = memory_retrieval.build_entries([self.memory], self.learnings)
        ranked = memory_retrieval.rank("BODY MUST NOT BE INDEXED", entries)
        self.assertEqual(ranked, [])

    def test_learning_title_description_and_trigger_are_retrievable(self):
        (self.learnings / "narrow-scan.md").write_text(
            "---\ntype: learning\nid: narrow-scan\ntitle: Narrow filesystem scan\n"
            "description: Avoid galactic home traversal\ntrigger: launching recursive find\n---\nbody secret\n",
            encoding="utf-8",
        )
        entries = memory_retrieval.build_entries([], self.learnings)
        self.assertEqual(memory_retrieval.rank("galactic home traversal", entries)[0]["id"], "narrow-scan")

    def test_fixture_parser_supports_documented_yaml_shape(self):
        path = self.root / "fixtures.yaml"
        path.write_text('- q: "some question"\n  expect: memory_id\n', encoding="utf-8")
        self.assertEqual(recall_bench.load_fixtures(path),
                         [{"q": "some question", "expect": "memory_id"}])

    def test_cli_is_warn_only_on_bad_fixture(self):
        path = self.root / "bad.yaml"
        path.write_text("- q: missing expectation\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(TOOLS / "recall_bench.py"), "--fixtures", str(path),
             "--project-dir", str(self.root), "--format", "json"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "warning")

    def test_nudge_build_does_not_index_other_projects(self):
        home = self.root / "home"
        project = self.root / "project"
        current_memory = project / "Memory"
        current_memory.mkdir(parents=True)
        (current_memory / "MEMORY.md").write_text(
            "- [Current](current_safe.md) - zephyr current project\n", encoding="utf-8"
        )
        (current_memory / "current_safe.md").write_text(
            "---\ndescription: zephyr current project\n---\n", encoding="utf-8"
        )
        other = home / ".claude" / "projects" / "-other" / "memory"
        other.mkdir(parents=True)
        (other / "MEMORY.md").write_text(
            "- [Other](other_private.md) - forbidden cross project\n", encoding="utf-8"
        )
        (other / "other_private.md").write_text(
            "---\ndescription: forbidden cross project\n---\n", encoding="utf-8"
        )
        index = self.root / "scoped-index.json"
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            memory_nudge.build(SimpleNamespace(
                project_dir=str(project), index=str(index), learnings_dir=str(self.learnings)
            ))
        ids = {entry.id for entry in memory_retrieval.load_index(index)}
        self.assertIn("current_safe", ids)
        self.assertNotIn("other_private", ids)
        self.assertEqual(stat.S_IMODE(index.stat().st_mode), 0o600)


class NudgeCLITest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="asha_nudge_"))
        self.index = self.root / "index.json"
        self.state = self.root / "state"
        self.log = self.root / "events.jsonl"
        entry = memory_retrieval.Entry(
            "reference_disk_case", "zephyrquartz disk pressure diagnosis",
            str(self.root / "reference_disk_case.md"), "memory",
            tuple(memory_retrieval.tokenize("zephyrquartz disk pressure diagnosis")),
        )
        self.index.write_text(json.dumps({"version": 1, "entries": [entry.json()]}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, payload: dict, env=None):
        command = [sys.executable, str(TOOLS / "memory_nudge.py"),
                   "--index", str(self.index), "--state-dir", str(self.state),
                   "--log", str(self.log), "match"]
        return subprocess.run(command, input=json.dumps(payload), text=True,
                              capture_output=True, env=env, check=False)

    def test_exactly_one_nudge_per_id_and_session(self):
        payload = {"session_id": "one", "tool_name": "Grep",
                   "tool_input": {"pattern": "zephyrquartz"}}
        first = self._run(payload)
        second = self._run(payload)
        self.assertEqual(first.returncode, 0)
        self.assertIn("reference_disk_case", first.stdout)
        self.assertEqual(second.stdout, "")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "one.json").stat().st_mode), 0o600)

    def test_unsupported_tool_is_silent(self):
        result = self._run({"session_id": "one", "tool_name": "Read",
                            "tool_input": {"file_path": "zephyrquartz"}})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_shell_kill_switch_suppresses_nudge(self):
        hook = REPO / "plugins" / "session" / "hooks" / "memory_nudge.sh"
        env = os.environ.copy()
        env.update({"ASHA_NUDGE": "0", "ASHA_NUDGE_INDEX": str(self.index)})
        result = subprocess.run(
            ["bash", str(hook)], input=json.dumps({"session_id": "off", "tool_name": "Grep",
                                                   "tool_input": {"pattern": "zephyrquartz"}}),
            text=True, capture_output=True, env=env, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_acted_read_is_logged(self):
        payload = {"session_id": "acted", "tool_name": "Grep",
                   "tool_input": {"pattern": "zephyrquartz"}}
        self._run(payload)
        acted_payload = {"session_id": "acted", "tool_name": "Read",
                         "tool_input": {"file_path": str(self.root / "reference_disk_case.md")}}
        command = [sys.executable, str(TOOLS / "memory_nudge.py"),
                   "--state-dir", str(self.state), "--log", str(self.log), "acted"]
        subprocess.run(command, input=json.dumps(acted_payload), text=True, check=True)
        statuses = [json.loads(line)["status"] for line in self.log.read_text().splitlines()]
        self.assertEqual(statuses, ["fired", "acted"])

    def test_malformed_index_fails_open(self):
        self.index.write_text("not json", encoding="utf-8")
        result = self._run({"session_id": "bad", "tool_name": "Grep",
                            "tool_input": {"pattern": "zephyrquartz"}})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
