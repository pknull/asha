#!/usr/bin/env python3
"""
Tests for project_root (workspace v1, delivery issue 2 — issue #33).

Two surfaces:
1. detect_project_root — the ONE shared Python resolver the historical
   detectors (pattern_analyzer, event_store, learnings_manager) now delegate
   to, parameterized so each keeps its exact historical layer set.
2. detect_workspace — the NEW workspace walk: upward from a start dir for
   .asha/workspace.json, stopping BEFORE $HOME and BEFORE the filesystem
   root (both exclusive, canonical comparison); a found manifest is parsed
   by workspace_manifest; an INVALID manifest is a typed verdict, never a
   silent keep-walking fallback. Nothing consumes the result yet — this
   increment ships the primitive only.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import project_root as pr  # type: ignore[reportMissingImports]  # noqa: E402


def _write_manifest(root: Path, data=None) -> None:
    (root / ".asha").mkdir(parents=True, exist_ok=True)
    payload = data if data is not None else {"version": 1, "workspace_name": "w"}
    (root / ".asha" / "workspace.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class DetectProjectRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_pr_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = self._env

    def test_env_layer_is_validated(self):
        # Python detectors validate CLAUDE_PROJECT_DIR against Memory/
        # (unlike the bash detectors) — historical, must stay.
        plain = self.tmp / "plain"
        plain.mkdir()
        os.environ["CLAUDE_PROJECT_DIR"] = str(plain)
        got = pr.detect_project_root(use_git=False, walk_base=None, on_fail="none")
        self.assertIsNone(got)

    def test_env_layer_accepts_project_with_memory(self):
        proj = self.tmp / "proj"
        (proj / "Memory").mkdir(parents=True)
        os.environ["CLAUDE_PROJECT_DIR"] = str(proj)
        got = pr.detect_project_root(use_git=False, walk_base=None, on_fail="none")
        self.assertEqual(got, proj)

    def test_argv_scan_validated_and_wins(self):
        proj = self.tmp / "proj"
        (proj / "Memory").mkdir(parents=True)
        other = self.tmp / "other"
        (other / "Memory").mkdir(parents=True)
        os.environ["CLAUDE_PROJECT_DIR"] = str(other)
        got = pr.detect_project_root(
            argv=["--project-dir", str(proj)],
            use_git=False, walk_base=None, on_fail="none",
        )
        self.assertEqual(got, proj.resolve())

    def test_argv_scan_equals_form(self):
        proj = self.tmp / "proj"
        (proj / "Memory").mkdir(parents=True)
        got = pr.detect_project_root(
            argv=[f"--project-dir={proj}"],
            use_git=False, walk_base=None, on_fail="none",
        )
        self.assertEqual(got, proj.resolve())

    def test_argv_invalid_falls_through(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        got = pr.detect_project_root(
            argv=["--project-dir", str(plain)],
            use_git=False, walk_base=None, on_fail="none",
        )
        self.assertIsNone(got)

    def test_walk_finds_memory_ancestor(self):
        proj = self.tmp / "proj"
        (proj / "Memory").mkdir(parents=True)
        sub = proj / "a" / "b"
        sub.mkdir(parents=True)
        got = pr.detect_project_root(
            use_env=False, use_git=False, walk_base=sub, on_fail="none"
        )
        self.assertEqual(got, sub.resolve().parents[1])

    def test_on_fail_raise_matches_historical_message(self):
        with self.assertRaises(RuntimeError) as ctx:
            pr.detect_project_root(
                use_env=False, use_git=False,
                walk_base=self.tmp / "nowhere-real",
                on_fail="raise",
            )
        self.assertIn("Cannot detect project root", str(ctx.exception))

    def test_on_fail_none_returns_none(self):
        got = pr.detect_project_root(
            use_env=False, use_git=False, walk_base=None, on_fail="none"
        )
        self.assertIsNone(got)


class DetectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_ws_")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        # Point $HOME inside the sandbox so home-exclusion is testable.
        self._home = os.environ.get("HOME")
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home

    def test_workspace_found_from_nested_cwd(self):
        ws = self.home / "Code" / "Thallus"
        child = ws / "egregore" / "src"
        child.mkdir(parents=True)
        _write_manifest(ws)
        det = pr.detect_workspace(start=child)
        self.assertEqual(det.root, ws)
        self.assertIsNotNone(det.manifest)
        self.assertEqual(det.errors, [])
        self.assertEqual(det.manifest["workspace_name"], "w")

    def test_no_manifest_anywhere_is_clean_none(self):
        lone = self.home / "Code" / "solo"
        lone.mkdir(parents=True)
        det = pr.detect_workspace(start=lone)
        self.assertIsNone(det.root)
        self.assertIsNone(det.manifest)
        self.assertEqual(det.errors, [])

    def test_home_is_excluded(self):
        # ~/.asha/workspace.json must NEVER make $HOME a workspace — the
        # user-scope config dir exists for every asha install.
        _write_manifest(self.home)
        start = self.home / "Code" / "proj"
        start.mkdir(parents=True)
        det = pr.detect_workspace(start=start)
        self.assertIsNone(det.root)
        self.assertEqual(det.errors, [])

    def test_start_at_home_itself_finds_nothing(self):
        _write_manifest(self.home)
        det = pr.detect_workspace(start=self.home)
        self.assertIsNone(det.root)

    def test_symlinked_home_compared_canonically(self):
        # Entering $HOME through a symlink spelling must not defeat the
        # exclusion (canonical comparison, per the ratified proposal).
        alias = self.tmp / "home-alias"
        alias.symlink_to(self.home)
        _write_manifest(self.home)
        start = alias / "Code" / "proj"
        (self.home / "Code" / "proj").mkdir(parents=True)
        det = pr.detect_workspace(start=start)
        self.assertIsNone(det.root)

    def test_invalid_manifest_is_typed_verdict_not_fallthrough(self):
        # Fail closed: a broken workspace surfaces as a verdict; the walk
        # must NOT keep climbing to a higher (valid) workspace.
        outer = self.home / "Code"
        inner = outer / "Thallus"
        child = inner / "egregore"
        child.mkdir(parents=True)
        _write_manifest(outer)                      # valid, above
        _write_manifest(inner, {"version": 2})      # invalid, nearer
        det = pr.detect_workspace(start=child)
        self.assertEqual(det.root, inner)
        self.assertIsNone(det.manifest)
        self.assertTrue(det.errors)
        codes = {e.code for e in det.errors}
        self.assertIn("unsupported_version", codes)

    def test_unreadable_manifest_is_typed(self):
        ws = self.home / "Code" / "ws"
        child = ws / "repo"
        child.mkdir(parents=True)
        _write_manifest(ws)
        path = ws / ".asha" / "workspace.json"
        path.chmod(0o000)
        self.addCleanup(lambda: path.chmod(0o644))
        det = pr.detect_workspace(start=child)
        self.assertEqual(det.root, ws)
        self.assertIsNone(det.manifest)
        self.assertEqual({e.code for e in det.errors}, {"unreadable"})

    def test_start_dir_itself_can_be_workspace_root(self):
        ws = self.home / "Code" / "ws"
        ws.mkdir(parents=True)
        _write_manifest(ws)
        det = pr.detect_workspace(start=ws)
        self.assertEqual(det.root, ws)
        self.assertIsNotNone(det.manifest)


class WorkspaceCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_wscli_")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home

    def _run(self, argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pr.main(argv)
        return rc, out.getvalue()

    def test_cli_reports_workspace(self):
        ws = self.tmp / "home" / "ws"
        child = ws / "repo"
        child.mkdir(parents=True)
        _write_manifest(ws)
        rc, out = self._run(["project_root.py", "workspace", "--start", str(child)])
        self.assertEqual(rc, 0)
        verdict = json.loads(out)
        self.assertEqual(verdict["workspace_root"], str(ws))
        self.assertTrue(verdict["ok"])

    def test_cli_reports_no_workspace(self):
        lone = self.tmp / "home" / "solo"
        lone.mkdir(parents=True)
        rc, out = self._run(["project_root.py", "workspace", "--start", str(lone)])
        self.assertEqual(rc, 0)
        verdict = json.loads(out)
        self.assertIsNone(verdict["workspace_root"])
        self.assertTrue(verdict["ok"])

    def test_cli_invalid_manifest_exits_one(self):
        ws = self.tmp / "home" / "ws"
        child = ws / "repo"
        child.mkdir(parents=True)
        _write_manifest(ws, {"version": 99})
        rc, out = self._run(["project_root.py", "workspace", "--start", str(child)])
        self.assertEqual(rc, 1)
        verdict = json.loads(out)
        self.assertEqual(verdict["workspace_root"], str(ws))
        self.assertFalse(verdict["ok"])
        self.assertTrue(verdict["errors"])


if __name__ == "__main__":
    unittest.main()
