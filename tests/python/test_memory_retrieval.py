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
import warnings
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

    def test_comments_only_starter_is_a_valid_empty_benchmark(self):
        # The shipped template is all comments; that is a deliberate empty set,
        # not an error.
        path = self.root / "starter.yaml"
        path.write_text("# add fixtures here\n\n# - q: \"example\"\n#   expect: mem\n",
                        encoding="utf-8")
        self.assertEqual(recall_bench.load_fixtures(path), [])

    def test_unparseable_content_warns_instead_of_scoring_empty(self):
        # Wrong keys ('- question:'/'expected:') used to parse to zero rows and
        # score a silent, successful 0/0 — the exact silent failure the
        # benchmark exists to catch (2026-08-04 adversarial review).
        path = self.root / "wrong-keys.yaml"
        path.write_text('- question: "where is x"\n  expected: mem_x\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            recall_bench.load_fixtures(path)
        result = subprocess.run(
            [sys.executable, str(TOOLS / "recall_bench.py"), "--fixtures", str(path),
             "--project-dir", str(self.root), "--format", "json"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "warning")

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


class WorkspaceDiscoveryTest(unittest.TestCase):
    """Workspace v2 issue #48: source-aware, canonically contained reads."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="asha_ws_recall_"))
        self.home = self.root / "home"
        self.ws = self.home / "Code" / "ws"
        self.child = self.ws / "child"
        self.child_memory = self.child / "Memory"
        self.workspace_memory = self.ws / "Memory"
        self.learnings = self.root / "learnings"
        for path in (self.child_memory, self.workspace_memory, self.learnings):
            path.mkdir(parents=True)
        (self.ws / ".asha").mkdir()
        (self.ws / ".asha" / "workspace.json").write_text(json.dumps({
            "version": 1,
            "workspace_name": "ws",
            "repositories": [{"path": "child"}],
        }), encoding="utf-8")
        self._entry(self.child_memory, "child_item", "child local signal")
        self._entry(self.workspace_memory, "workspace_item", "workspace shared signal")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _entry(directory: Path, entry_id: str, description: str) -> None:
        with (directory / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write(f"- [{entry_id}]({entry_id}.md) - {description}\n")
        (directory / f"{entry_id}.md").write_text(
            f"---\ndescription: {description}\n---\nbody\n", encoding="utf-8"
        )

    def test_child_launch_classifies_both_planes_without_duplicates(self):
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.child, home=self.home
        )
        entries = memory_retrieval.build_entries(
            memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        by_id = {entry.id: entry for entry in entries}
        self.assertEqual(by_id["child_item"].source, "memory")
        self.assertEqual(by_id["workspace_item"].source, "workspace")
        self.assertEqual([entry.id for entry in entries].count("workspace_item"), 1)

    def test_workspace_root_launch_loads_operational_plane_once_as_workspace(self):
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.ws, home=self.home
        )
        entries = memory_retrieval.build_entries(
            memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        workspace = [entry for entry in entries if entry.id == "workspace_item"]
        self.assertEqual(len(workspace), 1)
        self.assertEqual(workspace[0].source, "workspace")

    def test_operational_root_symlink_escape_disables_workspace_source(self):
        outside = self.root / "outside"
        outside.mkdir()
        self._entry(outside, "foreign", "foreign secret signal")
        shutil.rmtree(self.workspace_memory)
        self.workspace_memory.symlink_to(outside, target_is_directory=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
                self.child, home=self.home
            )
        self.assertIsNone(workspace_dir)
        self.assertTrue(any("outside the workspace" in str(w.message) for w in caught))
        entries = memory_retrieval.build_entries(memory_dirs, self.learnings)
        self.assertNotIn("foreign", {entry.id for entry in entries})

    def test_catalogue_and_glob_symlink_escapes_are_skipped(self):
        outside = self.root / "secret.md"
        outside.write_text("---\ndescription: foreign secret\n---\n", encoding="utf-8")
        (self.workspace_memory / "escape.md").symlink_to(outside)
        with (self.workspace_memory / "MEMORY.md").open("a", encoding="utf-8") as handle:
            handle.write("- [catalogue_escape](../secret.md) - foreign catalogue\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            entries = memory_retrieval.memory_entries(
                [self.workspace_memory], source="workspace",
                containment_root=self.workspace_memory.resolve(),
            )
        self.assertNotIn("escape", {entry.id for entry in entries})
        self.assertNotIn("secret", {entry.id for entry in entries})
        self.assertGreaterEqual(len(caught), 1)

    def test_contained_symlink_target_is_allowed(self):
        actual = self.workspace_memory / "actual.md"
        actual.write_text("---\ndescription: contained linked signal\n---\n", encoding="utf-8")
        (self.workspace_memory / "linked.md").symlink_to(actual)
        entries = memory_retrieval.memory_entries(
            [self.workspace_memory], source="workspace",
            containment_root=self.workspace_memory.resolve(),
        )
        self.assertIn(str(actual.resolve()), {entry.path for entry in entries})

    def test_invalid_workspace_fails_closed_without_losing_project_or_learning(self):
        (self.ws / ".asha" / "workspace.json").write_text(
            '{"version":9,"workspace_name":"bad"}', encoding="utf-8"
        )
        (self.learnings / "stable.md").write_text(
            "---\nid: stable\ndescription: stable learning\n---\n", encoding="utf-8"
        )
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.child, home=self.home
        )
        self.assertIsNone(workspace_dir)
        entries = memory_retrieval.build_entries(memory_dirs, self.learnings)
        self.assertIn("child_item", {entry.id for entry in entries})
        self.assertIn("stable", {entry.id for entry in entries})

    def test_workspace_change_alters_cached_source_signature(self):
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.child, home=self.home
        )
        first = memory_retrieval.source_signature(
            memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        self._entry(self.workspace_memory, "later", "later workspace signal")
        second = memory_retrieval.source_signature(
            memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        self.assertNotEqual(first, second)

    def test_cache_rebuilds_when_same_directory_changes_source_classification(self):
        manifest = self.ws / ".asha" / "workspace.json"
        manifest_bytes = manifest.read_bytes()
        manifest.unlink()
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.ws, home=self.home
        )
        self.assertIsNone(workspace_dir)
        index = self.root / "retrieval-index.json"
        first = memory_retrieval.dump_index(
            index, memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        first_item = next(row for row in first["entries"] if row["id"] == "workspace_item")
        self.assertEqual(first_item["source"], "memory")

        manifest.write_bytes(manifest_bytes)
        memory_dirs, workspace_dir = memory_retrieval.discover_retrieval_sources(
            self.ws, home=self.home
        )
        second = memory_retrieval.dump_index(
            index, memory_dirs, self.learnings, workspace_dir=workspace_dir
        )
        second_item = next(row for row in second["entries"] if row["id"] == "workspace_item")
        self.assertEqual(second_item["source"], "workspace")


class WorkspaceRankingOracleTest(unittest.TestCase):
    """Issue #49 deterministic repository-side ranking oracle.

    Pinned oracle (established 2026-08-08 because issue #49 omitted it):
    query = ``workspace context retrieval ranking``; corpus IDs and source/
    descriptions are exactly ORACLE_SPEC below (8 entries total, 7 competing
    non-workspace entries); expected top-5 IDs are ORACLE_TOP5. Changing any
    of these values is a contract change, not fixture cleanup.
    """

    QUERY = "workspace context retrieval ranking"
    ORACLE_SPEC = (
        ("l-bravo", "learning", "workspace context retrieval ranking"),
        ("m-alpha", "memory", "workspace context retrieval ranking"),
        ("w-target", "workspace", "workspace context retrieval ranking"),
        ("m-charlie", "memory", "workspace context retrieval"),
        ("m-delta", "memory", "workspace context ranking"),
        ("m-echo", "memory", "workspace retrieval ranking"),
        ("m-foxtrot", "memory", "context retrieval ranking"),
        ("m-golf", "memory", "workspace context"),
    )
    ORACLE_TOP5 = ["l-bravo", "m-alpha", "w-target", "m-echo", "m-foxtrot"]

    @classmethod
    def _entries(cls, include_workspace=True):
        return [memory_retrieval.Entry(
            entry_id, description, f"/{source}/{entry_id}.md", source,
            tuple(memory_retrieval.tokenize(description)),
        ) for entry_id, source, description in cls.ORACLE_SPEC
            if include_workspace or source != "workspace"]

    def test_exact_competitive_top_five(self):
        ranked = memory_retrieval.rank(self.QUERY, self._entries(), limit=5)
        self.assertEqual([row["id"] for row in ranked], self.ORACLE_TOP5)

    def test_equal_score_legacy_source_precedes_workspace(self):
        description = "equal tie token"
        entries = [
            memory_retrieval.Entry("a-workspace", description, "/a", "workspace",
                                   tuple(memory_retrieval.tokenize(description))),
            memory_retrieval.Entry("z-memory", description, "/z", "memory",
                                   tuple(memory_retrieval.tokenize(description))),
        ]
        self.assertEqual(
            [r["id"] for r in memory_retrieval.rank(description, entries, 2)],
            ["z-memory", "a-workspace"],
        )

    def test_memory_learning_and_unknown_source_keep_legacy_id_path_order(self):
        description = "equal tie token"
        entries = [memory_retrieval.Entry(
            entry_id, description, path, source,
            tuple(memory_retrieval.tokenize(description)),
        ) for entry_id, source, path in (
            ("b-memory", "memory", "/b"),
            ("a-learning", "learning", "/a"),
            ("c-future", "future", "/c"),
        )]
        self.assertEqual(
            [r["id"] for r in memory_retrieval.rank(description, entries, 3)],
            ["a-learning", "b-memory", "c-future"],
        )

    def test_no_workspace_results_are_byte_identical_to_legacy_sort(self):
        entries = self._entries(include_workspace=False)
        actual = memory_retrieval.rank(self.QUERY, entries, limit=5)
        # Pre-v2 serialized result captured before adding source-rank. This
        # pins scoring fields and existing memory/learning tie order as bytes.
        expected_bytes = (
            '[{"corpus_size":7,"description":"workspace context retrieval ranking",'
            '"entry_tokens":4,"id":"l-bravo","max_overlap_idf":1.287682,'
            '"min_overlap_df":5,"overlap":["context","ranking","retrieval",'
            '"workspace"],"overlap_idf":4.842427,"path":"/learning/l-bravo.md",'
            '"score":1.151639,"source":"learning"},{"corpus_size":7,'
            '"description":"workspace context retrieval ranking","entry_tokens":4,'
            '"id":"m-alpha","max_overlap_idf":1.287682,"min_overlap_df":5,'
            '"overlap":["context","ranking","retrieval","workspace"],'
            '"overlap_idf":4.842427,"path":"/memory/m-alpha.md","score":1.151639,'
            '"source":"memory"},{"corpus_size":7,"description":"workspace '
            'retrieval ranking","entry_tokens":3,"id":"m-echo",'
            '"max_overlap_idf":1.287682,"min_overlap_df":5,"overlap":["ranking",'
            '"retrieval","workspace"],"overlap_idf":3.708896,"path":"/memory/'
            'm-echo.md","score":0.7801,"source":"memory"},{"corpus_size":7,'
            '"description":"context retrieval ranking","entry_tokens":3,'
            '"id":"m-foxtrot","max_overlap_idf":1.287682,"min_overlap_df":5,'
            '"overlap":["context","ranking","retrieval"],"overlap_idf":3.708896,'
            '"path":"/memory/m-foxtrot.md","score":0.7801,"source":"memory"},'
            '{"corpus_size":7,"description":"workspace context retrieval",'
            '"entry_tokens":3,"id":"m-charlie","max_overlap_idf":1.287682,'
            '"min_overlap_df":5,"overlap":["context","retrieval","workspace"],'
            '"overlap_idf":3.554745,"path":"/memory/m-charlie.md",'
            '"score":0.747677,"source":"memory"}]'
        )
        self.assertEqual(
            json.dumps(actual, sort_keys=True, separators=(",", ":")),
            expected_bytes,
        )


class BroadEntryScrutinyTest(unittest.TestCase):
    """Harness memory-selector discipline, ported lexically: breadth discounts
    the score and disqualifies weak evidence at the firing gate."""

    NARROW_DESC = "kelvium reactor calibration"
    BROAD_DESC = ("xanthogloss flanged manifold recalibration " +
                  " ".join(f"filler{i:02d}" for i in range(26)))

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="asha_broad_"))
        self.state = self.root / "state"
        self.log = self.root / "events.jsonl"
        self.index = self.root / "index.json"
        entries = [
            memory_retrieval.Entry(
                "narrow_case", self.NARROW_DESC, str(self.root / "narrow_case.md"),
                "memory", tuple(memory_retrieval.tokenize(self.NARROW_DESC))),
            memory_retrieval.Entry(
                "broad_case", self.BROAD_DESC, str(self.root / "broad_case.md"),
                "memory", tuple(memory_retrieval.tokenize(self.BROAD_DESC))),
        ]
        self.index.write_text(
            json.dumps({"version": 1, "entries": [e.json() for e in entries]}),
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _match(self, session, pattern):
        command = [sys.executable, str(TOOLS / "memory_nudge.py"),
                   "--index", str(self.index), "--state-dir", str(self.state),
                   "--log", str(self.log), "match"]
        payload = {"session_id": session, "tool_name": "Grep",
                   "tool_input": {"pattern": pattern}}
        return subprocess.run(command, input=json.dumps(payload), text=True,
                              capture_output=True, check=False)

    def test_breadth_discount_prefers_narrow_entry_on_equal_overlap(self):
        shared = "kelvium reactor calibration"
        narrow = memory_retrieval.Entry(
            "narrow", shared, "/m/narrow.md", "memory",
            tuple(memory_retrieval.tokenize(shared)))
        broad = memory_retrieval.Entry(
            "broad", shared + " " + " ".join(f"pad{i:02d}" for i in range(30)),
            "/m/broad.md", "memory",
            tuple(memory_retrieval.tokenize(
                shared + " " + " ".join(f"pad{i:02d}" for i in range(30)))))
        ranked = memory_retrieval.rank(shared, [narrow, broad], limit=2)
        self.assertEqual([row["id"] for row in ranked], ["narrow", "broad"])
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        self.assertGreaterEqual(ranked[1]["entry_tokens"],
                                memory_retrieval.BROAD_ENTRY_TOKENS)

    def test_single_rare_token_never_fires_a_broad_entry(self):
        result = self._match("one", "xanthogloss")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "lone rare token in a broad entry must not fire")

    def test_broad_entry_needs_three_agreeing_tokens(self):
        two = self._match("two", "xanthogloss flanged")
        self.assertEqual(two.stdout, "", "two overlaps insufficient for a broad entry")
        three = self._match("three", "xanthogloss flanged manifold")
        self.assertIn("broad_case", three.stdout, "three agreeing tokens fire")

    def test_narrow_entry_keeps_two_token_and_rare_single_paths(self):
        rare = self._match("rare", "kelvium")
        self.assertIn("narrow_case", rare.stdout, "rare single token still fires narrow entries")
        pair = self._match("pair", "reactor calibration")
        self.assertIn("narrow_case", pair.stdout, "two agreeing tokens still fire narrow entries")


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
        # ASHA_NUDGE=0 is the memory-lexical row's disable_env in the
        # declarative nudge registry (hooks/nudges/rules.json), evaluated by
        # nudge-engine.sh — the successor to the retired memory_nudge.sh.
        hook = REPO / "plugins" / "session" / "hooks" / "handlers" / "nudge-engine.sh"
        env = os.environ.copy()
        env.update({"ASHA_NUDGE": "0", "ASHA_NUDGE_INDEX": str(self.index),
                    "HOME": str(self.root)})
        result = subprocess.run(
            ["bash", str(hook), "PreToolUse"],
            input=json.dumps({"session_id": "off", "tool_name": "Grep",
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
