from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.cli import _parse_start, main as control_main
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import JjAdapter
from lib.control.launch import launch_task
from lib.control.prepare import PrepareRequest, prepare_task_workspace
from lib.control.events import read_snapshot
from lib.control.reconcile import LiveAdapters
from lib.control.sources import GithubAdapter, SourceError
from lib.control.store import TaskStore
from tests.python.test_control_increment3 import FakeTmux


class GithubAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()

    @staticmethod
    def completed(argv, returncode=0, stdout=b"", stderr=b""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def test_preflight_distinguishes_absent_from_unauthenticated(self) -> None:
        with mock.patch("lib.control.sources.shutil.which", return_value=None):
            with self.assertRaisesRegex(SourceError, "not installed|not found"):
                GithubAdapter().preflight()

        runner = mock.Mock(side_effect=lambda argv, **kwargs: self.completed(
            argv, 1, stderr=b"not logged in\n",
        ))
        with mock.patch("lib.control.sources.shutil.which", return_value="/fake/gh"):
            with self.assertRaisesRegex(SourceError, "not authenticated"):
                GithubAdapter(runner=runner).preflight()
        self.assertEqual(runner.call_args.args[0], ["gh", "auth", "status"])
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_metadata_requests_only_bounded_fields_and_validates_identity(self) -> None:
        commit = "a" * 40
        outputs = {
            "pr": {
                "number": 34,
                "title": "Safe display title",
                "url": "https://github.example/repo/pull/34",
                "headRefOid": commit,
                "state": "OPEN",
                "isDraft": False,
                "isCrossRepository": True,
            },
            "issue": {
                "number": 51,
                "title": "Issue display title",
                "url": "https://github.example/repo/issues/51",
                "state": "OPEN",
            },
        }
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return self.completed(argv, stdout=json.dumps(outputs[argv[1]]).encode())

        adapter = GithubAdapter(runner=runner)
        self.assertEqual(adapter.pr_metadata(self.source, 34)["headRefOid"], commit)
        self.assertEqual(adapter.issue_metadata(self.source, 51)["number"], 51)
        self.assertEqual(calls, [
            [
                "gh", "pr", "view", "34", "--json",
                "number,title,url,headRefOid,state,isDraft,isCrossRepository",
            ],
            ["gh", "issue", "view", "51", "--json", "number,title,url,state"],
        ])

    def test_metadata_rejects_controls_oversize_and_invalid_object_id(self) -> None:
        base = {
            "number": 34,
            "title": "safe",
            "url": "https://github.example/repo/pull/34",
            "headRefOid": "a" * 40,
            "state": "OPEN",
            "isDraft": False,
            "isCrossRepository": False,
        }
        cases = (
            {**base, "title": "line one\nline two"},
            {**base, "url": "https://example.invalid/\u202econtrol"},
            {**base, "title": "x" * 501},
            {**base, "headRefOid": "not-a-commit"},
        )
        for value in cases:
            with self.subTest(value=value):
                adapter = GithubAdapter(runner=lambda argv, **kwargs: self.completed(
                    argv, stdout=json.dumps(value).encode(),
                ))
                with self.assertRaises(SourceError):
                    adapter.pr_metadata(self.source, 34)

    def test_fetch_and_import_use_only_the_reviewed_mutating_argv(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return self.completed(argv)

        adapter = GithubAdapter(runner=runner)
        fetch_report = adapter.fetch_pr_head(self.source, "origin", 34)
        import_report = adapter.import_into_jj(self.source)

        self.assertEqual(calls, [
            [
                "git", "-C", str(self.source), "fetch", "origin",
                "pull/34/head:refs/remotes/origin/asha-control-pr-34",
            ],
            ["jj", "-R", str(self.source), "--ignore-working-copy", "git", "import"],
        ])
        self.assertIn("fetched Git objects", fetch_report[0]["detail"])
        self.assertIn("refs/remotes/origin/asha-control-pr-34", fetch_report[1]["detail"])
        self.assertIn("jj operation-log", import_report[0]["detail"])


class Increment6GrammarAndDoctorTests(unittest.TestCase):
    def test_source_selector_grammar_and_issue_base_precedence(self) -> None:
        issue = _parse_start([
            "--issue", "9", "--base", "main@origin", "--goal", "Fix it",
        ])
        self.assertEqual((issue["issue"], issue["base"]), (9, "main@origin"))
        for argv, detail in (
            (["--pr", "9", "--base", "trunk()", "--goal", "x"], "--pr.*--base"),
            (["--pr", "9", "--issue", "8", "--goal", "x"], "--pr.*--issue"),
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, detail):
                _parse_start(argv)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = control_main(["task", "start", *argv], env={})
            self.assertEqual((status, stdout.getvalue()), (2, ""))
            self.assertRegex(stderr.getvalue(), detail)
        with self.assertRaisesRegex(ValueError, "requires --goal"):
            _parse_start(["--pr", "9"])

    def test_gh_doctor_probe_is_always_optional_and_names_its_scope(self) -> None:
        with mock.patch("lib.control.doctor.shutil.which", return_value=None):
            result = run_doctor(None, probes={"gh": DEFAULT_PROBES["gh"]})
        self.assertTrue(result["ok"])
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        self.assertIn("required only for --pr and --issue", result["probes"][0]["detail"])


_REAL_SOURCE_TOOLS = all(shutil.which(name) for name in ("git", "jj"))


@unittest.skipUnless(_REAL_SOURCE_TOOLS, "git and jj are required for the real source fixture")
class RealGithubSourceTests(unittest.TestCase):
    PR_NUMBER = 34
    ISSUE_NUMBER = 51

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.jj_config_patch = mock.patch.dict(
            os.environ, {
                "JJ_CONFIG": str(self.root / "jj-config.toml"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
            }, clear=False,
        )
        self.jj_config_patch.start()
        self.addCleanup(self.jj_config_patch.stop)
        self.home = self.root / "home"
        self.home.mkdir()
        self.remote = self.root / "origin.git"
        self.producer = self.root / "producer"
        self.source = self.root / "fixture-repo"
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }

        self.remote.mkdir()
        self._git(self.remote, "init", "-q", "--bare")
        self.producer.mkdir()
        self._git(self.producer, "init", "-q", "-b", "main")
        (self.producer / ".gitignore").write_text("/.asha/\n/Memory/\n/Work/\n")
        (self.producer / "tracked.txt").write_text("base\n")
        self._git(self.producer, "add", ".gitignore", "tracked.txt")
        self._git(self.producer, "commit", "-qm", "base")
        self.base_commit = self._git_output(self.producer, "rev-parse", "HEAD")
        self._git(self.producer, "remote", "add", "origin", str(self.remote))
        self._git(self.producer, "push", "-q", "-u", "origin", "main")
        subprocess.run(
            ["git", f"--git-dir={self.remote}", "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True, capture_output=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "clone", "-q", str(self.remote), str(self.source)],
            check=True, capture_output=True, env=self.git_env,
        )

        (self.producer / "pr.txt").write_text("pull request head\n")
        self._git(self.producer, "add", "pr.txt")
        self._git(self.producer, "commit", "-qm", "pull request head")
        self.pr_commit = self._git_output(self.producer, "rev-parse", "HEAD")
        self._git(
            self.producer, "push", "-q", "origin",
            "HEAD:refs/asha-fixture/pr-object",
        )
        subprocess.run(
            [
                "git", f"--git-dir={self.remote}", "update-ref",
                f"refs/pull/{self.PR_NUMBER}/head", self.pr_commit,
            ],
            check=True, capture_output=True, env=self.git_env,
        )
        subprocess.run(
            [
                "git", f"--git-dir={self.remote}", "update-ref", "-d",
                "refs/asha-fixture/pr-object",
            ],
            check=True, capture_output=True, env=self.git_env,
        )

        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.source)],
            check=True, capture_output=True, text=True,
        )
        # No pre-seeding. The source must know nothing of the PR head so the
        # adapter's own fetch and import are what make it visible; seeding it
        # here would manufacture the success condition and report confidence
        # the code never earned.
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "increment-six-fixture",
        }) + "\n")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n"
        )
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.gh_log = self.root / "gh-argv.log"
        self.shim = self.root / "shim"
        self.shim.mkdir()
        fake_gh = self.shim / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$ASHA_TEST_GH_LOG\"\n"
            "case \"$1 $2\" in\n"
            "  'auth status') [[ \"${ASHA_TEST_GH_AUTH:-ok}\" == ok ]] ;;\n"
            "  'pr view') printf '%s\\n' \"$ASHA_TEST_PR_JSON\" ;;\n"
            "  'issue view') printf '%s\\n' \"$ASHA_TEST_ISSUE_JSON\" ;;\n"
            "  *) exit 93 ;;\n"
            "esac\n"
        )
        fake_gh.chmod(0o700)
        self.title = "-display; #{pane_id} only"

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, env=self.git_env,
        )

    def _git_output(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True, env=self.git_env,
        ).stdout.strip()

    def _jj_bytes(self, *args: str) -> bytes:
        return subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy", *args],
            check=True, capture_output=True,
        ).stdout

    def source_positions(self) -> dict[str, bytes]:
        # Staged CONTENT, not raw .git/index bytes. `jj git import` makes git
        # rewrite the index's TREE cache extension, which changes those bytes
        # while staging nothing: measured directly, `git ls-files -s` and
        # `git status --porcelain` are byte-identical across the import. Listing
        # every staged entry (mode, object, stage, path) plus the porcelain
        # status is the stricter check — it still fails if anything is actually
        # staged, unstaged, or modified — without asserting on an internal cache.
        return {
            "at": self._jj_bytes(
                "log", "-r", "@", "--no-graph", "-T", "change_id ++ commit_id",
            ),
            "head": (self.source / ".git" / "HEAD").read_bytes(),
            "staged": self._git_output(self.source, "ls-files", "-s").encode(),
            "status": self._git_output(self.source, "status", "--porcelain").encode(),
            "bookmarks": self._jj_bytes(
                "bookmark", "list", "-T",
                'name ++ "\\t" ++ normal_target.commit_id() ++ "\\n"',
            ),
        }

    def gh_environment(self, *, title: str | None = None, auth: str = "ok") -> dict[str, str]:
        pr_title = self.title if title is None else title
        return {
            "PATH": str(self.shim) + os.pathsep + os.environ["PATH"],
            "ASHA_TEST_GH_LOG": str(self.gh_log),
            "ASHA_TEST_GH_AUTH": auth,
            "ASHA_TEST_PR_JSON": json.dumps({
                "number": self.PR_NUMBER,
                "title": pr_title,
                "url": f"https://github.example/repo/pull/{self.PR_NUMBER}",
                "headRefOid": self.pr_commit,
                "state": "OPEN",
                "isDraft": False,
                "isCrossRepository": True,
            }),
            "ASHA_TEST_ISSUE_JSON": json.dumps({
                "number": self.ISSUE_NUMBER,
                "title": "Issue context",
                "url": f"https://github.example/repo/issues/{self.ISSUE_NUMBER}",
                "state": "OPEN",
            }),
        }

    def invoke(self, args: list[str], *, process_env: dict[str, str] | None = None,
               launch=None) -> tuple[int, str, str, dict | None]:
        stdout, stderr = io.StringIO(), io.StringIO()
        captured: dict[str, object] = {}

        def fake_launch(config, task, **kwargs):
            captured["task"] = task
            captured["launch"] = kwargs
            if launch is not None:
                return launch(config, task, **kwargs)
            return {"task": task, "run": {}}

        additions = self.gh_environment() if process_env is None else process_env
        with mock.patch.dict(os.environ, additions, clear=False), \
                mock.patch("lib.control.cli.launch_task", side_effect=fake_launch), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(args, env=self.env)
        return status, stdout.getvalue(), stderr.getvalue(), captured.get("task")

    def test_pr_mode_resolves_head_preserves_source_and_reports_mutations(self) -> None:
        before = self.source_positions()
        status, stdout, stderr, task = self.invoke([
            "task", "start", "--repo", str(self.source),
            "--pr", str(self.PR_NUMBER), "--harness", "codex",
            "--goal", "Address requested changes", "--detach", "--json",
        ])
        self.assertEqual(status, 0, stderr)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(before, self.source_positions())
        self.assertEqual(task["jj"]["requested_base"], f"PR #{self.PR_NUMBER} head")
        self.assertEqual(task["jj"]["base_commit_id"], self.pr_commit)
        self.assertEqual(task["source"], {
            "kind": "pr",
            "number": self.PR_NUMBER,
            "url": f"https://github.example/repo/pull/{self.PR_NUMBER}",
        })
        identity = JjAdapter().inspect_workspace(
            Path(task["jj"]["workspace_path"]), task["jj"]["workspace_name"],
        )
        self.assertEqual(identity.parent_commit_ids, (self.pr_commit,))
        self.assertEqual(
            self._git_output(self.source, "rev-parse", f"refs/remotes/origin/asha-control-pr-{self.PR_NUMBER}"),
            self.pr_commit,
        )
        self.assertNotIn(f"pr-{self.PR_NUMBER}", self.source_positions()["bookmarks"].decode())
        self.assertIn(self.title, stderr)
        self.assertIn("fetched Git objects", stderr)
        self.assertIn("jj operation-log", stderr)
        self.assertEqual(json.loads(stdout)["source_mutations"][1]["ref"],
                         f"refs/remotes/origin/asha-control-pr-{self.PR_NUMBER}")

        calls = self.gh_log.read_text().splitlines()
        self.assertEqual(calls[0], "auth status")
        self.assertTrue(calls[1].startswith(f"pr view {self.PR_NUMBER} --json "))
        forbidden = {"comment", "edit", "close", "merge", "review", "create"}
        self.assertFalse(any(forbidden & set(line.split()) for line in calls))

    def test_pre_resolved_prepare_bypasses_revset_resolution_and_carries_source(self) -> None:
        class TrackingJj(JjAdapter):
            resolve_called = False
            visibility_called = False

            def resolve_base(inner, source, revset):
                inner.resolve_called = True
                raise AssertionError("resolved PR bases must bypass resolve_base")

            def require_visible_commit(inner, source, commit_id):
                inner.visibility_called = True
                return super().require_visible_commit(source, commit_id)

        # Production fetches and imports the PR head before preparing, so the
        # commit is visible to jj. Do the same here rather than assuming
        # visibility the adapter is responsible for establishing.
        sources = GithubAdapter()
        sources.fetch_pr_head(self.source, "origin", self.PR_NUMBER)
        sources.import_into_jj(self.source)

        adapter = TrackingJj()
        request = PrepareRequest(
            repository=self.source,
            requested_base=f"PR #{self.PR_NUMBER} head",
            resolved_base_commit_id=self.pr_commit,
            task_id=str(uuid.uuid4()),
            slug=f"fixture-repo-pr-{self.PR_NUMBER}",
            label="Use resolved head",
            source={
                "kind": "pr",
                "number": self.PR_NUMBER,
                "url": f"https://github.example/repo/pull/{self.PR_NUMBER}",
            },
        )

        task = prepare_task_workspace(self.config, request, jj=adapter)

        self.assertFalse(adapter.resolve_called)
        self.assertTrue(adapter.visibility_called)
        self.assertEqual(task["jj"]["base_commit_id"], self.pr_commit)
        self.assertEqual(task["source"], request.source)

    def test_issue_mode_uses_trunk_and_explicit_base_and_persists_no_title(self) -> None:
        before = self.source_positions()
        status, _stdout, stderr, task = self.invoke([
            "task", "start", "--repo", str(self.source),
            "--issue", str(self.ISSUE_NUMBER), "--goal", "Investigate issue",
            "--detach", "--json",
        ])
        self.assertEqual(status, 0, stderr)
        assert task is not None
        self.assertEqual(before, self.source_positions())
        self.assertEqual(task["source"]["kind"], "issue")
        self.assertEqual(task["jj"]["requested_base"], "trunk()")
        self.assertEqual(task["jj"]["base_commit_id"], self.base_commit)
        self.assertEqual(set(task["source"]), {"kind", "number", "url"})
        self.assertNotIn("Issue context", json.dumps(task))

        other = self.root / "second-state"
        env = {**self.env, "XDG_STATE_HOME": str(other / "state"),
               "XDG_DATA_HOME": str(other / "data"),
               "XDG_RUNTIME_DIR": str(other / "runtime")}
        stdout, stderr_stream = io.StringIO(), io.StringIO()
        captured: dict[str, dict] = {}

        def fake_launch(config, prepared, **kwargs):
            captured["task"] = prepared
            return {"task": prepared, "run": {}}

        with mock.patch.dict(os.environ, self.gh_environment(), clear=False), \
                mock.patch("lib.control.cli.launch_task", side_effect=fake_launch), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr_stream):
            rc = control_main([
                # Issue mode performs no PR fetch, so the base must be a commit
                # the source can actually see. The point under test is that an
                # explicit --base is used verbatim instead of trunk().
                "task", "start", "--repo", str(self.source),
                "--issue", str(self.ISSUE_NUMBER), "--base", self.base_commit,
                "--goal", "Use explicit base", "--detach", "--json",
            ], env=env)
        self.assertEqual(rc, 0, stderr_stream.getvalue())
        self.assertEqual(captured["task"]["jj"]["requested_base"], self.base_commit)
        self.assertEqual(captured["task"]["jj"]["base_commit_id"], self.base_commit)

    def test_absent_or_unauthenticated_gh_refuses_before_mutation(self) -> None:
        tool_path = self.root / "tools-without-gh"
        tool_path.mkdir()
        for name in ("git", "jj"):
            (tool_path / name).symlink_to(shutil.which(name))
        cases = (
            ({"PATH": str(tool_path)}, "not installed|not found"),
            (self.gh_environment(auth="bad"), "not authenticated"),
        )
        for process_env, expected in cases:
            with self.subTest(expected=expected):
                before = self.source_positions()
                status, stdout, stderr, task = self.invoke([
                    "task", "start", "--repo", str(self.source),
                    "--pr", str(self.PR_NUMBER), "--goal", "No mutation",
                    "--detach", "--json",
                ], process_env=process_env)
                self.assertEqual((status, stdout, task), (2, "", None))
                self.assertRegex(stderr, expected)
                self.assertEqual(before, self.source_positions())
                self.assertFalse(self._git_ref_exists(f"refs/remotes/origin/asha-control-pr-{self.PR_NUMBER}"))
                self.assertEqual(TaskStore(self.config).list(), [])

    def _git_ref_exists(self, ref: str) -> bool:
        return subprocess.run(
            ["git", "-C", str(self.source), "show-ref", "--verify", "--quiet", ref],
            check=False, capture_output=True, env=self.git_env,
        ).returncode == 0

    def test_hostile_control_title_reaches_no_slug_tmux_harness_or_record_sink(self) -> None:
        hostile = "-leading; #{pane_id}\ncontrol-\u0001"
        calls = {
            "slug": mock.Mock(side_effect=AssertionError("slug sink reached")),
            "prepare": mock.Mock(side_effect=AssertionError("record sink reached")),
            "launch": mock.Mock(side_effect=AssertionError("harness sink reached")),
            "tmux": mock.Mock(side_effect=AssertionError("tmux sink reached")),
        }
        with mock.patch.dict(os.environ, self.gh_environment(title=hostile), clear=False), \
                mock.patch("lib.control.cli._slug", calls["slug"]), \
                mock.patch("lib.control.cli.prepare_task_workspace", calls["prepare"]), \
                mock.patch("lib.control.cli.launch_task", calls["launch"]), \
                mock.patch("lib.control.tmux.TmuxAdapter.create_task_session", calls["tmux"]):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = control_main([
                    "task", "start", "--repo", str(self.source),
                    "--pr", str(self.PR_NUMBER), "--goal", "Safe operator goal",
                    "--detach", "--json",
                ], env=self.env)
        self.assertEqual((rc, stdout.getvalue()), (2, ""))
        self.assertIn("control", stderr.getvalue().casefold())
        for sink in calls.values():
            sink.assert_not_called()
        self.assertFalse(self._git_ref_exists(f"refs/remotes/origin/asha-control-pr-{self.PR_NUMBER}"))

    def test_display_title_is_discarded_before_all_launch_sinks(self) -> None:
        status, _stdout, stderr, task = self.invoke([
            "task", "start", "--repo", str(self.source),
            "--pr", str(self.PR_NUMBER), "--goal", "Safe operator goal",
            "--harness", "codex", "--detach", "--json",
        ])
        self.assertEqual(status, 0, stderr)
        assert task is not None
        adapter = RecordingTmux()
        with mock.patch("lib.control.launch.harness_api.process_identity", return_value="proc:fixture"), \
                mock.patch("lib.control.launch.harness_api.pane_ancestry_ok", return_value=True):
            launched = launch_task(
                self.config, task, tmux=adapter, harness="codex",
                goal_args=("Safe operator goal",),
            )["task"]
        sinks = {
            "slug": launched["slug"],
            "tmux": adapter.creation,
            "harness": adapter.respawn_argv,
            "record": launched,
        }
        self.assertEqual(launched["slug"], f"fixture-repo-pr-{self.PR_NUMBER}")
        for name, value in sinks.items():
            with self.subTest(sink=name):
                self.assertNotIn(self.title, json.dumps(value, sort_keys=True))
        self.assertEqual(set(launched["source"]), {"kind", "number", "url"})


