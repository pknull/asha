"""The coordinator's project index: declared manifest first, bounded discovery otherwise."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lib.control.orchestration import cli
from lib.control.orchestration.projects import (
    PROJECT_LIST_CONTRACT, ProjectIndexError, configured_roots, display_name,
    list_projects, list_projects_across, resolve_roots,
)
from tests.python.orchestration_workspace_fixtures import write_manifest, write_member


class ProjectIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base / "Code"
        self.root.mkdir()
        self.env = {
            "HOME": str(base / "home"), "ASHA_CONFIG": str(base / "missing.json"),
            "ASHA_HOME": str(base / "asha"),
            "XDG_RUNTIME_DIR": str(base / "runtime"),
        }
        for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        write_member(self.root / "termart", "termart-project")
        write_member(self.root / "asha", "asha-project")
        (self.root / "termart" / ".jj").mkdir()
        (self.root / "notes").mkdir()                      # plain directory
        (self.root / ".hidden" / ".asha").mkdir(parents=True)
        (self.root / "loose.txt").write_text("x")
        nested = self.root / "group" / "deep"
        write_member(nested, "deep-project")

    def payload(self, *args: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["initiative", "projects", "--root", str(self.root), *args, "--json"], env=self.env)
        self.assertEqual(rc, 0)
        return json.loads(out.getvalue())

    def test_discovery_lists_asha_projects_one_level_down_in_name_order(self) -> None:
        payload = list_projects(self.root)
        self.assertEqual(payload["contract"], PROJECT_LIST_CONTRACT)
        self.assertEqual(payload["source"], "discovery")
        self.assertEqual(payload["root"], str(self.root))
        self.assertIsNone(payload["match"])
        self.assertEqual([item["name"] for item in payload["projects"]], ["asha", "termart"])
        termart = payload["projects"][1]
        # `directory` is an additive label under v1, following the
        # `repository_id` precedent on storage `workspaces[]` entries: the
        # friendly `name` may be the project's own, so the directory it lives
        # in stays available for anyone who needs the path.
        self.assertEqual(set(termart), {
            "name", "directory", "root", "project_id", "role", "declared",
            "asha_project", "jj_colocated",
        })
        self.assertEqual(termart["project_id"], "termart-project")
        self.assertTrue(termart["jj_colocated"])
        self.assertFalse(payload["projects"][0]["jj_colocated"])
        self.assertFalse(termart["declared"])
        self.assertIsNone(termart["role"])

    def test_depth_reaches_nested_projects_and_is_bounded(self) -> None:
        self.assertEqual([item["name"] for item in list_projects(self.root, depth=2)["projects"]], ["asha", "deep", "termart"])
        with self.assertRaisesRegex(ProjectIndexError, "depth must be between 1 and 3"):
            list_projects(self.root, depth=4)
        with self.assertRaisesRegex(ProjectIndexError, "not a directory"):
            list_projects(self.root / "missing")

    def test_start_inside_a_project_lists_that_project_first(self) -> None:
        payload = list_projects(self.root / "termart")
        self.assertEqual([item["name"] for item in payload["projects"]], ["termart"])

    def test_match_is_exact_on_name_or_project_id_case_insensitively(self) -> None:
        self.assertEqual([item["name"] for item in list_projects(self.root, match="TermArt")["projects"]], ["termart"])
        self.assertEqual([item["name"] for item in list_projects(self.root, match="asha-project")["projects"]], ["asha"])
        self.assertEqual(list_projects(self.root, match="term")["projects"], [])

    def test_declared_manifest_wins_over_discovery_and_keeps_order_and_roles(self) -> None:
        write_manifest(self.root, ("termart", "asha"))
        manifest = json.loads((self.root / ".asha/workspace.json").read_text())
        manifest["repositories"][1]["role"] = "toolkit"
        (self.root / ".asha/workspace.json").write_text(json.dumps(manifest))
        payload = list_projects(self.root)
        self.assertEqual(payload["source"], "manifest")
        self.assertEqual([item["name"] for item in payload["projects"]], ["termart", "asha"])
        self.assertTrue(all(item["declared"] for item in payload["projects"]))
        self.assertEqual(payload["projects"][1]["role"], "toolkit")
        # A member below the manifest resolves to the same declared index.
        below = list_projects(self.root / "asha")
        self.assertEqual(below["source"], "manifest")
        self.assertEqual(below["root"], str(self.root))

    def test_invalid_manifest_is_a_typed_refusal(self) -> None:
        (self.root / ".asha").mkdir()
        (self.root / ".asha/workspace.json").write_text("{not json")
        with self.assertRaisesRegex(ProjectIndexError, "workspace"):
            list_projects(self.root)

    def test_unpublished_project_falls_back_to_its_own_config_project_id(self) -> None:
        bare = self.root / "bare"
        (bare / ".asha").mkdir(parents=True)
        (bare / "Memory").mkdir()
        (bare / ".asha/config.json").write_text(json.dumps({"initialized": True, "memory_version": 2, "project_id": "bare-id"}))
        entry = next(item for item in list_projects(self.root)["projects"] if item["name"] == "bare")
        self.assertEqual(entry["project_id"], "bare-id")
        self.assertTrue(entry["asha_project"])

    def test_cli_emits_the_closed_payload_and_refuses_bad_options(self) -> None:
        payload = self.payload("--match", "termart")
        self.assertEqual(payload["contract"], PROJECT_LIST_CONTRACT)
        self.assertEqual([item["name"] for item in payload["projects"]], ["termart"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["initiative", "projects", "--root", str(self.root), "--depth", "x", "--json"], env=self.env)
        self.assertEqual(rc, 2)
        self.assertIn("--depth must be an integer", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class FriendlyNameTests(unittest.TestCase):
    """A project may state its own name; the directory is the fallback."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        write_member(self.base / "aas", "aas-project")

    def named(self, value) -> str:
        path = self.base / "aas" / ".asha" / "config.json"
        config = json.loads(path.read_text())
        config["name"] = value
        path.write_text(json.dumps(config))
        return display_name(self.base / "aas", "aas")

    def test_a_stated_name_is_used_and_the_directory_is_the_fallback(self) -> None:
        self.assertEqual(display_name(self.base / "aas", "aas"), "aas")
        self.assertEqual(self.named("Ashes and Starlight"), "Ashes and Starlight")
        self.assertEqual(self.named("  spaced   out  "), "spaced out")

    def test_an_unusable_name_falls_back_rather_than_reaching_the_terminal(self) -> None:
        # Rendered straight into a terminal row, so it is bounded and printable.
        for value in (None, 42, "", "   ", "x" * 49, "bad\u0007bell", "re\u202everse"):
            self.assertEqual(self.named(value), "aas", repr(value))
        self.assertEqual(display_name(self.base / "missing", "fallback"), "fallback")

    def test_the_index_carries_both_the_name_and_its_directory(self) -> None:
        self.named("Ashes and Starlight")
        entry = list_projects(self.base)["projects"][0]
        self.assertEqual(entry["name"], "Ashes and Starlight")
        self.assertEqual(entry["directory"], "aas")
        # Exact match stays exact (the index resolves an intent to one repo),
        # but now answers to the stated name as well as the directory.
        self.assertEqual(len(list_projects(self.base, match="Ashes and Starlight")["projects"]), 1)
        self.assertEqual(len(list_projects(self.base, match="aas")["projects"]), 1)
        self.assertEqual(list_projects(self.base, match="ashes")["projects"], [])


