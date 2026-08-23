"""The coordinator's project index: declared manifest first, bounded discovery otherwise."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from lib.control.orchestration import cli
from lib.control.orchestration.projects import PROJECT_LIST_CONTRACT, ProjectIndexError, list_projects
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
            "XDG_STATE_HOME": str(base / "state"), "XDG_DATA_HOME": str(base / "data"),
            "XDG_RUNTIME_DIR": str(base / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
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
        self.assertEqual(set(termart), {"name", "root", "project_id", "role", "declared", "asha_project", "jj_colocated"})
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
