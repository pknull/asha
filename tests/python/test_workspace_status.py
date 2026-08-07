#!/usr/bin/env python3
"""
Tests for workspace_status (workspace v1, delivery issue 3 — issue #35).

The first consumer of the detection primitive: enriches detect_workspace()
with git state (active child repo, per-repo presence/branch/dirty,
shared_git_root state, manifest trackedness) and renders human + --json
views. Ratified convention (Keeper, 2026-08-08): the manifest is committed
in shared_git_root — an untracked manifest is a WARNING; an invalid one is
a typed error with guided repair, never auto-fix.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import workspace_status as ws  # type: ignore[reportMissingImports]  # noqa: E402


def _git(*args, cwd=None):
    subprocess.run(
        # init.defaultBranch pinned: the branch assertions must not depend on
        # the machine's git config (pass-2 portability finding).
        ["git", "-c", "init.defaultBranch=master", *args], cwd=cwd, check=True,
        capture_output=True, text=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class StatusFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_wss_")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._home = os.environ.get("HOME")
        home = self.tmp / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        self.addCleanup(self._restore_home)
        self.ws = home / "Code" / "thallus"
        self.ws.mkdir(parents=True)

    def _restore_home(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home

    def _manifest(self, data=None):
        (self.ws / ".asha").mkdir(exist_ok=True)
        payload = data if data is not None else {
            "version": 1, "workspace_name": "thallus",
            "repositories": [
                {"path": "egregore", "role": "svc"},
                {"path": "servitor", "role": "svc"},
            ],
        }
        (self.ws / ".asha" / "workspace.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _init_workspace_git(self, track_manifest=True):
        _git("init", "-q", str(self.ws))
        (self.ws / "README.md").write_text("x", encoding="utf-8")
        _git("add", "README.md", cwd=self.ws)
        if track_manifest and (self.ws / ".asha" / "workspace.json").exists():
            _git("add", ".asha/workspace.json", cwd=self.ws)
        _git("commit", "-qm", "init", cwd=self.ws)

    def _child(self, name, git=True):
        child = self.ws / name
        child.mkdir(exist_ok=True)
        if git:
            _git("init", "-q", str(child))
            (child / "f").write_text("x", encoding="utf-8")
            _git("add", "f", cwd=child)
            _git("commit", "-qm", "init", cwd=child)
        return child


class NoWorkspaceCases(StatusFixture):
    def test_no_workspace_is_ok_and_quiet(self):
        lone = self.tmp / "home" / "solo"
        lone.mkdir(parents=True)
        report = ws.build_status(start=lone)
        self.assertTrue(report["ok"])
        self.assertIsNone(report["workspace_root"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["warnings"], [])

    def test_cli_no_workspace_exit_zero(self):
        lone = self.tmp / "home" / "solo"
        lone.mkdir(parents=True)
        rc, out = _run_cli(["workspace_status.py", "--json",
                            "--start", str(lone)])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])


class ValidWorkspaceCases(StatusFixture):
    def setUp(self):
        super().setUp()
        self._manifest()
        self._init_workspace_git(track_manifest=True)
        self.egregore = self._child("egregore", git=True)
        # servitor is declared but ABSENT — must be reported, never assumed.

    def test_report_shape(self):
        report = ws.build_status(start=self.egregore)
        self.assertTrue(report["ok"])
        self.assertEqual(report["workspace_root"], str(self.ws))
        self.assertEqual(report["workspace_name"], "thallus")
        self.assertEqual(report["active_repository"], "egregore")
        sgr = report["shared_git_root"]
        self.assertEqual(sgr["path"], str(self.ws))
        self.assertTrue(sgr["is_git_worktree"])
        self.assertTrue(report["manifest_tracked"])

    def test_declared_repo_states(self):
        report = ws.build_status(start=self.egregore)
        repos = {r["path"]: r for r in report["repositories"]}
        self.assertTrue(repos["egregore"]["exists"])
        self.assertTrue(repos["egregore"]["is_git_worktree"])
        self.assertEqual(repos["egregore"]["branch"], "master")
        self.assertFalse(repos["egregore"]["dirty"])
        self.assertFalse(repos["servitor"]["exists"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("repo_missing", codes)

    def test_dirty_child_reported(self):
        (self.egregore / "new.txt").write_text("y", encoding="utf-8")
        report = ws.build_status(start=self.egregore)
        repos = {r["path"]: r for r in report["repositories"]}
        self.assertTrue(repos["egregore"]["dirty"])

    def test_active_repo_none_at_workspace_root(self):
        report = ws.build_status(start=self.ws)
        self.assertIsNone(report["active_repository"])

    def test_untracked_manifest_warns_per_convention(self):
        _git("rm", "-q", "--cached", ".asha/workspace.json", cwd=self.ws)
        report = ws.build_status(start=self.egregore)
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("manifest_untracked", codes)
        self.assertFalse(report["manifest_tracked"])
        self.assertTrue(report["ok"], "a warning must not flip ok")

    def test_cli_valid_exit_zero_json(self):
        rc, out = _run_cli(["workspace_status.py", "--json",
                            "--start", str(self.egregore)])
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertEqual(report["active_repository"], "egregore")

    def test_human_output_mentions_essentials(self):
        rc, out = _run_cli(["workspace_status.py",
                            "--start", str(self.egregore)])
        self.assertEqual(rc, 0)
        self.assertIn("thallus", out)
        self.assertIn("egregore", out)
        self.assertIn("servitor", out)
        # The proposal's status surface includes the memory roots (pass-2
        # blocking finding: they were omitted entirely).
        self.assertIn("Memory", out)
        self.assertIn("memory-local", out)
        self.assertIn("knowledge", out)

    def test_plain_subdirectory_is_not_a_repo(self):
        # Pass-2 BLOCKING: a declared repo that is a plain subdir inside the
        # PARENT repo must not inherit the parent's branch/dirty state —
        # is-inside-work-tree is true anywhere under the workspace repo.
        plain = self.ws / "plainchild"
        plain.mkdir()
        data = json.loads(
            (self.ws / ".asha" / "workspace.json").read_text(encoding="utf-8")
        )
        data["repositories"].append({"path": "plainchild"})
        self._manifest(data)
        report = ws.build_status(start=self.egregore)
        repos = {r["path"]: r for r in report["repositories"]}
        self.assertTrue(repos["plainchild"]["exists"])
        self.assertFalse(repos["plainchild"]["is_git_worktree"])
        self.assertIsNone(repos["plainchild"]["branch"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("repo_not_git", codes)

    def test_symlinked_repo_escaping_workspace_is_flagged_not_probed(self):
        # Pass-2 BLOCKING: a declared path that resolves outside the
        # workspace must not have git state read from the foreign target.
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        _git("init", "-q", str(outside))
        (self.ws / "escapee").symlink_to(outside)
        data = json.loads(
            (self.ws / ".asha" / "workspace.json").read_text(encoding="utf-8")
        )
        data["repositories"].append({"path": "escapee"})
        self._manifest(data)
        report = ws.build_status(start=self.egregore)
        repos = {r["path"]: r for r in report["repositories"]}
        self.assertFalse(repos["escapee"]["is_git_worktree"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("repo_escapes_workspace", codes)
        self.assertNotEqual(report["active_repository"], "escapee")

    def test_unborn_repo_still_reports_branch(self):
        unborn = self.ws / "fresh"
        unborn.mkdir()
        _git("init", "-q", str(unborn))
        data = json.loads(
            (self.ws / ".asha" / "workspace.json").read_text(encoding="utf-8")
        )
        data["repositories"].append({"path": "fresh"})
        self._manifest(data)
        report = ws.build_status(start=self.egregore)
        repos = {r["path"]: r for r in report["repositories"]}
        self.assertEqual(repos["fresh"]["branch"], "master")


class InvalidWorkspaceCases(StatusFixture):
    def test_invalid_manifest_typed_errors_and_repair(self):
        self._manifest({"version": 2, "workspace_name": "thallus"})
        child = self._child("egregore", git=False)
        report = ws.build_status(start=child)
        self.assertFalse(report["ok"])
        codes = {e["code"] for e in report["errors"]}
        self.assertIn("unsupported_version", codes)

    def test_cli_invalid_exit_one_with_repair_guidance(self):
        self._manifest({"version": 2, "workspace_name": "thallus"})
        child = self._child("egregore", git=False)
        rc, out = _run_cli(["workspace_status.py", "--start", str(child)])
        self.assertEqual(rc, 1)
        # Guided repair, never auto-fix (ratified convention).
        self.assertIn("repair", out.lower())
        self.assertIn("workspace.json", out)

    def test_manifest_outside_shared_git_root_warns(self):
        # Pass-2 BLOCKING: sgr="Memory" is a VALID layout (op == sgr), but
        # the workspace manifest then lies outside the sgr tree — the
        # committed-manifest convention cannot apply, and that must be a
        # visible warning, not a silent null.
        self._manifest({
            "version": 1, "workspace_name": "thallus",
            "memory": {"shared_git_root": "Memory",
                       "operational_root": "Memory"},
        })
        mem = self.ws / "Memory"
        mem.mkdir(exist_ok=True)
        _git("init", "-q", str(mem))
        (mem / "seed").write_text("x", encoding="utf-8")
        _git("add", "seed", cwd=mem)
        _git("commit", "-qm", "init", cwd=mem)
        child = self._child("egregore", git=False)
        report = ws.build_status(start=child)
        self.assertTrue(report["ok"])
        self.assertIsNone(report["manifest_tracked"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("manifest_outside_shared_git_root", codes)

    def test_detection_failure_renders_without_manifest_repair(self):
        # Pass-2: a bad --start is a DETECTION failure; telling the user to
        # repair a manifest that was never found is misdirection.
        rc, out = _run_cli(["workspace_status.py",
                            "--start", str(self.tmp / "nope")])
        self.assertEqual(rc, 1)
        self.assertNotIn("repair: edit", out)
        self.assertIn("detection", out.lower())

    def test_repair_guidance_uses_absolute_tool_path(self):
        self._manifest({"version": 2, "workspace_name": "thallus"})
        child = self._child("egregore", git=False)
        rc, out = _run_cli(["workspace_status.py", "--start", str(child)])
        self.assertEqual(rc, 1)
        self.assertIn(str(TOOLS_DIR / "workspace_manifest.py"), out)

    def test_cli_usage_errors(self):
        for argv in (["workspace_status.py", "--start="],
                     ["workspace_status.py", "--start", "--json"],
                     ["workspace_status.py", "--start"]):
            rc, _ = _run_cli(argv)
            self.assertEqual(rc, 2, argv)

    def test_shared_git_root_not_a_repo_warns(self):
        self._manifest()
        child = self._child("egregore", git=True)
        # workspace root deliberately NOT a git repo: sgr invalid for commits
        report = ws.build_status(start=child)
        self.assertTrue(report["ok"])
        self.assertFalse(report["shared_git_root"]["is_git_worktree"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("shared_git_root_not_git", codes)
        self.assertIsNone(report["manifest_tracked"])


def _run_cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ws.main(argv)
    return rc, out.getvalue()


if __name__ == "__main__":
    unittest.main()