class RecordingTmux(FakeTmux):
    def __init__(self) -> None:
        super().__init__()
        self.creation: dict = {}
        self.respawn_argv: list[str] = []

    def create_task_session(self, **kwargs):
        self.creation = kwargs
        return super().create_task_session(**kwargs)

    def respawn(self, pane_id, argv):
        self.respawn_argv = list(argv)
        return super().respawn(pane_id, argv)


class CliUsesLiveEvidenceTests(unittest.TestCase):
    """`list`/`show`/`reconcile` must reconcile from LIVE adapters.

    Regression guard. Every other test injects adapters directly, so nothing
    asserted what the production CLI actually constructs. It kept Increment 1's
    `UnavailableAdapters` placeholder long after the live tmux, process, jj, and
    event adapters shipped, and a real `task show` reported
    "not implemented in Increment 1" for every seam while silently falling back
    to the stored record.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / "home").mkdir()
        self.env = {
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "ASHA_CONFIG": str(root / "missing.json"),
        }

    def test_read_verbs_construct_live_adapters(self) -> None:
        for verb in ("list", "reconcile"):
            with self.subTest(verb=verb):
                with mock.patch(
                    "lib.control.cli.LiveAdapters", wraps=LiveAdapters,
                ) as live, mock.patch(
                    "lib.control.cli.UnavailableAdapters",
                ) as unavailable:
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        status = control_main(["task", verb, "--json"], env=self.env)
                    self.assertEqual(status, 0)
                    self.assertTrue(
                        live.called,
                        f"`asha task {verb}` must reconcile from LiveAdapters",
                    )
                    self.assertFalse(
                        unavailable.called,
                        f"`asha task {verb}` must not fall back to the Increment 1 stub",
                    )


if __name__ == "__main__":
    unittest.main()


class TmuxPresentationTests(unittest.TestCase):
    """The event path must mirror state into the tmux values users bind.

    The contract's tmux Presentation section requires hooks to update pane-local
    state and the server summary. Increment 4 shipped the data half (snapshot +
    reconciliation) but not this, so `task show` was correct while a status-line
    binding showed the launch-time value forever.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / "home").mkdir()
        self.env = {
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "run"),
            "ASHA_CONFIG": str(root / "missing.json"),
        }
        self.config = load_config(self.env)
        self.task_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())

    def _managed_env(self) -> dict[str, str]:
        return {
            **self.env,
            "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task_id,
            "ASHA_CONTROL_RUN_ID": self.run_id,
            "ASHA_CONTROL_STATE_DIR": str(self.config.tasks_dir),
            "ASHA_HARNESS": "codex",
        }

    def _recording_adapter(self, *, run_option: str | None, task_option: str | None):
        writes: list[tuple] = []
        outer = self

        class Recording:
            def pane_option(self, pane_id, option, **kwargs):
                return run_option if option == "@asha_run_id" else None

            def session_option(self, name, option, **kwargs):
                return task_option if option == "@asha_task_id" else None

            def pane_facts(self, pane_id, **kwargs):
                return type("F", (), {"session": "asha-demo-1234abcd"})()

            def set_pane_option(self, pane_id, option, value, **kwargs):
                writes.append(("pane", pane_id, option, value))

            def set_session_option(self, name, option, value, **kwargs):
                writes.append(("session", name, option, value))

            def set_server_summary(self, value, **kwargs):
                writes.append(("server", "@asha_summary", value))

        return Recording, writes

    def test_event_publishes_state_to_pane_session_and_server(self) -> None:
        Recording, writes = self._recording_adapter(
            run_option=self.run_id, task_option=self.task_id,
        )
        with mock.patch("lib.control.cli.TmuxAdapter", Recording):
            status = control_main(
                ["control", "event", "--event", "turn-stopped", "--pane-id", "%7"],
                env=self._managed_env(),
            )
        self.assertEqual(status, 0)
        self.assertIn(("pane", "%7", "@asha_state", "idle"), writes)
        self.assertIn(("session", "asha-demo-1234abcd", "@asha_state", "idle"), writes)
        summary = [w for w in writes if w[0] == "server"]
        self.assertEqual(len(summary), 1)
        self.assertIn("1 idle", summary[0][2])
        self.assertIn("1 total", summary[0][2])

    def test_foreign_pane_and_session_are_never_written(self) -> None:
        Recording, writes = self._recording_adapter(
            run_option="a-different-run", task_option=self.task_id,
        )
        with mock.patch("lib.control.cli.TmuxAdapter", Recording):
            control_main(
                ["control", "event", "--event", "turn-stopped", "--pane-id", "%7"],
                env=self._managed_env(),
            )
        self.assertEqual(writes, [], "a pane we do not own must never be written")

        Recording, writes = self._recording_adapter(
            run_option=self.run_id, task_option="a-different-task",
        )
        with mock.patch("lib.control.cli.TmuxAdapter", Recording):
            control_main(
                ["control", "event", "--event", "turn-stopped", "--pane-id", "%7"],
                env=self._managed_env(),
            )
        self.assertFalse(
            [w for w in writes if w[0] == "session"],
            "a session whose @asha_task_id differs must never be written",
        )

    def test_tmux_failure_never_fails_the_hook(self) -> None:
        class Exploding:
            def __getattr__(self, name):
                def boom(*args, **kwargs):
                    raise OSError("tmux is unavailable")
                return boom

        with mock.patch("lib.control.cli.TmuxAdapter", Exploding):
            status = control_main(
                ["control", "event", "--event", "tool-completed", "--pane-id", "%7"],
                env=self._managed_env(),
            )
        self.assertEqual(status, 0, "the hook path must stay fail-open")
        self.assertIsNotNone(
            read_snapshot(self.config, self.run_id),
            "the snapshot must still be written when tmux publication fails",
        )


