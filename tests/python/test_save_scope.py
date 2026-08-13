#!/usr/bin/env python3
"""Scope resolution for explicit Memory v2 publication."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import save_scope as ss  # type: ignore[reportMissingImports]  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(
        ["git", "-c", "init.defaultBranch=master", *args], cwd=cwd,
        check=True, capture_output=True, text=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class ScopeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_scope_")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._home = os.environ.get("HOME")
        home = self.tmp / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        self.addCleanup(self._restore_home)
        # Workspace: sgr = ws root, one child repo, workspace Memory plane.
        self.ws = home / "Code" / "thallus"
        (self.ws / ".asha").mkdir(parents=True)
        (self.ws / ".asha" / "workspace.json").write_text(json.dumps({
            "version": 1, "workspace_name": "thallus",
            "repositories": [{"path": "egregore"}],
        }), encoding="utf-8")
        (self.ws / "Memory").mkdir()
        (self.ws / "Memory" / "activeContext.md").write_text(
            "# ws context\n", encoding="utf-8")
        _git("init", "-q", str(self.ws))
        self.child = self.ws / "egregore"
        (self.child / "Memory").mkdir(parents=True)
        (self.child / "Memory" / "activeContext.md").write_text(
            "# child context\n", encoding="utf-8")
        _git("init", "-q", str(self.child))

    def _restore_home(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home


class ResolveCases(ScopeFixture):
    def test_workspace_scope_mapping(self):
        mapping, errors = ss.resolve_plane("workspace", start=self.child)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["plane_base"], str(self.ws))
        self.assertEqual(mapping["commit_repo"], str(self.ws))
        self.assertEqual(mapping["memory_root"], str(self.ws / "Memory"))
        self.assertEqual(mapping["scope"], "workspace")

    def test_repo_scope_maps_to_child(self):
        mapping, errors = ss.resolve_plane("repo", start=self.child)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["plane_base"], str(self.child))
        self.assertEqual(mapping["commit_repo"], str(self.child))
        self.assertEqual(mapping["memory_root"], str(self.child / "Memory"))

    def test_workspace_scope_without_manifest_is_typed_error(self):
        lone = self.tmp / "home" / "solo"
        lone.mkdir(parents=True)
        mapping, errors = ss.resolve_plane("workspace", start=lone)
        self.assertIsNone(mapping)
        self.assertEqual({e["code"] for e in errors}, {"no_workspace"})

    def test_workspace_scope_invalid_manifest_fails_closed(self):
        (self.ws / ".asha" / "workspace.json").write_text(
            '{"version": 9}', encoding="utf-8")
        mapping, errors = ss.resolve_plane("workspace", start=self.child)
        self.assertIsNone(mapping)
        self.assertTrue(errors)

    def test_workspace_scope_requires_sgr_worktree(self):
        # Same layout but the workspace root is not a git repo: commits are
        # impossible; resolution must fail closed, not stage into nothing.
        import shutil
        shutil.rmtree(self.ws / ".git")
        mapping, errors = ss.resolve_plane("workspace", start=self.child)
        self.assertIsNone(mapping)
        self.assertEqual({e["code"] for e in errors},
                         {"shared_git_root_not_git"})

    def test_repo_scope_at_workspace_root_is_error(self):
        # Pinned in the proposal: --scope repo from the workspace root fails
        # with guidance rather than guessing a child.
        mapping, errors = ss.resolve_plane("repo", start=self.ws)
        self.assertIsNone(mapping)
        self.assertEqual({e["code"] for e in errors}, {"no_active_repo"})


class CliCases(ScopeFixture):
    def _run(self, argv):
        import contextlib
        import io
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ss.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_resolve_cli_json(self):
        rc, out, _ = self._run(["save_scope.py", "resolve",
                                "--scope", "workspace",
                                "--start", str(self.child)])
        self.assertEqual(rc, 0)
        mapping = json.loads(out)
        self.assertEqual(mapping["plane_base"], str(self.ws))

    def test_resolve_cli_failure_typed(self):
        lone = self.tmp / "home" / "solo"
        lone.mkdir(parents=True)
        rc, out, _ = self._run(["save_scope.py", "resolve",
                                "--scope", "workspace",
                                "--start", str(lone)])
        self.assertEqual(rc, 1)
        verdict = json.loads(out)
        self.assertEqual(verdict["errors"][0]["code"], "no_workspace")

    def test_legacy_proof_verbs_are_rejected(self):
        rc, _, err = self._run(["save_scope.py", "write-proof",
                                "--scope", "workspace",
                                "--start", str(self.child)])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)

    def test_proof_api_is_removed(self):
        self.assertFalse(hasattr(ss, "write_proof"))
        self.assertFalse(hasattr(ss, "verify_proof"))

    def test_usage_error(self):
        rc, _, err = self._run(["save_scope.py", "bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err)


if __name__ == "__main__":
    unittest.main()
