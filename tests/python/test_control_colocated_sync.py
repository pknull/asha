from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import (
    JjAdapter, JjError, RepositoryFacts, colocated_sync_remediation,
)


class ColocatedProbeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    @staticmethod
    def completed(argv, returncode=0, stdout=b"", stderr=b""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def test_working_copy_parent_is_one_strict_full_sha_and_uses_read_only_argv(self) -> None:
        commit = "a" * 40
        runner = mock.Mock(return_value=self.completed([], stdout=commit.encode()))

        observed = JjAdapter(runner=runner).working_copy_parent(self.root)

        self.assertEqual(observed, commit)
        self.assertEqual(runner.call_args.args[0], [
            "jj", "-R", str(self.root), "--ignore-working-copy", "log",
            "-r", "@-", "--no-graph", "-T", "commit_id",
        ])
        self.assertEqual(runner.call_args.kwargs["timeout"], 60)
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertFalse(runner.call_args.kwargs["check"])

        for output in (
            b"a" * 39, b"a" * 64, b"A" * 40,
            b"a" * 40 + b"\n" + b"b" * 40 + b"\n",
        ):
            with self.subTest(output=output), self.assertRaisesRegex(JjError, "working-copy parent"):
                JjAdapter(runner=lambda argv, **kwargs: self.completed(
                    argv, stdout=output,
                )).working_copy_parent(self.root)

    def test_git_head_is_strict_and_unborn_requires_positive_confirmation(self) -> None:
        commit = "b" * 40
        calls: list[list[str]] = []

        def born(argv, **kwargs):
            calls.append(list(argv))
            return self.completed(argv, stdout=(commit + "\n").encode())

        self.assertEqual(JjAdapter(runner=born).git_head(self.root), commit)
        self.assertEqual(calls, [[
            "git", "-C", str(self.root), "rev-parse", "--verify", "HEAD^{commit}",
        ]])

        calls.clear()

        def unborn(argv, **kwargs):
            calls.append(list(argv))
            if "rev-parse" in argv:
                return self.completed(argv, 128, stderr=b"needed a single revision\n")
            if "symbolic-ref" in argv:
                return self.completed(argv, stdout=b"refs/heads/main\n")
            if "show-ref" in argv:
                return self.completed(argv, 1)
            raise AssertionError(argv)

        self.assertIsNone(JjAdapter(runner=unborn).git_head(self.root))
        self.assertEqual(calls[1:], [
            ["git", "-C", str(self.root), "symbolic-ref", "--quiet", "HEAD"],
            [
                "git", "-C", str(self.root), "show-ref", "--verify", "--quiet",
                "refs/heads/main",
            ],
        ])

        for output in (b"b" * 39 + b"\n", b"B" * 40 + b"\n", b"b" * 64 + b"\n"):
            with self.subTest(output=output), self.assertRaisesRegex(JjError, "Git HEAD"):
                JjAdapter(runner=lambda argv, **kwargs: self.completed(
                    argv, stdout=output,
                )).git_head(self.root)

        def not_confirmed(argv, **kwargs):
            if "rev-parse" in argv:
                return self.completed(argv, 128)
            return self.completed(argv, 1)

        with self.assertRaisesRegex(JjError, "Git HEAD"):
            JjAdapter(runner=not_confirmed).git_head(self.root)

    def test_probe_output_is_byte_capped(self) -> None:
        oversized = lambda argv, **kwargs: self.completed(argv, stdout=b"x" * (64 * 1024 + 1))
        with self.assertRaisesRegex(JjError, "bounded"):
            JjAdapter(runner=oversized).working_copy_parent(self.root)
        with self.assertRaisesRegex(JjError, "bounded"):
            JjAdapter(runner=oversized).git_head(self.root)

    def test_git_import_uses_one_reviewed_argv_and_reports_the_existing_mutation(self) -> None:
        runner = mock.Mock(return_value=self.completed([]))

        report = JjAdapter(runner=runner).import_git(self.root)

        self.assertEqual(runner.call_args.args[0], [
            "jj", "-R", str(self.root), "--ignore-working-copy", "git", "import",
        ])
        self.assertEqual(report, ({
            "kind": "jj-operation",
            "detail": "recorded a jj operation-log entry for git import",
            "operation": "git import",
        },))

    def immutable_runner(self, tree: bytes, *, conflict: bytes = b"false\n"):
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            if argv[0] == "git":
                return self.completed(argv, stdout=tree)
            if argv[-1] == "root" and argv[-2] != "git":
                return self.completed(argv, stdout=(str(self.root) + "\n").encode())
            if argv[-2:] == ["git", "root"]:
                return self.completed(argv, stdout=(str(self.root) + "\n").encode())
            if "conflict" in argv[-1]:
                return self.completed(argv, stdout=conflict)
            raise AssertionError(argv)

        return runner, calls

    def test_immutable_tree_uses_one_git_tree_and_hashes_modes_and_blob_ids(self) -> None:
        commit = "a" * 40
        entries = [
            ["link", "120000", "b" * 40],
            ["regular", "100644", "c" * 40],
        ]
        raw = (
            f"120000 blob {'b' * 40}\tlink\0"
            f"100644 blob {'c' * 40}\tregular\0"
        ).encode()
        runner, calls = self.immutable_runner(raw)
        tree = JjAdapter(runner=runner).immutable_tree(self.root, commit)
        canonical = json.dumps(
            entries, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
        ).encode()
        self.assertEqual(tree.entries, tuple(tuple(item) for item in entries))
        self.assertEqual(tree.digest, hashlib.sha256(canonical).hexdigest())
        git_calls = [call for call in calls if call[0] == "git"]
        self.assertEqual(git_calls, [[
            "git", "-C", str(self.root), "ls-tree", "-rz", "--full-tree", commit,
        ]])
        self.assertFalse(any("file" in call and "show" in call for call in calls))

    def test_immutable_tree_refuses_conflicts_submodules_entry_and_byte_overflow(self) -> None:
        commit = "a" * 40
        conflicted, calls = self.immutable_runner(b"", conflict=b"true\n")
        with self.assertRaisesRegex(JjError, "conflicted"):
            JjAdapter(runner=conflicted).immutable_tree(self.root, commit)
        self.assertFalse(any(call[0] == "git" for call in calls))

        submodule, _ = self.immutable_runner(
            f"160000 commit {'d' * 40}\tsubmodule\0".encode()
        )
        with self.assertRaisesRegex(JjError, "submodules"):
            JjAdapter(runner=submodule).immutable_tree(self.root, commit)

        two_entries = (
            f"100644 blob {'b' * 40}\ta\0"
            f"100644 blob {'c' * 40}\tb\0"
        ).encode()
        oversized_count, _ = self.immutable_runner(two_entries)
        with mock.patch("lib.control.jj.MAX_IMMUTABLE_TREE_ENTRIES", 1), \
                self.assertRaisesRegex(JjError, "exceeds 1 entries"):
            JjAdapter(runner=oversized_count).immutable_tree(self.root, commit)

        oversized_bytes, _ = self.immutable_runner(two_entries)
        with mock.patch("lib.control.jj.MAX_IMMUTABLE_TREE_BYTES", 8), \
                self.assertRaisesRegex(JjError, "bounded"):
            JjAdapter(runner=oversized_bytes).immutable_tree(self.root, commit)


class RepositorySyncDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".asha").mkdir()
        (self.root / "Memory").mkdir()
        (self.root / ".asha" / "config.json").write_text("{}\n", encoding="utf-8")
        (self.root / "Memory" / "activeContext.md").write_text("# State\n", encoding="utf-8")

    def probe(self, git_head: str | None, jj_parent: str):
        root = self.root

        class FakeJj:
            def discover_root(self, start):
                return root

            def preflight(self, source):
                return RepositoryFacts(root=root, git_root=root)

            def working_copy_parent(self, source):
                return jj_parent

            def git_head(self, git_root):
                return git_head

        with contextlib.chdir(root), \
                mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                mock.patch("lib.control.doctor.JjAdapter", FakeJj):
            return run_doctor(
                None, probes={"repository": DEFAULT_PROBES["repository"]},
            )["probes"][0]

    def test_divergence_is_a_mismatch_with_task_start_remediation(self) -> None:
        git_head, jj_parent = "a" * 40, "b" * 40

        probe = self.probe(git_head, jj_parent)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertEqual(
            probe["detail"],
            f"source working copy is out of sync with jj: git HEAD {git_head} "
            f"but jj @- is {jj_parent}; run `jj status` in {self.root} to import it, then retry",
        )

    def test_equal_and_root_parent_unborn_git_heads_remain_matches(self) -> None:
        commit = "c" * 40
        self.assertEqual(self.probe(commit, commit)["outcome"], "match")
        self.assertEqual(self.probe(None, "0" * 40)["outcome"], "match")

    def test_established_repository_with_unborn_git_head_is_a_mismatch(self) -> None:
        jj_parent = "c" * 40

        probe = self.probe(None, jj_parent)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertEqual(
            probe["detail"],
            f"source working copy is out of sync with jj: git HEAD is unborn "
            f"but jj @- is {jj_parent}; run `jj status` in {self.root} "
            "to import it, then retry",
        )

    def test_remediation_sanitizes_controls_and_bounds_long_roots(self) -> None:
        root = Path("/tmp/" + "segment" * 100 + "\ncontrol")

        detail = colocated_sync_remediation(root, "a" * 40, "b" * 40)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertLessEqual(len(detail), 500)
        self.assertTrue(all(character.isprintable() for character in detail))
        self.assertNotIn("\n", detail)
        self.assertTrue(detail.endswith("to import it, then retry"))

    def test_doctor_returns_a_valid_probe_for_a_long_control_root(self) -> None:
        root = Path("/tmp/" + "segment" * 100 + "\ncontrol")

        class FakeJj:
            def discover_root(self, start):
                return root

            def preflight(self, source):
                return RepositoryFacts(root=root, git_root=root)

            def working_copy_parent(self, source):
                return "b" * 40

            def git_head(self, git_root):
                return "a" * 40

        with mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                mock.patch("lib.control.doctor.JjAdapter", FakeJj):
            probe = run_doctor(
                None, probes={"repository": DEFAULT_PROBES["repository"]},
            )["probes"][0]

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertLessEqual(len(probe["detail"]), 500)
        self.assertTrue(all(character.isprintable() for character in probe["detail"]))
        self.assertTrue(probe["detail"].endswith("to import it, then retry"))