class DoctorProbeCompletionTests(unittest.TestCase):
    """`jj` and `repository` were Increment 1 stubs long after the code shipped.

    Both reported "not probed in Increment 1" forever, so `task doctor`
    under-reported exactly the two things an operator most needs before starting
    a task: whether jj exposes the command surface Control depends on, and
    whether the current directory can host a task at all.
    """

    def test_jj_probe_checks_command_semantics_not_a_version_string(self) -> None:
        calls: list[list[str]] = []

        def fake_capture(argv, **kwargs):
            calls.append(list(argv))
            if argv[1:] == ["--version"]:
                return 0, b"jj 0.38.0\n", b""
            return 0, b"help\n", b""

        with mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                mock.patch("lib.control.doctor.capture_bytes", side_effect=fake_capture):
            result = run_doctor(None, probes={"jj": DEFAULT_PROBES["jj"]})
        probe = result["probes"][0]
        self.assertEqual(probe["outcome"], "match")
        probed = {tuple(c[1:-1]) for c in calls if c[-1] == "--help"}
        self.assertEqual(
            probed,
            {("workspace", "add"), ("workspace", "forget"),
             ("operation", "log"), ("git", "import")},
        )

    def test_jj_probe_reports_a_missing_command_as_mismatch(self) -> None:
        def fake_capture(argv, **kwargs):
            if argv[1:] == ["--version"]:
                return 0, b"jj 0.30.0\n", b""
            if argv[1:3] == ["workspace", "forget"]:
                return 2, b"", b"unrecognized subcommand"
            return 0, b"help\n", b""

        with mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                mock.patch("lib.control.doctor.capture_bytes", side_effect=fake_capture):
            result = run_doctor(None, probes={"jj": DEFAULT_PROBES["jj"]})
        probe = result["probes"][0]
        self.assertEqual(probe["outcome"], "mismatch")
        self.assertIn("workspace forget", probe["detail"])

    def test_jj_probe_is_unavailable_without_the_executable(self) -> None:
        with mock.patch("lib.control.doctor.shutil.which", return_value=None):
            result = run_doctor(None, probes={"jj": DEFAULT_PROBES["jj"]})
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")

    def test_repository_probe_reports_uninitialized_and_absent_repositories(self) -> None:
        with mock.patch("lib.control.doctor.shutil.which", return_value=None):
            result = run_doctor(None, probes={"repository": DEFAULT_PROBES["repository"]})
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")

        # A jj repository that is not Memory v2 initialized must report `missing`
        # rather than `match`: task creation would refuse, and doctor should say
        # so before the operator discovers it mid-launch.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()

            class FakeJj:
                def discover_root(self, start):
                    return root

                def preflight(self, source):
                    return type("Facts", (), {"root": root, "git_root": root / ".git"})()

            with mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                    mock.patch("lib.control.doctor.JjAdapter", FakeJj), \
                    mock.patch("lib.control.doctor.Path") as fake_path:
                fake_path.cwd.return_value = root
                result = run_doctor(
                    None, probes={"repository": DEFAULT_PROBES["repository"]},
                )
            self.assertEqual(result["probes"][0]["outcome"], "missing")
            self.assertIn("Memory v2", result["probes"][0]["detail"])

    @unittest.skipUnless(shutil.which("git"), "git is required for the plain-Git repository probe")
    def test_repository_probe_reports_jj_colocate_remediation_for_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            self.assertFalse((root / ".jj").exists())

            with contextlib.chdir(root):
                result = run_doctor(
                    None, probes={"repository": DEFAULT_PROBES["repository"]},
                )

            probe = result["probes"][0]
            self.assertEqual(probe["outcome"], "missing")
            self.assertIn("jj git init --colocate", probe["detail"])
            self.assertIn(str(root), probe["detail"])
            self.assertFalse((root / ".jj").exists())

    def test_repository_probe_keeps_generic_result_outside_any_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with contextlib.chdir(root):
                result = run_doctor(
                    None, probes={"repository": DEFAULT_PROBES["repository"]},
                )

        probe = result["probes"][0]
        self.assertEqual(probe["outcome"], "unavailable")
        self.assertEqual(
            probe["detail"],
            "the working directory is not inside a jj repository; "
            "run `asha task start --repo PATH` or change directory",
        )
        self.assertNotIn("jj git init --colocate", probe["detail"])