class RootResolutionTests(unittest.TestCase):
    """Explicit beats ambient, per-invocation beats persistent."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        (self.home / ".asha").mkdir(parents=True)

    def config(self, value) -> dict:
        path = self.home / ".asha" / "config.json"
        path.write_text(json.dumps({"default_harness": "claude", "project_roots": value}))
        return {"HOME": str(self.home)}

    def test_precedence_is_argument_then_environment_then_config_then_cwd(self) -> None:
        env = self.config(["/one", "/two"])
        self.assertEqual(resolve_roots(["/explicit"], env=env), (["/explicit"], "argument"))
        env_with_ambient = dict(env, ASHA_PROJECTS_ROOT="/ambient")
        self.assertEqual(resolve_roots(None, env=env_with_ambient), (["/ambient"], "environment"))
        self.assertEqual(resolve_roots(None, env=env), (["/one", "/two"], "configuration"))
        bare = {"HOME": str(self.base / "nowhere")}
        self.assertEqual(resolve_roots(None, env=bare, cwd=Path("/here")), (["/here"], "cwd"))
        # An explicit argument outranks an ambient variable.
        self.assertEqual(resolve_roots(["/explicit"], env=env_with_ambient)[1], "argument")

    def test_unusable_configuration_falls_back_instead_of_failing(self) -> None:
        for value in ("not a list", {}, [1, 2], ["", "   "], None):
            env = self.config(value)
            self.assertEqual(configured_roots(env), [] if value != ["", "   "] else [])
        (self.home / ".asha" / "config.json").write_text("{not json")
        self.assertEqual(configured_roots({"HOME": str(self.home)}), [])
        (self.home / ".asha" / "config.json").unlink()
        self.assertEqual(configured_roots({"HOME": str(self.home)}), [])

    def test_the_root_list_is_bounded(self) -> None:
        env = self.config([f"/root-{index}" for index in range(30)])
        self.assertEqual(len(configured_roots(env)), 8)


class MultiRootIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.code = self.base / "Code"
        self.notes = self.base / "Obsidian"
        for root in (self.code, self.notes):
            root.mkdir()
        write_member(self.code / "termart", "termart-project")
        (self.code / "termart" / ".jj").mkdir()
        write_member(self.notes / "aas", "aas-project")

    def test_each_root_keeps_its_own_group_and_the_totals_are_flat(self) -> None:
        payload = list_projects_across([str(self.code), str(self.notes)])
        self.assertEqual([Path(group["root"]).name for group in payload["groups"]], ["Code", "Obsidian"])
        self.assertEqual(len(payload["projects"]), 2)
        self.assertEqual(payload["contract"], PROJECT_LIST_CONTRACT)
        self.assertEqual(payload["skipped"], [])
        self.assertIsNone(payload["root"], "no single root when several were indexed")

    def test_a_project_reachable_from_two_roots_is_listed_once(self) -> None:
        payload = list_projects_across([str(self.code), str(self.code)])
        self.assertEqual(len(payload["projects"]), 1)

    def test_one_unusable_root_is_skipped_not_fatal(self) -> None:
        payload = list_projects_across([str(self.code), str(self.base / "gone")])
        self.assertEqual(len(payload["projects"]), 1, "the good root still lists")
        self.assertEqual(len(payload["skipped"]), 1)
        self.assertIn("gone", payload["skipped"][0]["root"])

    def test_the_payload_says_where_its_roots_came_from(self) -> None:
        payload = list_projects_across([str(self.code)], source_of_roots="configuration")
        self.assertEqual(payload["roots_from"], "configuration")