class StartGuardOrderingTests(unittest.TestCase):
    def test_pr_fetch_is_rechecked_before_import_and_window_divergence_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            source.mkdir()
            home = root / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "ASHA_CONFIG": str(root / "missing.json"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
            events: list[str] = []

            class FakeJj:
                def preflight(self, requested):
                    return RepositoryFacts(root=source, git_root=source)

                def import_git(self, requested):
                    events.append("import")
                    raise AssertionError("import must not run after the second guard refuses")

            class FakeGithub:
                def preflight(self):
                    pass

                def pr_metadata(self, requested, number):
                    return {
                        "number": number,
                        "title": "PR title",
                        "url": "https://example.invalid/pull/7",
                        "headRefOid": "d" * 40,
                        "state": "OPEN",
                        "isDraft": False,
                        "isCrossRepository": False,
                    }

                def pr_remote(self, git_root, url, number):
                    return "origin"

                def fetch_pr_head(self, git_root, remote, number):
                    events.append("fetch")
                    return ({
                        "kind": "fetched-objects", "detail": "fetched",
                        "remote": remote, "source_ref": f"pull/{number}/head",
                    },)

            guard_calls = 0

            def guard(adapter, repository):
                nonlocal guard_calls
                guard_calls += 1
                events.append("guard")
                if guard_calls == 2:
                    raise ValueError("mutation-window divergence")

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("lib.control.cli._repo_argument", return_value=source), \
                    mock.patch("lib.control.cli.JjAdapter", return_value=FakeJj()), \
                    mock.patch("lib.control.cli.GithubAdapter", FakeGithub), \
                    mock.patch("lib.control.cli._guard_colocated_sync", side_effect=guard), \
                    mock.patch("lib.control.cli.shutil.which", return_value="/fake/codex"), \
                    mock.patch(
                        "lib.control.cli.prepare_task_workspace",
                        side_effect=AssertionError("prepare must not run"),
                    ), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = control_main([
                    "task", "start", "--repo", str(source), "--pr", "7",
                    "--harness", "codex", "--goal", "Close mutation window",
                    "--detach", "--json",
                ], env=env)

        self.assertEqual(status, 2)
        self.assertEqual(events, ["guard", "fetch", "guard"])
        self.assertNotIn("import", events)
        self.assertIn("mutation-window divergence", stderr.getvalue())


_REAL_TOOLS = all(shutil.which(name) for name in ("git", "jj"))


@unittest.skipUnless(_REAL_TOOLS, "git and jj are required for the colocated sync fixture")
class RealColocatedSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "JJ_CONFIG": str(self.root / "jj-config.toml"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.env = {
            **self.git_env,
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.git("init", "-q", "-b", "main")
        (self.source / ".gitignore").write_text("/.asha/\n/Memory/\n/Work/\n", encoding="utf-8")
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".gitignore", "tracked.txt")
        self.git("commit", "-qm", "base")
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.source)], check=True,
            capture_output=True, env=self.git_env, cwd=self.root,
        )
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "colocated-sync-fixture",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        self.source.chmod(0o755)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.source), *args], check=True,
            capture_output=True, text=True, env=self.git_env,
        ).stdout.strip()

    def jj(self, *args: str, ignore: bool = True) -> str:
        argv = ["jj", "-R", str(self.source)]
        if ignore:
            argv.append("--ignore-working-copy")
        return subprocess.run(
            [*argv, *args], check=True, capture_output=True, text=True,
            env=self.git_env, cwd=self.root,
        ).stdout.strip()

    def invoke(self) -> tuple[int, str, str, dict | None]:
        stdout, stderr = io.StringIO(), io.StringIO()
        captured: dict[str, object] = {}
        real_which = shutil.which

        def fake_launch(config, task, **kwargs):
            captured["task"] = task
            return {"task": task, "run": {}}

        with contextlib.chdir(self.root), \
                mock.patch.dict(os.environ, self.git_env, clear=False), \
                mock.patch(
                    "lib.control.cli.shutil.which",
                    side_effect=lambda name: "/fake/codex" if name == "codex" else real_which(name),
                ), \
                mock.patch("lib.control.cli.launch_task", side_effect=fake_launch), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "plain-git-branch",
                "--harness", "codex", "--goal", "Exercise colocated sync guard",
                "--detach", "--json",
            ], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue(), captured.get("task")

    def test_plain_git_divergence_refuses_unchanged_then_status_allows_one_import(self) -> None:
        stale_parent = self.jj("log", "-r", "@-", "--no-graph", "-T", "commit_id")
        self.git("checkout", "-qb", "plain-git-branch")
        (self.source / "plain-git.txt").write_text("committed outside jj\n", encoding="utf-8")
        self.git("add", "plain-git.txt")
        self.git("commit", "-qm", "plain Git commit")
        divergent_head = self.git("rev-parse", "HEAD")
        branch = self.git("symbolic-ref", "HEAD")
        self.assertNotEqual(divergent_head, stale_parent)

        status, stdout, stderr, task = self.invoke()

        expected = (
            f"source working copy is out of sync with jj: git HEAD {divergent_head} "
            f"but jj @- is {stale_parent}; run `jj status` in {self.source} "
            "to import it, then retry"
        )
        self.assertEqual((status, stdout, task), (2, "", None))
        self.assertEqual(stderr, f"asha control: {expected}\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), divergent_head)
        self.assertEqual(self.git("symbolic-ref", "HEAD"), branch)
        self.assertEqual(
            self.jj("log", "-r", "@-", "--no-graph", "-T", "commit_id"),
            stale_parent,
        )

        self.jj("status", ignore=False)
        synchronized_parent = self.jj(
            "log", "-r", "@-", "--no-graph", "-T", "commit_id",
        )
        self.assertEqual(synchronized_parent, divergent_head)

        status, stdout, stderr, task = self.invoke()

        self.assertEqual(status, 0, stderr)
        self.assertIsNotNone(task)
        self.assertEqual(json.loads(stdout)["source_mutations"], [{
            "kind": "jj-operation",
            "detail": "recorded a jj operation-log entry for git import",
            "operation": "git import",
        }])
        self.assertEqual(self.git("rev-parse", "HEAD"), divergent_head)
        self.assertEqual(self.git("symbolic-ref", "HEAD"), branch)

    def test_established_repository_switched_to_unborn_branch_refuses_without_movement(self) -> None:
        stale_parent = self.jj("log", "-r", "@-", "--no-graph", "-T", "commit_id")
        self.git("checkout", "-q", "--orphan", "plain-git-branch")
        branch = self.git("symbolic-ref", "HEAD")
        before_status = self.git("status", "--porcelain=v1")
        unborn = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False, capture_output=True, env=self.git_env,
        )
        self.assertNotEqual(unborn.returncode, 0)
        self.assertNotEqual(stale_parent, "0" * 40)

        status, stdout, stderr, task = self.invoke()

        expected = (
            "source working copy is out of sync with jj: git HEAD is unborn "
            f"but jj @- is {stale_parent}; run `jj status` in {self.source} "
            "to import it, then retry"
        )
        self.assertEqual((status, stdout, task), (2, "", None))
        self.assertEqual(stderr, f"asha control: {expected}\n")
        self.assertEqual(self.git("symbolic-ref", "HEAD"), branch)
        self.assertEqual(self.git("status", "--porcelain=v1"), before_status)
        self.assertNotEqual(subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "--verify", "HEAD^{commit}"],
            check=False, capture_output=True, env=self.git_env,
        ).returncode, 0)
        self.assertEqual(
            self.jj("log", "-r", "@-", "--no-graph", "-T", "commit_id"),
            stale_parent,
        )


if __name__ == "__main__":
    unittest.main()
