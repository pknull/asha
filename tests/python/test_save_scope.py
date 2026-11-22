#!/usr/bin/env python3
"""Scope resolution for explicit Memory v2 publication."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import save_scope as ss  # type: ignore[reportMissingImports]  # noqa: E402
import memory_v2


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
        self.ws = home / "Code" / "foundry"
        (self.ws / ".asha").mkdir(parents=True)
        (self.ws / ".asha" / "workspace.json").write_text(json.dumps({
            "version": 1, "workspace_name": "foundry",
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
    def _managed_marker(self, workspace: Path) -> None:
        (workspace / ".asha").mkdir(exist_ok=True)
        marker = workspace / ".asha" / "control-task.json"
        marker.write_text(json.dumps({
            "contract": "asha.control-task-context.v1",
            "task_id": "11111111-1111-4111-8111-111111111111",
            "repository": {
                "root": str(self.child.resolve()),
                "identity": "repo:" + "a" * 64,
            },
            "jj": {
                "workspace_name": "asha-managed-11111111",
                "workspace_path": str(workspace.resolve()),
                "change_id": "k" * 32,
                "working_commit_id": "a" * 40,
            },
        }, sort_keys=True) + "\n", encoding="utf-8")
        marker.chmod(0o600)

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

    def test_explicit_repo_scope_does_not_discover_invalid_control_marker(self):
        marker = self.ws / ".asha" / "control-task.json"
        marker.write_text('{"contract":"wrong"}\n', encoding="utf-8")
        marker.chmod(0o600)
        with mock.patch.object(ss, "find_marker", wraps=ss.find_marker) as find_marker:
            mapping, errors = ss.resolve_effective_plane("repo", start=self.child)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "repo")
        self.assertEqual(mapping["plane_base"], str(self.child))
        find_marker.assert_not_called()

    def test_explicit_workspace_scope_does_not_discover_invalid_control_marker(self):
        marker = self.ws / ".asha" / "control-task.json"
        marker.write_text('{"contract":"wrong"}\n', encoding="utf-8")
        marker.chmod(0o600)
        with mock.patch.object(ss, "find_marker", wraps=ss.find_marker) as find_marker:
            mapping, errors = ss.resolve_effective_plane("workspace", start=self.child)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "workspace")
        self.assertEqual(mapping["plane_base"], str(self.ws))
        find_marker.assert_not_called()

    def test_bare_scope_in_managed_workspace_becomes_none_without_git(self):
        managed = self.tmp / "managed"
        (managed / "Memory").mkdir(parents=True)
        self._managed_marker(managed)
        with mock.patch.object(ss.subprocess, "run") as run:
            mapping, errors = ss.resolve_effective_plane(None, start=managed / "Memory")
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "none")
        self.assertEqual(mapping["plane_base"], str(managed))
        run.assert_not_called()

    def test_malformed_managed_marker_fails_closed_before_git(self):
        managed = self.tmp / "malformed"
        (managed / ".asha").mkdir(parents=True)
        marker = managed / ".asha" / "control-task.json"
        marker.write_text('{"contract":"wrong"}\n', encoding="utf-8")
        marker.chmod(0o600)
        with mock.patch.object(ss.subprocess, "run") as run:
            mapping, errors = ss.resolve_effective_plane(None, start=managed)
        self.assertIsNone(mapping)
        self.assertEqual(errors[0]["code"], "invalid_control_task_marker")
        run.assert_not_called()

    def test_explicit_none_with_malformed_marker_fails_closed_before_git(self):
        managed = self.tmp / "malformed-explicit-none"
        (managed / ".asha").mkdir(parents=True)
        marker = managed / ".asha" / "control-task.json"
        marker.write_text('{"contract":"wrong"}\n', encoding="utf-8")
        marker.chmod(0o600)
        with mock.patch.object(ss.subprocess, "run") as run:
            mapping, errors = ss.resolve_effective_plane("none", start=managed)
        self.assertIsNone(mapping)
        self.assertEqual(errors[0]["code"], "invalid_control_task_marker")
        run.assert_not_called()

    def test_explicit_none_outside_git_uses_initialized_project_without_git(self):
        project = self.tmp / "private-project"
        (project / ".asha").mkdir(parents=True)
        (project / ".asha" / "config.json").write_text(json.dumps({
            "memory_version": 2,
            "project_id": "project-1",
        }), encoding="utf-8")
        with mock.patch.object(ss.subprocess, "run") as run:
            mapping, errors = ss.resolve_effective_plane("none", start=project)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "none")
        self.assertEqual(mapping["plane_base"], str(project))
        run.assert_not_called()

    def test_explicit_none_rejects_oversized_config_without_git(self):
        project = self.tmp / "oversized-private-project"
        (project / ".asha").mkdir(parents=True)
        (project / ".asha" / "config.json").write_bytes(b"{" + b" " * (64 * 1024) + b"}")
        with mock.patch.object(ss.subprocess, "run") as run:
            mapping, errors = ss.resolve_effective_plane("none", start=project)
        self.assertIsNone(mapping)
        self.assertEqual(errors[0]["code"], "invalid_memory_config")
        run.assert_not_called()


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


class ChairSaveCases(ScopeFixture):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"ASHA_HOME": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.chair = Path(os.environ["HOME"]) / ".asha" / "chair"
        self.chair.mkdir(parents=True)
        memory_v2.initialize(self.chair)

    # Removing automatic chair recognition must break the actual command and
    # publisher, not merely an internal branch or a mocked collaborator.
    def test_executable_initialized_chair_bare_save_never_invokes_git(self):
        fake_bin = self.tmp / "chair-bin"
        fake_bin.mkdir()
        invoked = self.tmp / "chair-git-invoked"
        git = fake_bin / "git"
        git.write_text(f"#!/bin/sh\nprintf invoked >> {invoked}\nexit 99\n")
        git.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
               "ASHA_SESSION_ID": "fixture-chair-save"}
        result = subprocess.run([
            sys.executable, str(TOOLS_DIR / "save_scope.py"), "resolve",
            "--start", str(self.chair),
        ], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        mapping = json.loads(result.stdout)
        self.assertEqual(mapping["scope"], "none")
        self.assertEqual(mapping["plane_base"], str(self.chair))
        self.assertIsNone(mapping["commit_repo"])
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.chair))
        active = self.tmp / "chair-active.md"
        decisions = self.tmp / "chair-decisions.md"
        active.write_text("# Objective\n\nFixture\n\n# State\n\nSaved.\n\n# Next\n\n- None.\n\n# Blockers\n\n- None.\n")
        decisions.write_text("# Decisions\n\n- Preserve fixture state.\n")
        result = subprocess.run([
            sys.executable, str(TOOLS_DIR / "save_none.py"), "publish",
            "--start", str(self.chair), "--active-file", str(active),
            "--decisions-file", str(decisions),
            "--expected-active", before["active"],
            "--expected-decisions", before["decisions"],
        ], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["scope"], "none")
        self.assertEqual(receipt["plane_base"], str(self.chair))
        self.assertFalse(receipt["git_invoked"])
        self.assertEqual((self.chair / "Memory/activeContext.md").read_text(), active.read_text())
        self.assertEqual((self.chair / "Memory/decisions.md").read_text(), decisions.read_text())
        self.assertEqual(receipt["current"], memory_v2.snapshot_digests(
            memory_v2.read_published_snapshot(self.chair)))
        self.assertFalse(invoked.exists())

    # Ignoring ASHA_HOME or comparing lexical paths instead of the real chair
    # would make a supported relocated or linked seat resolve as a repository.
    def test_bare_chair_uses_custom_asha_home_and_canonical_path(self):
        custom = self.tmp / "custom"
        custom.mkdir()
        (custom / "chair").symlink_to(self.chair, target_is_directory=True)
        with mock.patch.dict(os.environ, {"ASHA_HOME": str(custom)}), \
                mock.patch.object(ss.subprocess, "run", side_effect=AssertionError("Git invoked")):
            mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "none")
        self.assertEqual(mapping["plane_base"], str(self.chair))

    # A broad initialized-directory fallback would swallow unrelated projects
    # or a distinct nested project despite the launcher's exact chair seat.
    def test_bare_non_chair_directories_are_not_inferred_from_initialization(self):
        for path in (self.tmp / "other", self.chair / "nested"):
            with self.subTest(path=path):
                path.mkdir()
                memory_v2.initialize(path)
                with mock.patch.dict(os.environ, {"ASHA_SESSION_PROFILE": "chair", "ASHA_SEAT": "1"}):
                    mapping, errors = ss.resolve_effective_plane(None, start=path)
                self.assertIsNone(mapping)
                self.assertEqual(errors[0]["code"], "no_repo")
        mapping, errors = ss.resolve_effective_plane("none", start=self.chair / "nested")
        self.assertEqual(errors, [])
        self.assertEqual(mapping["plane_base"], str(self.chair / "nested"))

    # Treating the seat path itself as initialization would admit invalid
    # config or publish into an initialized ancestor of an uninitialized chair.
    def test_bare_chair_invalid_config_fails_closed_without_git(self):
        config = self.chair / ".asha/config.json"
        for data in ("{", "[]", '{"memory_version":1,"project_id":"fixture"}',
                     '{"memory_version":2,"project_id":""}'):
            with self.subTest(data=data):
                config.write_text(data)
                with mock.patch.object(ss.subprocess, "run", side_effect=AssertionError("Git invoked")):
                    mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
                self.assertIsNone(mapping)
                self.assertEqual(errors[0]["code"], "invalid_memory_config")
        config.unlink()
        memory_v2.initialize(self.chair.parent)
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertIsNone(mapping)
        self.assertEqual(errors[0]["code"], "no_memory_project")

    # Chair inference before marker validation could bypass a malformed
    # Control marker or discard valid managed ownership.
    def test_bare_chair_preserves_control_marker_precedence(self):
        ResolveCases._managed_marker(self, self.chair)
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertIsNotNone(mapping["managed_task"])
        (self.chair / ".asha/control-task.json").write_text("{}")
        for scope in (None, "none"):
            with self.subTest(scope=scope):
                mapping, errors = ss.resolve_effective_plane(scope, start=self.chair)
                self.assertIsNone(mapping)
                self.assertEqual(errors[0]["code"], "invalid_control_task_marker")

    # Automatically selecting none for every chair would change existing
    # repository saves, including a chair nested within a Git worktree.
    def test_bare_git_backed_chair_keeps_repository_scope(self):
        _git("init", "-q", str(self.chair))
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "repo")
        self.assertEqual(mapping["commit_repo"], str(self.chair))
        with mock.patch.object(ss.subprocess, "run", side_effect=AssertionError("Git invoked")):
            mapping, errors = ss.resolve_effective_plane("none", start=self.chair)
        self.assertEqual(errors, [])
        self.assertIsNone(mapping["commit_repo"])

    def test_bare_chair_inside_ancestor_repo_keeps_repository_scope(self):
        _git("init", "-q", str(self.chair.parent))
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "repo")
        self.assertEqual(mapping["commit_repo"], str(self.chair.parent))

    # Ignoring a gitdir file or explicit Git environment would wrongly turn
    # these ordinary worktree saves into Git-free chair saves.
    def test_bare_chair_with_separate_git_dir_keeps_repository_scope(self):
        _git("init", "-q", "--separate-git-dir", str(self.tmp / "gitdir"), str(self.chair))
        self.assertTrue((self.chair / ".git").is_file())
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "repo")
        self.assertEqual(mapping["commit_repo"], str(self.chair))

    def test_bare_chair_with_explicit_git_environment_keeps_repository_scope(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.child / ".git"),
                                          "GIT_WORK_TREE": str(self.chair)}):
            mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "repo")
        self.assertEqual(mapping["commit_repo"], str(self.chair))

    # Changing the default for arbitrary initialized locations would also
    # disturb the existing bare repo and workspace-child routes.
    def test_bare_repository_and_workspace_child_keep_existing_scope(self):
        lone = self.tmp / "ordinary"
        lone.mkdir()
        _git("init", "-q", str(lone))
        for path in (lone, self.child):
            with self.subTest(path=path):
                mapping, errors = ss.resolve_effective_plane(None, start=path)
                self.assertEqual(errors, [])
                self.assertEqual(mapping["scope"], "repo")
                self.assertEqual(mapping["commit_repo"], str(path))

    # An empty directory named .git is not a Git repository. Treating it as
    # one prevents an otherwise initialized non-Git chair from saving.
    def test_bare_chair_ignores_empty_ancestor_git_directory(self):
        (self.chair.parent / ".git").mkdir()
        with mock.patch.object(ss.subprocess, "run", side_effect=AssertionError("Git invoked")):
            mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertEqual(errors, [])
        self.assertEqual(mapping["scope"], "none")
        self.assertEqual(mapping["plane_base"], str(self.chair))

    # Skipping the workspace resolver at the chair would bypass its root
    # ambiguity and malformed-manifest failures.
    def test_bare_chair_preserves_workspace_boundary(self):
        path = self.chair / ".asha/workspace.json"
        path.write_text(json.dumps({"version": 1, "workspace_name": "fixture",
                                    "repositories": [{"path": "child"}]}))
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertIsNone(mapping)
        self.assertEqual(errors[0]["code"], "no_active_repo")
        path.write_text("{")
        mapping, errors = ss.resolve_effective_plane(None, start=self.chair)
        self.assertIsNone(mapping)
        self.assertTrue(errors)


class ManagedBareSaveAcceptance(ScopeFixture):
    def test_executable_managed_bare_save_path_never_invokes_git(self):
        managed = self.tmp / "managed-save"
        (managed / ".asha").mkdir(parents=True)
        (managed / "Memory").mkdir()
        (managed / "Work" / "session-state").mkdir(parents=True)
        (managed / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "managed-save",
        }) + "\n", encoding="utf-8")
        marker = {
            "contract": "asha.control-task-context.v1",
            "task_id": "11111111-1111-4111-8111-111111111111",
            "repository": {"root": str(self.child), "identity": "repo:" + "a" * 64},
            "jj": {
                "workspace_name": "asha-managed-11111111",
                "workspace_path": str(managed), "change_id": "k" * 32,
                "working_commit_id": "a" * 40,
            },
        }
        marker_path = managed / ".asha" / "control-task.json"
        marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
        marker_path.chmod(0o600)
        old_active = "# Objective\n\nOld\n\n# State\n\nOld\n\n# Next\n\n- Old\n\n# Blockers\n\n- None.\n"
        old_decisions = "# Decisions\n\n- Old.\n"
        (managed / "Memory" / "activeContext.md").write_text(old_active, encoding="utf-8")
        (managed / "Memory" / "decisions.md").write_text(old_decisions, encoding="utf-8")
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(managed))
        active = self.tmp / "active.md"
        decisions = self.tmp / "decisions.md"
        active.write_text(old_active.replace("Old", "New"), encoding="utf-8")
        decisions.write_text("# Decisions\n\n- New.\n", encoding="utf-8")
        fake_bin = self.tmp / "fake-bin"
        fake_bin.mkdir()
        invoked = self.tmp / "git-invoked"
        git = fake_bin / "git"
        git.write_text(f"#!/bin/sh\nprintf invoked >> {invoked}\nexit 99\n", encoding="utf-8")
        git.chmod(0o755)
        command = TOOLS_DIR / "save_none.py"
        result = subprocess.run([
            sys.executable, str(command), "publish", "--start", str(managed),
            "--active-file", str(active), "--decisions-file", str(decisions),
            "--expected-active", before['active'], "--expected-decisions", before['decisions'],
        ], capture_output=True, text=True, env={
            **os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASHA_SESSION_ID": "session-managed-save",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["scope"], "none")
        self.assertEqual(payload["changed"], ["Memory/activeContext.md", "Memory/decisions.md"])
        self.assertFalse(invoked.exists())

    def test_executable_explicit_none_without_marker_never_invokes_git(self):
        project = self.tmp / "explicit-none-save"
        (project / ".asha").mkdir(parents=True)
        (project / "Memory").mkdir()
        (project / "Work" / "session-state").mkdir(parents=True)
        (project / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "explicit-none-save",
        }) + "\n", encoding="utf-8")
        old_active = "# Objective\n\nOld\n\n# State\n\nOld\n\n# Next\n\n- Old\n\n# Blockers\n\n- None.\n"
        (project / "Memory" / "activeContext.md").write_text(old_active, encoding="utf-8")
        (project / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- Old.\n", encoding="utf-8",
        )
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(project))
        active = self.tmp / "explicit-active.md"
        decisions = self.tmp / "explicit-decisions.md"
        active.write_text(old_active.replace("Old", "New"), encoding="utf-8")
        decisions.write_text("# Decisions\n\n- New.\n", encoding="utf-8")
        fake_bin = self.tmp / "explicit-fake-bin"
        fake_bin.mkdir()
        invoked = self.tmp / "explicit-git-invoked"
        git = fake_bin / "git"
        git.write_text(f"#!/bin/sh\nprintf invoked >> {invoked}\nexit 99\n", encoding="utf-8")
        git.chmod(0o755)

        result = subprocess.run([
            sys.executable, str(TOOLS_DIR / "save_none.py"), "publish",
            "--scope", "none", "--start", str(project),
            "--active-file", str(active), "--decisions-file", str(decisions),
            "--expected-active", before['active'], "--expected-decisions", before['decisions'],
        ], capture_output=True, text=True, env={
            **os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "ASHA_SESSION_ID": "session-explicit-none",
        })

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["scope"], "none")
        self.assertEqual(payload["plane_base"], str(project))
        self.assertFalse(invoked.exists())

    def test_missing_save_identity_is_nonfatal_after_managed_publication(self):
        managed = self.tmp / "managed-no-identity"
        (managed / ".asha").mkdir(parents=True)
        (managed / "Memory").mkdir()
        (managed / "Work" / "session-state").mkdir(parents=True)
        (managed / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "managed-no-identity",
        }) + "\n", encoding="utf-8")
        ResolveCases._managed_marker(self, managed)
        active = self.tmp / "no-identity-active.md"
        decisions = self.tmp / "no-identity-decisions.md"
        (managed / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (managed / "Memory" / "decisions.md").write_text("# Decisions\n\n- Old.\n", encoding="utf-8")
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(managed))
        active.write_text((managed / "Memory" / "activeContext.md").read_text(), encoding="utf-8")
        decisions.write_text("# Decisions\n\n- D.\n", encoding="utf-8")
        env = {
            key: value for key, value in os.environ.items()
            if key not in {"ASHA_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"}
        }

        result = subprocess.run([
            sys.executable, str(TOOLS_DIR / "save_none.py"), "publish",
            "--start", str(managed), "--active-file", str(active),
            "--decisions-file", str(decisions),
            "--expected-active", before['active'], "--expected-decisions", before['decisions'],
        ], capture_output=True, text=True, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsNone(payload["session_id"])
        self.assertEqual(payload["identity_status"], "skipped")
        self.assertEqual(
            (managed / "Memory" / "decisions.md").read_text(encoding="utf-8"),
            decisions.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
