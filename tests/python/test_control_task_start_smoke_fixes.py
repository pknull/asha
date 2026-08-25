from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.cli import (
    RepositorySelection, _build_start_preflight_request, _ensure_colocated,
    _guard_colocated_sync, _parse_start, _preflight_plain_git_start,
    _start_new_task, main as control_main,
)
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import (
    ColocationIntentStore, DEFAULT_BASE_REVSET, DefaultBaseResolution, JjAdapter,
    JjError, RepositoryFacts,
    discover_git_root, inspect_pre_enable_binding, require_pre_enable_binding,
)
from lib.control.prepare import PreparationError
from lib.control.prepare import (
    PlainGitPreEnablePlan, PrepareRequest, preflight_plain_git_enablement,
    revalidate_plain_git_pre_enable_plan,
)
from lib.control.sources import GithubAdapter, ValidatedPrRemote
from lib.control.store import StoreError, TaskStore
from lib.control.transaction import CreationJournalStore, JournalError
from lib.control.tui import (
    ModalCandidate, StartCandidateSnapshot, TuiModel,
    _TuiShutdown,
    _classify_start_worker_exit,
    _default_base_candidate,
    _reap_deferred_start_workers,
    _retry_arguments,
    _start_form,
    _successful_worker_message,
    _supervise_start_process,
)
from tests.python.test_control_config_model import task_record


TASK_ID = "12345678-1234-4234-8234-123456789abc"


class DefaultBaseResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", "-b", "master", str(self.repo)], check=True)
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        self.first = self.commit("first")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True, env=self.git_env,
        ).stdout.strip()

    def commit(self, label: str) -> str:
        (self.repo / "tracked.txt").write_text(label + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "tracked.txt"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", label],
            check=True, env=self.git_env,
        )
        return self.git("rev-parse", "HEAD")

    def remote_default(self, remote: str, oid: str, branch: str = "master") -> None:
        self.git("update-ref", f"refs/remotes/{remote}/{branch}", oid)
        self.git(
            "symbolic-ref", f"refs/remotes/{remote}/HEAD",
            f"refs/remotes/{remote}/{branch}",
        )

    @staticmethod
    def completed(
        argv: list[str], *, returncode: int = 0, stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def scripted_default_runner(
        self, *, symbolic: bytes, remotes: bytes = b"",
        ref_stdout: bytes | None = None,
    ):
        oid_output = (self.first + "\n").encode() if ref_stdout is None else ref_stdout

        def runner(argv, **_kwargs):
            if "symbolic-ref" in argv:
                return self.completed(
                    list(argv), returncode=1 if symbolic == b"" else 0,
                    stdout=symbolic,
                )
            if "for-each-ref" in argv:
                return self.completed(list(argv), stdout=remotes)
            if "show-ref" in argv:
                return self.completed(list(argv))
            if "rev-parse" in argv:
                return self.completed(list(argv), stdout=oid_output)
            raise AssertionError(f"unexpected exact-Git argv: {argv!r}")

        return runner

    def test_attached_local_branch_wins_over_stale_packed_and_current_remote_defaults(self) -> None:
        self.remote_default("origin", self.first)
        self.git("pack-refs", "--all")
        current = self.commit("current")
        self.remote_default("keybase", current)

        resolution = JjAdapter().resolve_default_base(self.repo)

        self.assertEqual(resolution, DefaultBaseResolution(
            references=("refs/heads/master",),
            commit_id=current,
            tier="attached-local",
        ))

        candidate, expected = _default_base_candidate(self.repo)
        self.assertEqual(expected, current)
        self.assertEqual(candidate.value, "")
        self.assertIn("refs/heads/master", candidate.detail)
        self.assertIn(current[:12], candidate.detail)

    def test_tui_submits_preview_oid_as_assertion_without_making_it_the_base(self) -> None:
        current = self.git("rev-parse", "HEAD")
        home = self.root / "home"
        home.mkdir()
        env = {
            "HOME": str(home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        config = load_config(env)
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repo), "test"),),
            bases={str(self.repo): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(config.default_harness, "installed"),),
            roles=("implementer",),
        )
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "goal"), 10,
        ])
        with mock.patch(
            "lib.control.tui.freeze_start_candidates", return_value=snapshot,
        ), mock.patch(
            "lib.control.tui._source_colocation_watch", return_value=(None, False),
        ), mock.patch(
            "lib.control.tui._supervise_start_process", return_value="started",
        ) as supervise:
            self.assertEqual(
                _start_form(screen, FakeCurses(), TuiModel([]), env, config),
                "started",
            )

        argv = supervise.call_args.args[4]
        self.assertNotIn("--base", argv)
        self.assertEqual(argv[argv.index("--expected-default") + 1], current)

    def test_detached_head_accepts_multiple_remote_defaults_at_the_same_oid(self) -> None:
        self.remote_default("origin", self.first)
        self.remote_default("keybase", self.first)
        self.git("checkout", "--detach", "-q", self.first)

        resolution = JjAdapter().resolve_default_base(self.repo)

        self.assertEqual(resolution.commit_id, self.first)
        self.assertEqual(resolution.tier, "remote-head")
        self.assertEqual(resolution.references, (
            "refs/remotes/keybase/master", "refs/remotes/origin/master",
        ))

    def test_detached_head_refuses_remote_defaults_at_different_oids(self) -> None:
        second = self.commit("second")
        self.remote_default("origin", self.first)
        self.remote_default("keybase", second)
        self.git("checkout", "--detach", "-q", second)

        with self.assertRaisesRegex(
            JjError, r"different commits.*--base",
        ) as caught:
            JjAdapter().resolve_default_base(self.repo)

        self.assertIn("refs/remotes/keybase/master", str(caught.exception))
        self.assertIn("refs/remotes/origin/master", str(caught.exception))

    def test_unborn_head_falls_through_to_one_remote_default(self) -> None:
        unborn = self.root / "unborn"
        subprocess.run(["git", "init", "-q", "-b", "work", str(unborn)], check=True)
        self.git("update-ref", "refs/remotes/origin/master", self.first)
        self.git(
            "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master",
        )
        # Copy only the object/ref backend into a repository whose attached HEAD
        # names an unborn branch.
        subprocess.run(
            ["git", f"--git-dir={self.repo / '.git'}", "bundle", "create",
             str(self.root / "one.bundle"), "refs/remotes/origin/master"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(unborn), "fetch", "-q", str(self.root / "one.bundle"),
             "refs/remotes/origin/master:refs/remotes/origin/master"], check=True,
        )
        subprocess.run([
            "git", "-C", str(unborn), "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/master",
        ], check=True)

        resolution = JjAdapter().resolve_default_base(unborn)

        self.assertEqual(resolution.commit_id, self.first)
        self.assertEqual(resolution.tier, "remote-head")

    def test_conventional_local_refs_are_ambiguous_only_when_oids_differ(self) -> None:
        self.git("checkout", "--detach", "-q", self.first)
        self.git("branch", "-D", "master")
        self.git("branch", "main", self.first)
        self.git("branch", "trunk", self.first)

        agreeing = JjAdapter().resolve_default_base(self.repo)

        self.assertEqual(agreeing.tier, "conventional-local")
        self.assertEqual(agreeing.references, ("refs/heads/main", "refs/heads/trunk"))
        second = self.commit("detached-second")
        self.git("branch", "-f", "trunk", second)
        with self.assertRaisesRegex(JjError, r"different commits.*--base"):
            JjAdapter().resolve_default_base(self.repo)

    def test_detached_repository_without_candidates_refuses_explicitly(self) -> None:
        self.git("checkout", "--detach", "-q", self.first)
        self.git("branch", "-D", "master")

        with self.assertRaisesRegex(JjError, r"no attached.*explicit --base"):
            JjAdapter().resolve_default_base(self.repo)

    def test_existing_remote_default_that_is_not_a_commit_fails_closed(self) -> None:
        self.git("checkout", "--detach", "-q", self.first)
        self.git("branch", "-D", "master")
        blob = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
            input="blob\n", check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.git("update-ref", "refs/remotes/origin/master", blob)
        self.git(
            "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/master",
        )

        with self.assertRaisesRegex(JjError, r"does not name a commit"):
            JjAdapter().resolve_default_base(self.repo)

    def test_exact_git_disables_lazy_promisor_fetch_for_default_and_ref_reads(self) -> None:
        requests: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler_self) -> None:
                requests.append(handler_self.path)
                handler_self.send_response(404)
                handler_self.send_header("Content-Length", "0")
                handler_self.end_headers()

            def do_POST(handler_self) -> None:
                requests.append(handler_self.path)
                handler_self.send_response(404)
                handler_self.send_header("Content-Length", "0")
                handler_self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        observed_environments: list[dict[str, str]] = []

        def runner(argv, **kwargs):
            observed_environments.append(dict(kwargs["env"]))
            return subprocess.run(argv, **kwargs)

        try:
            port = server.server_address[1]
            self.git("config", "core.repositoryformatversion", "1")
            self.git("config", "extensions.partialClone", "origin")
            self.git("config", "remote.origin.promisor", "true")
            self.git("config", "remote.origin.partialCloneFilter", "blob:none")
            self.git(
                "config", "remote.origin.url",
                f"http://127.0.0.1:{port}/internal.git",
            )
            missing = "1" * 40
            (self.repo / ".git" / "refs" / "heads" / "master").write_text(
                missing + "\n", encoding="ascii",
            )
            adapter = JjAdapter(runner=runner)

            with self.assertRaises(JjError):
                adapter.resolve_default_base(self.repo)
            with self.assertRaises(JjError):
                adapter._default_ref_commit(self.repo, "refs/heads/master")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(requests, [])
        self.assertTrue(observed_environments)
        self.assertTrue(all(
            environment.get("GIT_NO_LAZY_FETCH") == "1"
            for environment in observed_environments
        ))

    def test_attached_ref_requires_complete_git_ref_grammar(self) -> None:
        malformed = (
            b"refs/heads/foo~bar\n",
            b"refs/heads/foo..bar\n",
            b"refs/heads/.hidden\n",
            b"refs/heads/foo.lock\n",
            b"refs/heads/foo@{bar\n",
            b"refs/heads/foo.\n",
            b"refs/heads/foo//bar\n",
            b"refs/heads/foo\\bar\n",
            b"refs/heads/" + b"x" * 301 + b"\n",
            b"refs/heads/non-ascii-\xff\n",
        )
        for output in malformed:
            with self.subTest(output=output), self.assertRaisesRegex(
                JjError, "symbolic HEAD was malformed",
            ):
                JjAdapter(runner=self.scripted_default_runner(
                    symbolic=output,
                )).resolve_default_base(self.repo)

    def test_exact_ref_oid_rejects_padding_and_extra_blank_lines(self) -> None:
        malformed = (
            (" " + self.first + "\n").encode(),
            (self.first + " \n").encode(),
            (self.first + "\n\n").encode(),
            (self.first + "\n \n").encode(),
            (self.first + "\nother\n").encode(),
            self.first.encode() + b"\xff\n",
        )
        for output in malformed:
            with self.subTest(output=output), self.assertRaises(JjError):
                JjAdapter(runner=self.scripted_default_runner(
                    symbolic=b"refs/heads/master\n", ref_stdout=output,
                )).resolve_default_base(self.repo)

    def test_remote_default_fields_require_complete_git_ref_grammar(self) -> None:
        malformed = (
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/foo~bar\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/foo..bar\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/.hidden\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/foo.lock\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/foo@{bar\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/foo\\bar\n",
            b"refs/remotes/origin/HE~AD\0refs/remotes/origin/main\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/non-ascii-\xff\n",
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/"
            + b"x" * 301 + b"\n",
        )
        for output in malformed:
            with self.subTest(output=output), self.assertRaisesRegex(
                JjError, "remote-default refs were malformed",
            ):
                JjAdapter(runner=self.scripted_default_runner(
                    symbolic=b"", remotes=output,
                )).resolve_default_base(self.repo)

    def test_dangling_and_direct_remote_heads_fail_closed(self) -> None:
        dangling_row = (
            b"refs/remotes/origin/HEAD\0refs/remotes/origin/missing\n"
        )

        def dangling_runner(argv, **_kwargs):
            if "symbolic-ref" in argv:
                return self.completed(list(argv), returncode=1)
            if "for-each-ref" in argv:
                return self.completed(list(argv), stdout=dangling_row)
            if "show-ref" in argv:
                return self.completed(list(argv), returncode=1)
            raise AssertionError(f"unexpected exact-Git argv: {argv!r}")

        with self.assertRaisesRegex(JjError, "missing target"):
            JjAdapter(runner=dangling_runner).resolve_default_base(self.repo)

        direct_row = b"refs/remotes/origin/HEAD\0\n"
        with self.assertRaisesRegex(JjError, "remote-default refs were malformed"):
            JjAdapter(runner=self.scripted_default_runner(
                symbolic=b"", remotes=direct_row,
            )).resolve_default_base(self.repo)

    def test_attached_and_conventional_noncommit_refs_fail_closed(self) -> None:
        def noncommit_runner(*, attached: bool):
            def runner(argv, **_kwargs):
                if "symbolic-ref" in argv:
                    return self.completed(
                        list(argv), returncode=0 if attached else 1,
                        stdout=b"refs/heads/master\n" if attached else b"",
                    )
                if "for-each-ref" in argv:
                    return self.completed(list(argv))
                if "show-ref" in argv:
                    ref = argv[-1]
                    exists = attached or ref == "refs/heads/main"
                    return self.completed(
                        list(argv), returncode=0 if exists else 1,
                    )
                if "rev-parse" in argv:
                    return self.completed(list(argv), returncode=1)
                raise AssertionError(f"unexpected exact-Git argv: {argv!r}")

            return runner

        for attached in (True, False):
            with self.subTest(attached=attached), self.assertRaisesRegex(
                JjError, "does not name a commit",
            ):
                JjAdapter(runner=noncommit_runner(
                    attached=attached,
                )).resolve_default_base(self.repo)

    def test_remote_default_count_is_bounded_before_target_resolution(self) -> None:
        rows = b"".join(
            f"refs/remotes/r{index}/HEAD\0refs/remotes/r{index}/main\n".encode()
            for index in range(129)
        )
        calls: list[list[str]] = []
        delegate = self.scripted_default_runner(symbolic=b"", remotes=rows)

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return delegate(argv, **kwargs)

        with self.assertRaisesRegex(JjError, "too many remote default refs"):
            JjAdapter(runner=runner).resolve_default_base(self.repo)

        self.assertFalse(any("show-ref" in call for call in calls))


class FakeCurses:
    class error(Exception):
        pass

    KEY_ENTER = 343
    KEY_RESIZE = 410
    KEY_DOWN = 258


class ProgressScreen:
    def __init__(self, keys: list[int], *, key_delay: float = 0.0) -> None:
        self.keys = list(keys)
        self.key_delay = key_delay
        self.getch_calls = 0
        self.lines: list[str] = []

    def getmaxyx(self):
        return 24, 120

    def erase(self):
        pass

    def refresh(self):
        pass

    def addnstr(self, _y, _x, value, _limit, _attribute=0):
        self.lines.append(value)

    def move(self, _y, _x):
        pass

    def clrtoeol(self):
        pass

    def getch(self):
        self.getch_calls += 1
        if self.key_delay:
            time.sleep(self.key_delay)
        return self.keys.pop(0) if self.keys else -1


class TuiStartCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "HOME": str(home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        preview = mock.patch(
            "lib.control.tui._default_base_candidate",
            return_value=(
                ModalCandidate(
                    "", "current refs/heads/main @ " + "a" * 12,
                ),
                "a" * 40,
            ),
        )
        preview.start()
        self.addCleanup(preview.stop)

    def test_escape_at_each_field_never_starts_a_worker_and_affordance_is_visible(self) -> None:
        for cancelled_index in range(5):
            screen = ProgressScreen([10] * cancelled_index + [27])
            with self.subTest(field=cancelled_index), \
                    mock.patch("lib.control.tui._supervise_start_process") as supervise:
                message = _start_form(
                    screen, FakeCurses(), TuiModel([]), self.env, self.config,
                )

            self.assertEqual(message, "task start cancelled")
            supervise.assert_not_called()
            self.assertIn("Esc cancel", "\n".join(screen.lines))

    def test_completed_form_preallocates_id_and_requests_detached_json_worker(self) -> None:
        screen = ProgressScreen([10, 10, 10, 10, *map(ord, "goal"), 10])
        with mock.patch("lib.control.tui.new_uuid", return_value=TASK_ID), \
                mock.patch(
                    "lib.control.tui._supervise_start_process",
                    return_value="task started: goal",
                ) as supervise:
            message = _start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )

        self.assertEqual(message, "task started: goal")
        arguments = supervise.call_args.args[4]
        self.assertEqual(arguments[-7:], [
            "--goal", "goal", "--task-id", TASK_ID, "--detach", "--json",
            "--tui-worker",
        ])
        self.assertNotIn("--shell", arguments)

    def test_unavailable_default_keeps_operator_on_base_and_never_starts_worker(self) -> None:
        model = TuiModel([])

        class AdaptiveScreen(ProgressScreen):
            def getch(screen_self):
                screen_self.getch_calls += 1
                if screen_self.getch_calls <= 2:
                    return 10
                if model.message:
                    return 27
                sequence = [10, 10, *map(ord, "goal"), 10]
                offset = screen_self.getch_calls - 3
                return sequence[offset] if offset < len(sequence) else 27

        unavailable = ModalCandidate(
            "", "unavailable; select/type explicit base: no candidate",
        )
        with mock.patch(
            "lib.control.tui._default_base_candidate",
            return_value=(unavailable, None),
        ), mock.patch(
            "lib.control.tui._supervise_start_process",
        ) as supervise:
            message = _start_form(
                AdaptiveScreen([]), FakeCurses(), model, self.env, self.config,
            )

        self.assertEqual(message, "task start cancelled")
        self.assertIn("explicit Base", model.message)
        supervise.assert_not_called()

    def test_explicit_base_can_proceed_after_default_preview_failure(self) -> None:
        model = TuiModel([])

        class AdaptiveScreen(ProgressScreen):
            def __init__(screen_self) -> None:
                super().__init__([])
                screen_self.followup: list[int] = []

            def getch(screen_self):
                screen_self.getch_calls += 1
                if screen_self.getch_calls <= 2:
                    return 10
                if not screen_self.followup:
                    if not model.message:
                        return 27
                    screen_self.followup = [
                        *map(ord, "main"), 10, 10, 10,
                        *map(ord, "goal"), 10,
                    ]
                return screen_self.followup.pop(0)

        unavailable = ModalCandidate(
            "", "unavailable; select/type explicit base: no candidate",
        )
        with mock.patch(
            "lib.control.tui._default_base_candidate",
            return_value=(unavailable, None),
        ), mock.patch(
            "lib.control.tui._source_colocation_watch", return_value=(None, False),
        ), mock.patch(
            "lib.control.tui._supervise_start_process", return_value="started",
        ) as supervise:
            message = _start_form(
                AdaptiveScreen(), FakeCurses(), model, self.env, self.config,
            )

        self.assertEqual(message, "started")
        argv = supervise.call_args.args[4]
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertNotIn("--expected-default", argv)

    def test_resize_on_base_recomputes_default_preview_assertion(self) -> None:
        first = DefaultBaseResolution(
            ("refs/heads/main",), "a" * 40, "attached-local",
        )
        second = DefaultBaseResolution(
            ("refs/heads/main",), "b" * 40, "attached-local",
        )
        screen = ProgressScreen([
            10, FakeCurses.KEY_RESIZE, 10, 10, 10, *map(ord, "goal"), 10,
        ])
        with mock.patch(
            "lib.control.tui._default_base_candidate",
            side_effect=(
                (ModalCandidate("", f"current refs/heads/main @ {first.commit_id[:12]}"),
                 first.commit_id),
                (ModalCandidate("", f"current refs/heads/main @ {second.commit_id[:12]}"),
                 second.commit_id),
                (ModalCandidate("", f"current refs/heads/main @ {second.commit_id[:12]}"),
                 second.commit_id),
            ),
        ) as preview, mock.patch(
            "lib.control.tui._source_colocation_watch", return_value=(None, False),
        ), mock.patch(
            "lib.control.tui._supervise_start_process", return_value="started",
        ) as supervise:
            self.assertEqual(
                _start_form(screen, FakeCurses(), TuiModel([]), self.env, self.config),
                "started",
            )

        self.assertEqual(preview.call_count, 3)
        argv = supervise.call_args.args[4]
        self.assertEqual(
            argv[argv.index("--expected-default") + 1], second.commit_id,
        )

    def test_ongoing_worker_is_polled_and_escape_sends_one_group_sigterm(self) -> None:
        screen = ProgressScreen([27, 27])
        real_killpg = os.killpg
        signals: list[tuple[int, int]] = []

        def recording_killpg(pid: int, signum: int) -> None:
            signals.append((pid, signum))
            real_killpg(pid, signum)

        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        with mock.patch("lib.control.tui.os.killpg", side_effect=recording_killpg):
            message = _supervise_start_process(
                screen, FakeCurses(), TuiModel([]), self.config, argv, TASK_ID,
                self.env,
            )

        self.assertEqual(message, "task start cancelled")
        self.assertGreaterEqual(screen.getch_calls, 1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0][1], signal.SIGTERM)
        self.assertIn("Esc cancel", "\n".join(screen.lines))

    def test_normal_completion_wins_over_escape_read_during_worker_exit(self) -> None:
        payload = {
            "contract": "asha.control-task-start.v1",
            "task": {
                "task_id": TASK_ID, "slug": "done",
                "jj": {"base_commit_id": "b" * 40},
            },
        }
        script = f"import json; print(json.dumps({payload!r}))"
        screen = ProgressScreen([27], key_delay=0.2)
        with mock.patch("lib.control.tui.os.killpg") as killpg:
            message = _supervise_start_process(
                screen, FakeCurses(), TuiModel([]), self.config,
                [sys.executable, "-c", script], TASK_ID, self.env,
            )

        self.assertEqual(
            message, f"task started: done ({TASK_ID}); base {'b' * 40}",
        )
        killpg.assert_not_called()

    def test_success_message_requires_and_names_authoritative_full_base_oid(self) -> None:
        payload = json.dumps({
            "contract": "asha.control-task-start.v1",
            "task": {
                "task_id": TASK_ID, "slug": "done",
                "jj": {"base_commit_id": "c" * 40},
            },
            "source_mutations": [],
        }).encode()

        self.assertEqual(
            _successful_worker_message(payload, TASK_ID),
            f"task started: done ({TASK_ID}); base {'c' * 40}",
        )
        bad = json.loads(payload)
        bad["task"]["jj"]["base_commit_id"] = "short"
        with self.assertRaisesRegex(ValueError, "base commit"):
            _successful_worker_message(json.dumps(bad).encode(), TASK_ID)

    def test_retry_omits_base_only_for_legacy_default_sentinel(self) -> None:
        omitted = task_record()
        omitted["jj"]["requested_base"] = DEFAULT_BASE_REVSET
        explicit = task_record()
        explicit["jj"]["requested_base"] = "bookmarks(exact:\"release\")"

        self.assertNotIn("--base", _retry_arguments(omitted, TASK_ID))
        explicit_arguments = _retry_arguments(explicit, TASK_ID)
        base_index = explicit_arguments.index("--base")
        self.assertEqual(
            explicit_arguments[base_index + 1], 'bookmarks(exact:"release")',
        )

    def test_worker_exit_classification_uses_terminal_journal_boundary(self) -> None:
        tasks = mock.Mock()
        tasks.transaction_lock.return_value = contextlib.nullcontext()
        tasks.read.side_effect = StoreError(f"task not found: {TASK_ID}")
        journals = mock.Mock()
        journals.read.side_effect = JournalError(
            f"creation journal not found: {TASK_ID}"
        )
        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), \
                mock.patch(
                    "lib.control.tui.CreationJournalStore", return_value=journals,
                ):
            self.assertEqual(
                _classify_start_worker_exit(
                    self.config, TASK_ID, 130, b"", b"", cancelled=True,
                ),
                "task start cancelled",
            )
        tasks.transaction_lock.assert_called_once_with(TASK_ID)

        tasks.read.side_effect = None
        tasks.read.return_value = {
            "task_id": TASK_ID,
            "slug": "race",
            "tmux": {"socket": "default", "session": "asha-race"},
            "runs": [],
        }
        journals.read.side_effect = None
        journals.read.return_value = {
            "phase": "launch-attempted", "launch_attempted": True,
        }
        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), \
                mock.patch(
                    "lib.control.tui.CreationJournalStore", return_value=journals,
                ):
            message = _classify_start_worker_exit(
                self.config, TASK_ID, 130, b"", b"", cancelled=True,
            )
        self.assertNotIn("cancelled", message)
        self.assertIn("resources preserved", message)
        self.assertIn(f"asha task recover {TASK_ID}", message)

    def test_cancel_during_colocation_reports_retained_partial_source_enablement(self) -> None:
        source = self.root / "source"
        (source / ".jj").mkdir(parents=True)
        tasks = mock.Mock()
        tasks.transaction_lock.return_value = contextlib.nullcontext()
        tasks.read.side_effect = StoreError(f"task not found: {TASK_ID}")
        journals = mock.Mock()
        journals.read.side_effect = JournalError(
            f"creation journal not found: {TASK_ID}"
        )
        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), \
                mock.patch(
                    "lib.control.tui.CreationJournalStore", return_value=journals,
                ):
            message = _classify_start_worker_exit(
                self.config, TASK_ID, 130, b"", b"", cancelled=True,
                source=source, source_was_plain_git=True,
            )

        self.assertIn("task start cancelled", message)
        self.assertIn("repository enablement", message)
        self.assertIn("retained", message)
        self.assertIn("jj status", message)

    def test_cancelled_partial_v2_creation_reports_manual_inspection_not_prune(self) -> None:
        tasks = mock.Mock()
        tasks.transaction_lock.return_value = contextlib.nullcontext()
        tasks.read.return_value = {
            "task_id": TASK_ID, "slug": "retained", "runs": [],
        }
        journals = mock.Mock()
        journals.read.return_value = {
            "contract": "asha.control-creation-journal.v2",
            "phase": "preserved", "launch_attempted": False,
            "repository": {"root": str(self.root / "source")},
            "workspace": {
                "path": str(self.root / "partial-workspace"),
                "root_fact": None,
                "created_parents": [{"path": str(self.root / "partial-workspace")}],
            },
            "context_owned": {},
        }

        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), \
                mock.patch(
                    "lib.control.tui.CreationJournalStore", return_value=journals,
                ):
            message = _classify_start_worker_exit(
                self.config, TASK_ID, 130, b"", b"", cancelled=True,
            )

        self.assertIn("task start cancelled", message)
        self.assertIn("workspace retained", message)
        self.assertIn("manual inspection and cleanup required", message)
        self.assertIn("workspace list", message)
        self.assertNotIn("task prune", message)
        self.assertNotIn("launch may have occurred", message)

    def test_cancelled_fully_bound_v2_creation_reports_archive_prune_cleanup(self) -> None:
        tasks = mock.Mock()
        tasks.transaction_lock.return_value = contextlib.nullcontext()
        tasks.read.return_value = {
            "task_id": TASK_ID, "slug": "retained", "runs": [],
        }
        journals = mock.Mock()
        journals.read.return_value = {
            "contract": "asha.control-creation-journal.v2",
            "phase": "preserved", "launch_attempted": False,
            "repository": {"root": str(self.root / "source")},
            "workspace": {
                "path": str(self.root / "bound-workspace"),
                "root_fact": {"dev": 1, "ino": 2, "mode": 0o700, "uid": os.geteuid()},
                "created_parents": [],
            },
            "context_owned": {
                ".asha/control-task.json": {
                    "dev": 1, "ino": 3, "mode": 0o600, "uid": os.geteuid(),
                },
            },
        }

        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), \
                mock.patch(
                    "lib.control.tui.CreationJournalStore", return_value=journals,
                ):
            message = _classify_start_worker_exit(
                self.config, TASK_ID, 130, b"", b"", cancelled=True,
            )

        self.assertIn("task start cancelled", message)
        self.assertIn("workspace retained", message)
        self.assertIn(f"asha task archive {TASK_ID}", message)
        self.assertIn(f"asha task prune {TASK_ID} --yes", message)
        self.assertNotIn("launch may have occurred", message)

    def test_reaped_worker_does_not_wait_for_descendant_inherited_pipes(self) -> None:
        pid_file = self.root / "descendant.pid"
        payload = {
            "contract": "asha.control-task-start.v1",
            "task": {
                "task_id": TASK_ID, "slug": "done",
                "jj": {"base_commit_id": "b" * 40},
            },
        }
        script = (
            "import json,subprocess,sys; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); "
            f"open({str(pid_file)!r},'w').write(str(p.pid)); "
            f"print(json.dumps({payload!r}))"
        )
        started = time.monotonic()
        try:
            message = _supervise_start_process(
                ProgressScreen([]), FakeCurses(), TuiModel([]), self.config,
                [sys.executable, "-c", script], TASK_ID, self.env,
            )
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("task started", message)

    def test_exceptional_cleanup_wait_is_finite_and_signals_group_once(self) -> None:
        class ExplodingScreen(ProgressScreen):
            def getch(self):
                deadline = time.monotonic() + 1
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise RuntimeError("synthetic curses failure")

        pid_file = self.root / "leader.pid"
        script = (
            "import os,signal,time; "
            f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
            "signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(10)"
        )
        real_killpg = os.killpg
        calls: list[tuple[int, int]] = []

        def signal_once(pid, signum):
            calls.append((pid, signum))
            real_killpg(pid, signum)

        started = time.monotonic()
        try:
            with mock.patch("lib.control.tui.os.killpg", side_effect=signal_once), \
                    self.assertRaisesRegex(ValueError, "termination unconfirmed"):
                _supervise_start_process(
                    ExplodingScreen([]), FakeCurses(), TuiModel([]), self.config,
                    [sys.executable, "-c", script], TASK_ID, self.env,
                )
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    _reap_deferred_start_workers()
                    time.sleep(0.01)

        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(calls), 1)

    def test_signal_shutdown_carries_recovery_when_worker_termination_is_unconfirmed(self) -> None:
        class ShutdownScreen(ProgressScreen):
            def getch(self):
                deadline = time.monotonic() + 1
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise _TuiShutdown(signal.SIGHUP)

        pid_file = self.root / "shutdown-leader.pid"
        script = (
            "import os,signal,time; "
            f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
            "signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(10)"
        )
        try:
            with self.assertRaises(_TuiShutdown) as raised:
                _supervise_start_process(
                    ShutdownScreen([]), FakeCurses(), TuiModel([]), self.config,
                    [sys.executable, "-c", script], TASK_ID, self.env,
                )
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    _reap_deferred_start_workers()
                    time.sleep(0.01)

        self.assertIn("termination unconfirmed", str(raised.exception))
        self.assertIn(f"asha task recover {TASK_ID}", str(raised.exception))


class GitColocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.source)], check=True,
        )
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / "mixed.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "tracked.txt", "mixed.txt")
        self.git("commit", "-qm", "base")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.source), *args], check=check,
            capture_output=True, env=self.git_env,
        )

    def semantic_git_state(self) -> tuple[bytes, ...]:
        values = [self.git(*args).stdout for args in (
            ("rev-parse", "HEAD"),
            ("symbolic-ref", "HEAD"),
            ("status", "--porcelain=v2", "-z", "--untracked-files=all"),
            ("ls-files", "--stage", "-z"),
            ("diff", "--binary", "--no-ext-diff"),
            ("diff", "--cached", "--binary", "--no-ext-diff"),
            ("for-each-ref", "--format=%(refname)%00%(objectname)"),
        )]
        values[-1] = b"\n".join(
            line for line in values[-1].splitlines()
            if not line.startswith(b"refs/jj/")
        ) + b"\n"
        return tuple(values)

    def test_plain_git_discovery_is_read_only_and_finds_exact_root(self) -> None:
        nested = self.source / "nested"
        nested.mkdir()
        before = self.semantic_git_state()

        discovered = discover_git_root(nested.resolve())

        self.assertEqual(discovered, self.source.resolve())
        self.assertEqual(self.semantic_git_state(), before)
        self.assertFalse((self.source / ".jj").exists())

    def test_exact_git_base_resolution_ignores_inherited_repository_selection(self) -> None:
        foreign = self.root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(foreign)], check=True)
        (foreign / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(foreign), "add", "foreign.txt"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(foreign), "commit", "-qm", "foreign"],
            check=True, env=self.git_env,
        )
        expected = self.git("rev-parse", "HEAD").stdout.decode().strip()
        foreign_head = subprocess.run(
            ["git", "-C", str(foreign), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertNotEqual(expected, foreign_head)

        poisoned = {
            "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign),
            "GIT_OBJECT_DIRECTORY": str(foreign / ".git" / "objects"),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
        }
        with mock.patch.dict(os.environ, poisoned, clear=False):
            observed = JjAdapter().resolve_git_commit(self.source.resolve(), "main")

        self.assertEqual(observed, expected)

    def test_exact_git_base_resolution_refuses_invalid_explicit_text(self) -> None:
        with self.assertRaisesRegex(JjError, "explicit base.*Git commit"):
            JjAdapter().resolve_git_commit(self.source.resolve(), "missing | main")

    def test_exact_git_exec_ignores_hostile_path_and_loader_environment(self) -> None:
        hostile = self.root / "hostile-bin"
        hostile.mkdir()
        sentinel = self.root / "hostile-executed"
        for name in ("env", "git"):
            executable = hostile / name
            executable.write_text(
                f"#!/bin/sh\nprintf x > {sentinel}\nprintf '{'a' * 40}\\n'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        observed: list[tuple[list[str], dict[str, str]]] = []

        def runner(argv, **kwargs):
            observed.append((list(argv), dict(kwargs["env"])))
            return subprocess.run(argv, **kwargs)

        expected = self.git("rev-parse", "HEAD").stdout.decode().strip()
        with mock.patch.dict(os.environ, {
            "PATH": str(hostile),
            "LD_PRELOAD": str(self.root / "hostile-loader.so"),
            "DYLD_INSERT_LIBRARIES": str(self.root / "hostile-dyld.dylib"),
            "GIT_DIR": str(self.root / "foreign.git"),
        }, clear=False):
            actual = JjAdapter(runner=runner).resolve_git_commit(self.source, "HEAD")

        self.assertEqual(actual, expected)
        self.assertFalse(sentinel.exists())
        self.assertTrue(observed)
        argv, child_env = observed[0]
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertNotIn("LD_PRELOAD", child_env)
        self.assertNotIn("DYLD_INSERT_LIBRARIES", child_env)
        self.assertNotIn("GIT_DIR", child_env)

    def test_semantic_snapshot_disables_repository_fsmonitor_execution(self) -> None:
        sentinel = self.root / "fsmonitor-executed"
        monitor = self.root / "fsmonitor"
        monitor.write_text(
            f"#!/bin/sh\nprintf x > {sentinel}\nprintf '\\n'\n",
            encoding="utf-8",
        )
        monitor.chmod(0o755)
        self.git("config", "core.fsmonitor", str(monitor))

        JjAdapter()._git_semantic_state(self.source, include_jj_refs=True)

        self.assertFalse(sentinel.exists())

    def test_semantic_snapshot_never_executes_repository_filters(self) -> None:
        sentinel = self.root / "filter-executed"
        filter_program = self.root / "filter-program"
        filter_program.write_text(
            f"#!/bin/sh\nprintf x > {sentinel}\ncat\n",
            encoding="utf-8",
        )
        filter_program.chmod(0o755)
        (self.source / ".gitattributes").write_text(
            "tracked.txt filter=hostile\n", encoding="utf-8",
        )
        self.git("add", ".gitattributes")
        self.git("commit", "-qm", "attributes")
        self.git("config", "filter.hostile.clean", str(filter_program))
        self.git("config", "filter.hostile.smudge", str(filter_program))
        self.git("config", "filter.hostile.process", str(filter_program))
        (self.source / "tracked.txt").write_text("dirty one\n", encoding="utf-8")
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            return subprocess.run(argv, **kwargs)

        adapter = JjAdapter(runner=runner)
        before = adapter._git_semantic_state(self.source, include_jj_refs=True)
        (self.source / "tracked.txt").write_text("dirty two\n", encoding="utf-8")
        after = adapter._git_semantic_state(self.source, include_jj_refs=True)

        self.assertNotEqual(before, after)
        self.assertFalse(sentinel.exists())
        self.assertFalse(any("status" in call or "diff" in call for call in calls))

    def test_plumbing_snapshot_binds_all_operator_visible_git_planes(self) -> None:
        (self.source / "deleted.txt").write_text("delete me\n", encoding="utf-8")
        (self.source / "mode.txt").write_text("mode\n", encoding="utf-8")
        os.symlink("tracked.txt", self.source / "linked.txt")
        self.git("add", "deleted.txt", "mode.txt", "linked.txt")
        self.git("commit", "-qm", "more tracked forms")
        self.git("update-ref", "refs/jj/keep", "HEAD")
        self.git("update-ref", "refs/heads/evidence", "HEAD")
        adapter = JjAdapter()
        before = adapter._git_semantic_state(self.source, include_jj_refs=True)

        (self.source / "mixed.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "mixed.txt")
        (self.source / "mixed.txt").write_text("working after stage\n", encoding="utf-8")
        (self.source / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
        (self.source / "deleted.txt").unlink()
        (self.source / "linked.txt").unlink()
        os.symlink("mixed.txt", self.source / "linked.txt")
        os.chmod(self.source / "mode.txt", 0o755)
        (self.source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        self.git("update-ref", "refs/jj/keep", "HEAD^")
        self.git("update-ref", "refs/heads/evidence", "HEAD^")

        after = adapter._git_semantic_state(self.source, include_jj_refs=True)

        self.assertNotEqual(before.index, after.index)
        self.assertNotEqual(before.tracked_paths, after.tracked_paths)
        self.assertNotEqual(before.tracked_modes, after.tracked_modes)
        self.assertNotEqual(before.paths, after.paths)
        self.assertNotEqual(before.refs, after.refs)

    def test_semantic_snapshot_binds_extended_index_flags(self) -> None:
        adapter = JjAdapter()
        ordinary = adapter._git_semantic_state(self.source, include_jj_refs=True)

        self.git("update-index", "--skip-worktree", "tracked.txt")
        skipped = adapter._git_semantic_state(self.source, include_jj_refs=True)
        self.assertNotEqual(skipped, ordinary)
        self.assertNotEqual(skipped.index_flags, ordinary.index_flags)

        self.git("update-index", "--no-skip-worktree", "tracked.txt")
        self.git("update-index", "--assume-unchanged", "tracked.txt")
        assumed = adapter._git_semantic_state(self.source, include_jj_refs=True)
        self.assertNotEqual(assumed, ordinary)
        self.assertNotEqual(assumed.index_flags, ordinary.index_flags)

        self.git("update-index", "--no-assume-unchanged", "tracked.txt")
        intent_path = self.source / "intent.txt"
        intent_path.write_text("intent\n", encoding="utf-8")
        self.git("add", "--intent-to-add", "intent.txt")
        intent = adapter._git_semantic_state(self.source, include_jj_refs=True)
        intent_flags = [
            flags for path, stage, flags in intent.index_flags
            if path == "intent.txt" and stage == 0
        ]
        self.assertEqual(len(intent_flags), 1)
        self.assertNotEqual(intent_flags[0], 0)

    def test_semantic_snapshot_refuses_noncanonical_unbounded_index_numbers(self) -> None:
        staged = self.git("ls-files", "--stage", "-z").stdout
        malformed = b"".join(
            record + b"\0"
            + b"  ctime: 999999999999999999999:0\n"
            + b"  mtime: 0:0\n"
            + b"  dev: 0\tino: 0\n"
            + b"  uid: 0\tgid: 0\n"
            + b"  size: 0\tflags: 0\n"
            for record in staged.split(b"\0") if record
        )
        adapter = JjAdapter()
        exact = adapter._exact_git_bytes

        def injected(root, args, *, limit):
            if args == ["ls-files", "--stage", "--debug", "-z"]:
                return malformed
            return exact(root, args, limit=limit)

        with mock.patch.object(adapter, "_exact_git_bytes", side_effect=injected), \
                self.assertRaisesRegex(JjError, "bounded index-cache"):
            adapter._git_semantic_state(self.source, include_jj_refs=True)

    def test_semantic_snapshot_handles_sparse_and_conflicted_index_entries(self) -> None:
        sparse_root = self.source / "sparse"
        sparse_root.mkdir()
        other_root = self.source / "other-tree"
        other_root.mkdir()
        (sparse_root / "inside.txt").write_text("inside\n", encoding="utf-8")
        (other_root / "outside.txt").write_text("outside\n", encoding="utf-8")
        self.git("add", "sparse/inside.txt", "other-tree/outside.txt")
        self.git("commit", "-qm", "sparse paths")
        self.git("sparse-checkout", "init", "--cone")
        self.git("sparse-checkout", "set", "sparse")

        sparse = JjAdapter()._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        outside = [
            flags for path, stage, flags in sparse.index_flags
            if path == "other-tree/outside.txt" and stage == 0
        ]
        self.assertEqual(len(outside), 1)
        self.assertNotEqual(outside[0], 0)
        self.assertTrue(any(
            path == "other-tree/outside.txt" and identity == "missing"
            for path, _mode, _kind, _size, identity in sparse.tracked_paths
        ))

        self.git("sparse-checkout", "disable")
        self.git("branch", "other")
        (self.source / "tracked.txt").write_text("main\n", encoding="utf-8")
        self.git("commit", "-qam", "main side")
        self.git("checkout", "-q", "other")
        (self.source / "tracked.txt").write_text("other\n", encoding="utf-8")
        self.git("commit", "-qam", "other side")
        self.git("checkout", "-q", "main")
        self.git("merge", "other", check=False)

        conflicted = JjAdapter()._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        conflict_stages = sorted(
            stage for path, stage, _flags in conflicted.index_flags
            if path == "tracked.txt"
        )
        self.assertEqual(conflict_stages, [1, 2, 3])

    def test_semantic_snapshot_binds_same_oid_symbolic_ref_target(self) -> None:
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("update-ref", "refs/remotes/origin/release", "HEAD")
        self.git(
            "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        )
        adapter = JjAdapter()
        before = adapter._git_semantic_state(self.source, include_jj_refs=True)

        self.git(
            "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/release",
        )
        after = adapter._git_semantic_state(self.source, include_jj_refs=True)

        self.assertNotEqual(after.refs, before.refs)
        self.assertTrue(any(
            record.startswith(b"refs/remotes/origin/HEAD\0")
            and record.endswith(b"\0refs/remotes/origin/release")
            for record in after.refs
        ))

    def test_semantic_snapshot_accepts_sha256_index_flags_and_refs(self) -> None:
        source = self.root / "sha256-source"
        initialized = subprocess.run([
            "git", "init", "-q", "--object-format=sha256", "-b", "main",
            str(source),
        ], capture_output=True)
        if initialized.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        tracked = source / "tracked.txt"
        tracked.write_text("sha256\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(source), "add", "tracked.txt"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(source), "commit", "-qm", "base"],
            check=True, env=self.git_env,
        )
        adapter = JjAdapter()
        ordinary = adapter._git_semantic_state(source.resolve(), include_jj_refs=True)
        subprocess.run([
            "git", "-C", str(source), "update-index", "--skip-worktree",
            "tracked.txt",
        ], check=True)
        skipped = adapter._git_semantic_state(source.resolve(), include_jj_refs=True)

        oid = subprocess.run([
            "git", "-C", str(source), "rev-parse", "HEAD",
        ], check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(len(oid), 64)
        index_oid = ordinary.index.split(b" ", 2)[1]
        self.assertEqual(len(index_oid), 64)
        self.assertTrue(any(oid.encode("ascii") in ref for ref in ordinary.refs))
        self.assertNotEqual(skipped.index_flags, ordinary.index_flags)

    def test_init_refuses_and_retains_index_flag_or_symref_mutation(self) -> None:
        if shutil.which("jj") is None:
            self.skipTest("jj is required")

        for mutation in ("skip-worktree", "symbolic-ref"):
            with self.subTest(mutation=mutation):
                source = self.root / f"boundary-{mutation}"
                source.mkdir()
                subprocess.run(
                    ["git", "init", "-q", "-b", "main", str(source)], check=True,
                )
                tracked = source / "tracked.txt"
                tracked.write_text("preserve me\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(source), "add", "tracked.txt"],
                    check=True, env=self.git_env,
                )
                subprocess.run(
                    ["git", "-C", str(source), "commit", "-qm", "base"],
                    check=True, env=self.git_env,
                )
                if mutation == "symbolic-ref":
                    for name in ("main", "release"):
                        subprocess.run([
                            "git", "-C", str(source), "update-ref",
                            f"refs/remotes/origin/{name}", "HEAD",
                        ], check=True)
                    subprocess.run([
                        "git", "-C", str(source), "symbolic-ref",
                        "refs/remotes/origin/HEAD", "refs/remotes/origin/main",
                    ], check=True)
                injected = False

                def runner(argv, **kwargs):
                    nonlocal injected
                    if not injected and Path(argv[0]).name == "jj" and "init" in argv:
                        injected = True
                        if mutation == "skip-worktree":
                            command = [
                                "git", "-C", str(source), "update-index",
                                "--skip-worktree", "tracked.txt",
                            ]
                        else:
                            command = [
                                "git", "-C", str(source), "symbolic-ref",
                                "refs/remotes/origin/HEAD",
                                "refs/remotes/origin/release",
                            ]
                        subprocess.run(command, check=True)
                    return subprocess.run(argv, **kwargs)

                with self.assertRaisesRegex(
                    JjError, "changed semantic Git or working-tree state",
                ):
                    JjAdapter(runner=runner).init_colocated(source.resolve())

                self.assertTrue(injected)
                self.assertTrue((source / ".jj").is_dir())
                self.assertEqual(tracked.read_text(encoding="utf-8"), "preserve me\n")
                if mutation == "skip-worktree":
                    listing = subprocess.run([
                        "git", "-C", str(source), "ls-files", "-v", "tracked.txt",
                    ], check=True, capture_output=True, text=True).stdout
                    self.assertEqual(listing[:2], "S ")
                else:
                    target = subprocess.run([
                        "git", "-C", str(source), "symbolic-ref",
                        "refs/remotes/origin/HEAD",
                    ], check=True, capture_output=True, text=True).stdout.strip()
                    self.assertEqual(target, "refs/remotes/origin/release")

    def test_exact_semantic_reauthentication_reads_ignore_inherited_git_selection(self) -> None:
        expected = JjAdapter()._git_semantic_state(
            self.source.resolve(), include_jj_refs=True,
        )
        foreign = self.root / "semantic-foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        poisoned = {
            "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
        }
        with mock.patch.dict(os.environ, poisoned, clear=False):
            observed = JjAdapter()._git_semantic_state(
                self.source.resolve(), include_jj_refs=True,
            )
        self.assertEqual(observed, expected)

    def test_init_uses_no_auto_track_and_preserves_staged_unstaged_untracked_state(self) -> None:
        if shutil.which("jj") is None:
            self.skipTest("jj is required")
        (self.source / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
        (self.source / "mixed.txt").write_text("staged\n", encoding="utf-8")
        self.git("add", "mixed.txt")
        (self.source / "mixed.txt").write_text("working\n", encoding="utf-8")
        (self.source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        os.chmod(self.source / "untracked.txt", 0o600)
        before = self.semantic_git_state()
        bytes_before = {
            path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in self.source.iterdir() if path.is_file()
        }

        mutation = JjAdapter().init_colocated(self.source.resolve())

        self.assertEqual(mutation["kind"], "jj-operation")
        self.assertEqual(mutation["operation"], "git init --colocate")
        self.assertIn("retained", mutation["detail"])
        self.assertTrue((self.source / ".jj").is_dir())
        self.assertEqual(self.semantic_git_state(), before)
        self.assertEqual({
            path.name: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in self.source.iterdir() if path.is_file()
        }, bytes_before)
        self.assertNotIn(b"untracked.txt", self.git("ls-files", "-z").stdout)

    def test_init_argv_is_exact_and_failed_partial_is_retained(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            (self.source / ".jj").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(argv, 2, b"", b"synthetic failure")

        adapter = JjAdapter(runner=runner)
        state = object()
        with mock.patch.object(adapter, "_git_semantic_state", return_value=state):
            with self.assertRaisesRegex(JjError, "retained"):
                adapter.init_colocated(self.source.resolve())

        self.assertEqual(calls, [[
            "jj", "--config", 'snapshot.auto-track="none()"',
            "git", "init", "--colocate", str(self.source.resolve()),
        ]])
        self.assertTrue((self.source / ".jj").is_dir())

    def test_semantic_snapshot_does_not_hash_clean_tracked_tree(self) -> None:
        large_tracked = self.source / "large-tracked.bin"
        with large_tracked.open("wb") as stream:
            stream.truncate(65 * 1024 * 1024)
        self.git("add", "large-tracked.bin")
        self.git("commit", "-qm", "large clean tracked file")
        real_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            if Path(path) == large_tracked:
                raise AssertionError("clean tracked content must not be opened or hashed")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("lib.control.jj.os.open", side_effect=guarded_open):
            state = JjAdapter()._git_semantic_state(self.source)

        self.assertEqual(state.paths, ())
        self.assertTrue(any(path[0] == "large-tracked.bin" for path in state.tracked_paths))

    def test_semantic_snapshot_detects_tracked_posix_mode_change(self) -> None:
        tracked = self.source / "tracked.txt"
        os.chmod(tracked, 0o600)
        adapter = JjAdapter()
        before = adapter._git_semantic_state(self.source)
        os.chmod(tracked, 0o644)
        after = adapter._git_semantic_state(self.source)

        self.assertNotEqual(before, after)
        self.assertNotEqual(before.tracked_modes, after.tracked_modes)

    def test_adapter_preserves_keyboard_interrupt_for_cli_diagnostic(self) -> None:
        def interrupted(argv, **_kwargs):
            (self.source / ".jj").mkdir(exist_ok=True)
            raise KeyboardInterrupt

        adapter = JjAdapter(runner=interrupted)
        state = object()
        with mock.patch.object(adapter, "_git_semantic_state", return_value=state), \
                self.assertRaises(KeyboardInterrupt):
            adapter.init_colocated(self.source)

    def test_linked_worktree_is_refused_but_real_submodule_gitdir_is_supported(self) -> None:
        linked = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "linked-test", str(linked))

        with self.assertRaisesRegex(
            JjError, "linked Git worktree.*primary worktree.*manual",
        ):
            discover_git_root(linked.resolve())

        module_remote = self.root / "module-remote"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(module_remote)], check=True,
        )
        (module_remote / "module.txt").write_text("module\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(module_remote), "add", "module.txt"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(module_remote), "commit", "-qm", "module"],
            check=True, env=self.git_env,
        )
        self.git(
            "-c", "protocol.file.allow=always", "submodule", "add", "-q",
            str(module_remote), "module",
        )
        module = (self.source / "module").resolve()

        self.assertTrue((module / ".git").is_file())
        self.assertEqual(discover_git_root(module), module)
        if shutil.which("jj") is not None:
            mutation = JjAdapter().init_colocated(module)
            self.assertEqual(mutation["operation"], "git init --colocate")
            self.assertTrue((module / ".jj").is_dir())


class PlainGitPreEnableTests(unittest.TestCase):
    def test_start_parser_tracks_base_explicitness_without_changing_default(self) -> None:
        omitted = _parse_start(["--goal", "x"])
        explicit = _parse_start(["--base", omitted["base"], "--goal", "x"])
        self.assertFalse(omitted["base_explicit"])
        self.assertTrue(explicit["base_explicit"])
        self.assertEqual(explicit["base"], omitted["base"])

    def test_expected_default_is_a_private_omitted_base_race_assertion(self) -> None:
        oid = "a" * 40
        parsed = _parse_start(["--expected-default", oid, "--goal", "x"])
        self.assertEqual(parsed["expected_default"], oid)
        self.assertFalse(parsed["base_explicit"])
        for conflicting in (("--base", "main"), ("--pr", "7")):
            with self.subTest(conflicting=conflicting), self.assertRaisesRegex(
                ValueError, "--expected-default",
            ):
                _parse_start([
                    "--expected-default", oid, *conflicting, "--goal", "x",
                ])
        with self.assertRaisesRegex(ValueError, "full Git object ID"):
            _parse_start(["--expected-default", "short", "--goal", "x"])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o755)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)], check=True)
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / ".gitignore").write_text(
            "/.asha/\n/Memory/\n/Work/\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.source), "add", "tracked.txt", ".gitignore"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=self.git_env,
        )
        self.config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        self.write_memory()

    def write_memory(self) -> None:
        (self.source / ".asha").mkdir(exist_ok=True)
        (self.source / "Memory").mkdir(exist_ok=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "pre-enable",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )

    def request(self, *, base: str = "main", slug: str = "pre-enable") -> PrepareRequest:
        return PrepareRequest(
            repository=self.source, requested_base=base, task_id=TASK_ID,
            slug=slug, label="Pre-enable", source={
                "kind": "ad-hoc", "number": None, "url": None,
            },
        )

    @staticmethod
    def context_proof_paths() -> dict[str, tuple[str, ...]]:
        return {
            "planned_context_paths": (
                ".asha/config.json", ".asha/control-task.json",
                "Memory/activeContext.md", "Memory/decisions.md",
            ),
            "private_directory_paths": ("Work/session-state/",),
        }

    def test_pre_enable_resolves_explicit_base_and_plans_destination_without_mutation(self) -> None:
        adapter = JjAdapter()
        before = adapter._git_semantic_state(self.source)

        plan = preflight_plain_git_enablement(
            self.config, self.request(), jj=adapter, base_explicit=True,
        )

        expected = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(plan.resolved_base_commit_id, expected)
        self.assertEqual(plan.destination.name, "pre-enable")
        self.assertEqual(adapter._git_semantic_state(self.source), before)
        self.assertFalse((self.source / ".jj").exists())

    def _commit_context_base(self, ignore: str, *tracked: str) -> str:
        (self.source / ".gitignore").write_text(ignore, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "-f", ".gitignore", *tracked],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "context base"],
            check=True, env=self.git_env,
        )
        return subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_immutable_context_proof_reuses_tracked_memory_without_ignore(self) -> None:
        base = self._commit_context_base(
            "/.asha/\n/Work/\n",
            "Memory/activeContext.md", "Memory/decisions.md",
        )
        adapter = JjAdapter()
        plan = adapter.materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )
        # The mutable checkout is not an authority for the selected base.
        (self.source / ".gitignore").write_text(
            "!/.asha/control-task.json\n!/Work/session-state/.asha-control-probe\n",
            encoding="utf-8",
        )

        proof = adapter.prove_context_compatibility(
            self.source, self.source / ".git", plan, project_id="pre-enable",
            **self.context_proof_paths(),
        )

        self.assertEqual(
            proof.reused_paths,
            ("Memory/activeContext.md", "Memory/decisions.md"),
        )
        self.assertEqual(
            proof.required_ignored_paths,
            (".asha/config.json", ".asha/control-task.json",
             "Work/session-state/"),
        )

    def test_immutable_context_proof_honors_nested_rules_spaces_unicode_and_negation(self) -> None:
        (self.source / "Memory" / ".gitignore").write_text(
            "/decisions.md\n", encoding="utf-8",
        )
        base = self._commit_context_base(
            "/.asha/\n/Work/\n/private space/\n/\u03bc/\n",
            "Memory/.gitignore", "Memory/activeContext.md",
        )
        adapter = JjAdapter()
        plan = adapter.materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )

        proof = adapter.prove_context_compatibility(
            self.source, self.source / ".git", plan, project_id="pre-enable",
            **self.context_proof_paths(),
        )
        self.assertIn("Memory/decisions.md", proof.required_ignored_paths)

        subprocess.run(
            ["git", "-C", str(self.source), "checkout", "--", ".gitignore"],
            check=True,
        )
        (self.source / ".gitignore").write_text(
            "/.asha/*\n!/.asha/control-task.json\n/Work/\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.source), "add", ".gitignore"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "negated marker"],
            check=True, env=self.git_env,
        )
        negated_base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        negated_plan = adapter.materialization_plan(
            self.source / ".git", negated_base, exact_root=self.source,
        )
        with self.assertRaisesRegex(JjError, "not positively ignored.*control-task"):
            adapter.prove_context_compatibility(
                self.source, self.source / ".git", negated_plan,
                project_id="pre-enable",
                **self.context_proof_paths(),
            )

    def test_immutable_context_proof_refuses_a_magic_session_sentinel_only_rule(self) -> None:
        base = self._commit_context_base(
            "/.asha/config.json\n/.asha/control-task.json\n"
            "/Work/session-state/.asha-control-probe\n",
            "Memory/activeContext.md", "Memory/decisions.md",
        )
        adapter = JjAdapter()
        plan = adapter.materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )

        with self.assertRaisesRegex(JjError, "Work/session-state"):
            adapter.prove_context_compatibility(
                self.source, self.source / ".git", plan,
                project_id="pre-enable",
                **self.context_proof_paths(),
            )

    def test_immutable_context_proof_covers_each_exact_planned_private_path(self) -> None:
        base = self._commit_context_base(
            "/.asha/config.json\n/.asha/control-task.json\n/Work/session-state/\n",
            "Memory/activeContext.md", "Memory/decisions.md",
        )
        adapter = JjAdapter()
        plan = adapter.materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )
        paths = self.context_proof_paths()

        with self.assertRaisesRegex(JjError, "generated-extra"):
            adapter.prove_context_compatibility(
                self.source, self.source / ".git", plan,
                project_id="pre-enable",
                planned_context_paths=(
                    *paths["planned_context_paths"], ".asha/generated-extra.json",
                ),
                private_directory_paths=paths["private_directory_paths"],
            )

    def test_immutable_context_proof_ignores_host_default_global_excludes(self) -> None:
        base = self._commit_context_base(
            "/.asha/config.json\n/Work/session-state/\n",
            "Memory/activeContext.md", "Memory/decisions.md",
        )
        global_directory = self.home / ".config" / "git"
        global_directory.mkdir(parents=True)
        (global_directory / "ignore").write_text(
            "/.asha/control-task.json\n", encoding="utf-8",
        )
        adapter = JjAdapter()
        plan = adapter.materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )
        environment = adapter._exact_git_environment()
        environment["HOME"] = str(self.home)

        with mock.patch.object(
            JjAdapter, "_exact_git_environment", return_value=environment,
        ), self.assertRaisesRegex(JjError, "control-task"):
            adapter.prove_context_compatibility(
                self.source, self.source / ".git", plan,
                project_id="pre-enable",
                **self.context_proof_paths(),
            )

    def test_pre_enable_context_refusal_precedes_intent_workspace_and_task_state(self) -> None:
        self._commit_context_base(
            "/.asha/*\n!/.asha/control-task.json\n/Work/\n",
            "Memory/activeContext.md", "Memory/decisions.md",
        )

        with self.assertRaisesRegex(PreparationError, "not positively ignored"):
            preflight_plain_git_enablement(
                self.config, self.request(), jj=JjAdapter(), base_explicit=True,
            )

        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())
        self.assertFalse(self.config.workspace_root.exists())
        self.assertFalse(self.config.tasks_dir.exists())

    def test_pre_enable_refuses_writable_source_missing_memory_invalid_base_and_collision(self) -> None:
        self.source.chmod(0o775)
        with self.assertRaisesRegex(PreparationError, "writable non-sticky ancestor"):
            preflight_plain_git_enablement(
                self.config, self.request(), jj=JjAdapter(), base_explicit=True,
            )
        self.source.chmod(0o755)
        shutil.rmtree(self.source / "Memory")
        with self.assertRaisesRegex(PreparationError, "project config|Memory"):
            preflight_plain_git_enablement(
                self.config, self.request(), jj=JjAdapter(), base_explicit=True,
            )
        self.write_memory()
        with self.assertRaisesRegex(PreparationError, "explicit base.*Git commit"):
            preflight_plain_git_enablement(
                self.config, self.request(base="missing"),
                jj=JjAdapter(), base_explicit=True,
            )
        valid = preflight_plain_git_enablement(
            self.config, self.request(), jj=JjAdapter(), base_explicit=True,
        )
        valid.destination.mkdir(parents=True)
        current = valid.destination
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        with self.assertRaisesRegex(PreparationError, "destination already exists"):
            preflight_plain_git_enablement(
                self.config, self.request(), jj=JjAdapter(), base_explicit=True,
            )

        self.assertFalse((self.source / ".jj").exists())

    def test_pre_enable_refuses_existing_managed_parent_without_exact_private_mode(self) -> None:
        initial = preflight_plain_git_enablement(
            self.config, self.request(), jj=JjAdapter(), base_explicit=True,
        )
        initial.destination.parent.mkdir(parents=True, mode=0o700)
        current = initial.destination.parent
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        initial.destination.parent.chmod(0o755)

        with self.assertRaisesRegex(PreparationError, "parent.*mode 0700"):
            preflight_plain_git_enablement(
                self.config, self.request(), jj=JjAdapter(), base_explicit=True,
            )

        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())

    def test_omitted_default_resolves_conventional_main_before_colocation(self) -> None:
        plan = preflight_plain_git_enablement(
            self.config, self.request(base="ignored jj default"),
            jj=JjAdapter(), base_explicit=False,
        )
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(plan.selected_base, "refs/heads/main")
        self.assertEqual(plan.resolved_base_commit_id, head)
        self.assertFalse(plan.default_base_deferred)
        self.assertFalse((self.source / ".jj").exists())

    def test_omitted_default_resolves_any_attached_local_branch_without_mutation(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.source), "branch", "-m", "master"], check=True,
        )
        master = preflight_plain_git_enablement(
            self.config, self.request(base="ignored jj default"),
            jj=JjAdapter(), base_explicit=False,
        )
        self.assertEqual(master.selected_base, "refs/heads/master")

        subprocess.run(
            ["git", "-C", str(self.source), "branch", "-m", "dev"], check=True,
        )
        dev = preflight_plain_git_enablement(
            self.config, self.request(base="ignored jj default"),
            jj=JjAdapter(), base_explicit=False,
        )
        self.assertEqual(dev.selected_base, "refs/heads/dev")
        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())

    def test_existing_jj_omitted_preflight_uses_exact_git_default_not_jj_revset(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        real = JjAdapter()
        adapter = mock.Mock(spec=JjAdapter)
        adapter.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/main",), head, "attached-local",
        )
        adapter.materialization_plan.return_value = real.materialization_plan(
            self.source / ".git", head, exact_root=self.source,
        )

        plan = preflight_plain_git_enablement(
            self.config, self.request(base=DEFAULT_BASE_REVSET), jj=adapter,
            base_explicit=False, existing_jj=True,
        )

        self.assertEqual(plan.resolved_base_commit_id, head)
        self.assertEqual(plan.default_base_resolution, DefaultBaseResolution(
            ("refs/heads/main",), head, "attached-local",
        ))
        adapter.resolve_base.assert_not_called()

    def test_pre_enable_revalidation_refuses_a_default_ref_race(self) -> None:
        plan = preflight_plain_git_enablement(
            self.config, self.request(base=DEFAULT_BASE_REVSET), jj=JjAdapter(),
            base_explicit=False,
        )
        adapter = mock.Mock(spec=JjAdapter)
        adapter.resolve_default_base.return_value = DefaultBaseResolution(
            plan.default_base_resolution.references,
            "f" * 40,
            plan.default_base_resolution.tier,
        )

        with self.assertRaisesRegex(PreparationError, "default base changed"):
            revalidate_plain_git_pre_enable_plan(plan, jj=adapter)

    def test_omitted_default_prefers_one_unambiguous_remote_head(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run([
            "git", "-C", str(self.source), "update-ref",
            "refs/remotes/origin/release", head,
        ], check=True)
        subprocess.run([
            "git", "-C", str(self.source), "symbolic-ref",
            "refs/remotes/origin/HEAD", "refs/remotes/origin/release",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(self.source), "checkout", "--detach", "-q", head],
            check=True,
        )

        plan = preflight_plain_git_enablement(
            self.config, self.request(base="ignored jj default"),
            jj=JjAdapter(), base_explicit=False,
        )

        self.assertEqual(plan.selected_base, "refs/remotes/origin/release")
        self.assertEqual(plan.resolved_base_commit_id, head)

    def test_aas_shaped_stale_packed_origin_cannot_override_attached_local_tree(self) -> None:
        subprocess.run([
            "git", "-C", str(self.source), "add", "-f",
            ".asha/config.json", "Memory/activeContext.md", "Memory/decisions.md",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "current identity"],
            check=True, env=self.git_env,
        )
        current = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.source), "checkout", "--orphan", "legacy"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "rm", "-rf", "--cached", "."],
            check=True, capture_output=True,
        )
        (self.source / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "stale-project",
        }) + "\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "-f", ".asha/config.json",
             ".gitignore", "tracked.txt", "Memory/activeContext.md",
             "Memory/decisions.md"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "legacy identity"],
            check=True, env=self.git_env,
        )
        stale = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.source), "checkout", "-q", "main"], check=True,
        )
        subprocess.run([
            "git", "-C", str(self.source), "update-ref",
            "refs/remotes/origin/master", stale,
        ], check=True)
        subprocess.run([
            "git", "-C", str(self.source), "symbolic-ref",
            "refs/remotes/origin/HEAD", "refs/remotes/origin/master",
        ], check=True)
        subprocess.run(
            ["git", "-C", str(self.source), "pack-refs", "--all"], check=True,
        )
        subprocess.run([
            "git", "-C", str(self.source), "update-ref",
            "refs/remotes/keybase/master", current,
        ], check=True)
        subprocess.run([
            "git", "-C", str(self.source), "symbolic-ref",
            "refs/remotes/keybase/HEAD", "refs/remotes/keybase/master",
        ], check=True)

        plan = preflight_plain_git_enablement(
            self.config, self.request(base=DEFAULT_BASE_REVSET), jj=JjAdapter(),
            base_explicit=False,
        )

        self.assertEqual(plan.selected_base, "refs/heads/main")
        self.assertEqual(plan.resolved_base_commit_id, current)
        self.assertNotEqual(plan.resolved_base_commit_id, stale)

    def test_pre_enable_plan_binds_root_and_git_marker_against_path_replacement(self) -> None:
        plan = preflight_plain_git_enablement(
            self.config, self.request(), jj=JjAdapter(), base_explicit=True,
        )
        original = self.root / "source-a"
        replacement = self.root / "source-b"
        self.source.rename(original)
        replacement.mkdir(mode=0o755)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(replacement)], check=True,
        )
        replacement.rename(self.source)
        adapter = mock.Mock(spec=JjAdapter)

        with self.assertRaisesRegex(ValueError, "binding changed|pre-enable"):
            _ensure_colocated(
                adapter, RepositorySelection(self.source, plain_git=True),
                ColocationIntentStore(self.config), pre_enable_plan=plan,
            )

        adapter.init_colocated.assert_not_called()
        self.assertFalse((self.source / ".jj").exists())

    def test_source_swap_at_immediate_init_boundary_never_initializes_replacement(self) -> None:
        plan = preflight_plain_git_enablement(
            self.config, self.request(), jj=JjAdapter(), base_explicit=True,
        )
        replacement = self.root / "replacement-ready"
        replacement.mkdir(mode=0o755)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(replacement)], check=True,
        )
        authorized_away = self.root / "authorized-away"
        real_revalidate = __import__(
            "lib.control.prepare", fromlist=["revalidate_plain_git_pre_enable_plan"],
        ).revalidate_plain_git_pre_enable_plan
        calls = 0

        def swap_on_immediate_boundary(candidate, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.source.rename(authorized_away)
                replacement.rename(self.source)
            return real_revalidate(candidate)

        adapter = mock.Mock(spec=JjAdapter)
        with mock.patch(
            "lib.control.cli.revalidate_plain_git_pre_enable_plan",
            side_effect=swap_on_immediate_boundary,
        ), self.assertRaisesRegex(ValueError, "binding changed"):
            _ensure_colocated(
                adapter, RepositorySelection(self.source, plain_git=True),
                ColocationIntentStore(self.config), pre_enable_plan=plan,
            )

        self.assertEqual(calls, 3)
        adapter.init_colocated.assert_not_called()
        self.assertFalse((self.source / ".jj").exists())

    def test_source_swap_after_init_is_detected_before_verified_record(self) -> None:
        plan = preflight_plain_git_enablement(
            self.config, self.request(), jj=JjAdapter(), base_explicit=True,
        )
        replacement = self.root / "replacement-after"
        replacement.mkdir(mode=0o755)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(replacement)], check=True,
        )
        authorized_away = self.root / "authorized-after-away"
        real_revalidate = __import__(
            "lib.control.prepare", fromlist=["revalidate_plain_git_pre_enable_plan"],
        ).revalidate_plain_git_pre_enable_plan
        calls = 0

        def swap_after_init(candidate, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.source.rename(authorized_away)
                replacement.rename(self.source)
            return real_revalidate(candidate)

        adapter = mock.Mock(spec=JjAdapter)
        adapter.init_colocated.side_effect = lambda source, **_kwargs: (
            (source / ".jj").mkdir(),
            {"kind": "jj-operation", "operation": "git init --colocate", "detail": "x"},
        )[1]
        with mock.patch(
            "lib.control.cli.revalidate_plain_git_pre_enable_plan",
            side_effect=swap_after_init,
        ), self.assertRaisesRegex(ValueError, "binding changed"):
            _ensure_colocated(
                adapter, RepositorySelection(self.source, plain_git=True),
                ColocationIntentStore(self.config), pre_enable_plan=plan,
            )

        self.assertEqual(calls, 4)
        self.assertFalse((self.source / ".jj").exists())
        assessment = ColocationIntentStore(self.config).classify(self.source)
        self.assertEqual(assessment.kind, "mismatch")

    def test_later_sync_and_materialization_reads_use_exact_source_binding(self) -> None:
        adapter = mock.Mock(spec=JjAdapter)
        adapter.working_copy_parent.return_value = "a" * 40
        adapter.git_head_exact.return_value = "a" * 40
        repository = RepositoryFacts(self.source, self.source / ".git")

        _guard_colocated_sync(adapter, repository)

        adapter.git_head_exact.assert_called_once_with(self.source)
        adapter.git_head.assert_not_called()

        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        real_plan = JjAdapter().materialization_plan(
            self.source / ".git", head, exact_root=self.source,
        )
        prepared = mock.Mock(spec=JjAdapter)
        prepared.preflight.return_value = repository
        prepared.require_visible_commit.return_value = None
        prepared.pin_operation.return_value = "a" * 128

        def exact_plan(git_root, base_commit_id, *, exact_root=None):
            self.assertEqual(git_root, self.source / ".git")
            self.assertEqual(exact_root, self.source)
            raise JjError("stop after exact tree read")

        prepared.materialization_plan.side_effect = exact_plan
        with self.assertRaisesRegex(PreparationError, "exact tree read"):
            __import__(
                "lib.control.prepare", fromlist=["prepare_task_workspace"],
            ).prepare_task_workspace(
                self.config,
                PrepareRequest(
                    repository=self.source, requested_base="main", task_id=TASK_ID,
                    slug="exact-later", label="Exact later",
                    resolved_base_commit_id=head,
                ),
                jj=prepared,
            )

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_real_pr_without_remote_refuses_before_colocation(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        metadata = {
            "number": 7, "title": "No remote", "url": "https://example.test/o/r/pull/7",
            "headRefOid": head, "state": "OPEN", "isDraft": False,
            "isCrossRepository": False,
        }
        stderr = io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch.object(GithubAdapter, "preflight"), \
                mock.patch.object(GithubAdapter, "pr_metadata", return_value=metadata), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--pr", "7",
                "--task-id", TASK_ID, "--harness", "codex", "--goal", "No remote",
                "--detach", "--json",
            ], env={
                "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
                "ASHA_HOME": str(self.root / "asha"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            })
        self.assertEqual(status, 2)
        self.assertIn("remote", stderr.getvalue())
        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_real_pr_mismatched_sole_remote_refuses_before_colocation(self) -> None:
        subprocess.run([
            "git", "-C", str(self.source), "remote", "add", "origin",
            "https://example.test/foreign/repository.git",
        ], check=True)
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        metadata = {
            "number": 7, "title": "Wrong remote",
            "url": "https://example.test/o/r/pull/7", "headRefOid": head,
            "state": "OPEN", "isDraft": False, "isCrossRepository": False,
        }
        stderr = io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch.object(GithubAdapter, "preflight"), \
                mock.patch.object(GithubAdapter, "pr_metadata", return_value=metadata), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--pr", "7",
                "--task-id", TASK_ID, "--harness", "codex",
                "--goal", "Wrong remote", "--detach", "--json",
            ], env={
                "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
                "ASHA_HOME": str(self.root / "asha"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            })
        self.assertEqual(status, 2)
        self.assertIn("matching the pull request repository", stderr.getvalue())
        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())

    def test_pr_preflight_carries_selected_remote_and_metadata_without_reread(self) -> None:
        parsed = _parse_start([
            "--pr", "7", "--harness", "codex", "--goal", "Carry PR",
        ])
        head = "b" * 40
        metadata = {
            "number": 7, "title": "Carried title",
            "url": "https://example.test/o/r/pull/7", "headRefOid": head,
            "state": "OPEN", "isDraft": False, "isCrossRepository": False,
        }
        github = mock.Mock(spec=GithubAdapter)
        github.pr_metadata.return_value = metadata
        selected_remote = ValidatedPrRemote(
            "upstream", "https://example.test/o/r.git", "https", "c" * 64,
        )
        github.pr_remote.return_value = selected_remote
        adapter = mock.Mock(spec=JjAdapter)
        with mock.patch("lib.control.cli.GithubAdapter", return_value=github):
            request = _build_start_preflight_request(
                parsed, self.config, adapter, self.source, TASK_ID,
            )

        self.assertEqual(request.github_title, "Carried title")
        self.assertEqual(request.pr_remote, selected_remote)
        self.assertEqual(request.resolved_base_commit_id, head)
        github.pr_remote.assert_called_once_with(
            self.source, metadata["url"], 7, git=adapter,
        )

        repository = RepositoryFacts(self.source, self.source / ".git")
        adapter.preflight.return_value = repository
        adapter.working_copy_parent.return_value = head
        adapter.git_head_exact.return_value = head
        adapter.resolve_git_commit.return_value = head
        adapter.import_git.return_value = ()
        github.reset_mock()
        github.fetch_pr_head.return_value = ()
        prepared = {
            "tmux": {"socket": "default"},
            "jj": {"workspace_path": str(self.root / "workspace")},
        }
        with mock.patch("lib.control.cli.GithubAdapter", return_value=github), \
                mock.patch("lib.control.cli.prepare_task_workspace", return_value=prepared), \
                mock.patch("lib.control.cli.launch_task", return_value={}), \
                mock.patch("lib.control.cli.revalidate_pr_source_proof_after_fetch"), \
                mock.patch("lib.control.cli._emit_start_result", return_value=0):
            status = _start_new_task(
                parsed, {}, self.config, adapter, self.source, task_id=TASK_ID,
                selected_harness="codex", selected_role="implementer",
                preflight_request=request,
                pre_enable_plan=mock.sentinel.pre_enable_plan,
            )
        self.assertEqual(status, 0)
        github.preflight.assert_not_called()
        github.pr_metadata.assert_not_called()
        github.pr_remote.assert_not_called()
        github.fetch_pr_head.assert_called_once_with(
            self.source, selected_remote, 7, git=adapter,
        )

    def test_existing_jj_omitted_start_pins_default_before_git_import(self) -> None:
        parsed = _parse_start([
            "--harness", "codex", "--goal", "Pin current default",
        ])
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        events: list[str] = []
        adapter = mock.Mock(spec=JjAdapter)
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )
        adapter.working_copy_parent.return_value = head
        adapter.git_head_exact.return_value = head
        adapter.resolve_default_base.side_effect = lambda _root: (
            events.append("resolve") or DefaultBaseResolution(
                ("refs/heads/main",), head, "attached-local",
            )
        )
        adapter.import_git.side_effect = lambda _root: events.append("import") or ()
        prepared = {
            "tmux": {"socket": "default"},
            "jj": {"workspace_path": str(self.root / "workspace")},
        }
        captured: list[PrepareRequest] = []

        def prepare(_config, request, *, jj):
            events.append("prepare")
            captured.append(request)
            return prepared

        with mock.patch(
            "lib.control.cli.prepare_task_workspace", side_effect=prepare,
        ), mock.patch(
            "lib.control.cli.launch_task", return_value={},
        ), mock.patch(
            "lib.control.cli._emit_start_result", return_value=0,
        ):
            self.assertEqual(_start_new_task(
                parsed, {}, self.config, adapter, self.source, task_id=TASK_ID,
                selected_harness="codex", selected_role="implementer",
            ), 0)

        self.assertEqual(events, ["resolve", "import", "prepare"])
        self.assertEqual(captured[0].requested_base, DEFAULT_BASE_REVSET)
        self.assertEqual(captured[0].resolved_base_commit_id, head)
        adapter.resolve_base.assert_not_called()

    def test_preview_race_refuses_before_import(self) -> None:
        parsed = _parse_start([
            "--expected-default", "a" * 40,
            "--harness", "codex", "--goal", "Refuse raced default",
        ])
        adapter = mock.Mock(spec=JjAdapter)
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )
        adapter.working_copy_parent.return_value = "b" * 40
        adapter.git_head_exact.return_value = "b" * 40
        adapter.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/main",), "b" * 40, "attached-local",
        )

        with self.assertRaisesRegex(ValueError, "TUI preview"):
            _start_new_task(
                parsed, {}, self.config, adapter, self.source, task_id=TASK_ID,
                selected_harness="codex", selected_role="implementer",
            )

        adapter.import_git.assert_not_called()

    def test_explicit_legacy_default_text_remains_a_verbatim_jj_revset(self) -> None:
        parsed = _parse_start([
            "--base", DEFAULT_BASE_REVSET,
            "--harness", "codex", "--goal", "Explicit legacy expression",
        ])
        adapter = mock.Mock(spec=JjAdapter)
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )
        adapter.working_copy_parent.return_value = "a" * 40
        adapter.git_head_exact.return_value = "a" * 40
        adapter.import_git.return_value = ()
        adapter.resolve_default_base.side_effect = AssertionError(
            "an explicit revset must not enter omitted-base resolution"
        )
        captured: list[PrepareRequest] = []
        prepared = {
            "tmux": {"socket": "default"},
            "jj": {"workspace_path": str(self.root / "workspace")},
        }

        with mock.patch(
            "lib.control.cli.prepare_task_workspace",
            side_effect=lambda _config, request, **_kwargs: (
                captured.append(request) or prepared
            ),
        ), mock.patch(
            "lib.control.cli.launch_task", return_value={},
        ), mock.patch(
            "lib.control.cli._emit_start_result", return_value=0,
        ):
            self.assertEqual(_start_new_task(
                parsed, {}, self.config, adapter, self.source, task_id=TASK_ID,
                selected_harness="codex", selected_role="implementer",
            ), 0)

        self.assertEqual(captured[0].requested_base, DEFAULT_BASE_REVSET)
        self.assertIsNone(captured[0].resolved_base_commit_id)
        adapter.resolve_default_base.assert_not_called()

    def test_omitted_default_keeps_requested_identity_while_carrying_oid(self) -> None:
        parsed = _parse_start([
            "--harness", "codex", "--goal", "Stable omitted replay",
        ])
        request = _build_start_preflight_request(
            parsed, self.config, JjAdapter(), self.source, TASK_ID,
        )

        normalized, plan = _preflight_plain_git_start(
            parsed, self.config, JjAdapter(), self.source, TASK_ID,
            request=request,
        )

        self.assertEqual(normalized.requested_base, DEFAULT_BASE_REVSET)
        self.assertEqual(plan.selected_base, "refs/heads/main")
        self.assertEqual(
            normalized.resolved_base_commit_id,
            subprocess.run(
                ["git", "-C", str(self.source), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
        )

    def test_hardening_candidate_preserves_existing_jj_revset_resolution(self) -> None:
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        real = JjAdapter()
        adapter = mock.Mock(spec=JjAdapter)
        adapter.resolve_base.return_value = head
        adapter.materialization_plan.return_value = real.materialization_plan(
            self.source / ".git", head, exact_root=self.source,
        )
        revset = 'bookmarks(exact:"release") | tags(exact:"v1")'

        plan = preflight_plain_git_enablement(
            self.config, self.request(base=revset), jj=adapter,
            base_explicit=True, existing_jj=True,
        )

        self.assertEqual(plan.resolved_base_commit_id, head)
        adapter.resolve_base.assert_called_once_with(self.source, revset)
        adapter.resolve_git_commit.assert_not_called()

    def test_real_cli_missing_memory_refuses_before_intent_or_jj(self) -> None:
        shutil.rmtree(self.source / "Memory")
        stderr = io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "HEAD",
                "--task-id", TASK_ID, "--harness", "codex", "--goal", "Refuse",
                "--json",
            ], env={
                "HOME": str(self.home),
                "ASHA_CONFIG": str(self.root / "missing.json"),
                "ASHA_HOME": str(self.root / "asha"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            })
        self.assertEqual(status, 2)
        self.assertIn("unchanged", stderr.getvalue())
        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())


class ColocationIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)

    @staticmethod
    def _fact_positions(binding: dict) -> tuple[dict, ...]:
        return (
            binding["root_fact"],
            binding["git_binding"]["marker_fact"],
            binding["git_binding"]["target_fact"],
            binding["jj_fact"],
        )

    def _rewrite_stored_devices(self, store: ColocationIntentStore, old_dev: int) -> bytes:
        path = store.path(self.source)
        value = json.loads(path.read_bytes())
        for fact in self._fact_positions(value):
            fact["dev"] = old_dev
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        return raw

    def test_usable_jj_with_unverified_control_intent_is_never_adopted(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        adapter = mock.Mock()
        adapter.preflight.return_value = RepositoryFacts(self.source, self.source / ".git")

        with self.assertRaisesRegex(ValueError, "ambiguous Control colocation"):
            _ensure_colocated(
                adapter, RepositorySelection(self.source, plain_git=False), store,
            )

        adapter.preflight.assert_not_called()

    def test_verified_intent_allows_waiter_and_is_bound_to_root_inode(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        adapter = mock.Mock()
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )

        mutations = _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=True), store,
        )

        self.assertEqual(mutations, ())
        adapter.preflight.assert_called_once_with(self.source)

        replacement = self.root / "replacement"
        self.source.rename(replacement)
        shutil.copytree(replacement, self.source)
        with self.assertRaisesRegex(JjError, "bound to the current root fact"):
            store.read(self.source)

    def test_verified_root_hardening_typed_candidate_updates_only_root_fact_under_lock(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o775)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        before = store.path(self.source).read_bytes()
        before_value = json.loads(before)
        self.source.chmod(0o755)

        assessment = store.classify(self.source)

        self.assertEqual(assessment.kind, "verified_root_hardening_candidate")
        self.assertEqual(assessment.raw, before)
        self.assertEqual(assessment.digest, __import__("hashlib").sha256(before).hexdigest())
        store.reauthenticate_root_hardening(self.source, assessment)
        after_value = json.loads(store.path(self.source).read_bytes())
        self.assertEqual(after_value["state"], "verified")
        self.assertEqual(after_value["root_fact"]["mode"] & 0o777, 0o755)
        for field in ("contract", "root", "git_binding", "jj_fact", "state"):
            self.assertEqual(after_value[field], before_value[field])

    def test_verified_device_rebind_candidate_refreshes_every_device_only(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        current = json.loads(store.path(self.source).read_bytes())
        live_dev = current["root_fact"]["dev"]
        before = self._rewrite_stored_devices(store, live_dev + 1000)
        before_value = json.loads(before)

        assessment = store.classify(self.source)

        self.assertEqual(assessment.kind, "verified_device_rebind_candidate")
        self.assertEqual(assessment.device_remap, ((live_dev + 1000, live_dev),))
        self.assertEqual(assessment.raw, before)
        store.reauthenticate_device_rebind(self.source, assessment)
        after_value = json.loads(store.path(self.source).read_bytes())
        self.assertEqual(store.classify(self.source).kind, "verified")
        for before_fact, after_fact, current_fact in zip(
            self._fact_positions(before_value),
            self._fact_positions(after_value),
            self._fact_positions(current),
        ):
            self.assertEqual(after_fact["dev"], current_fact["dev"])
            self.assertEqual(
                {key: value for key, value in after_fact.items() if key != "dev"},
                {key: value for key, value in before_fact.items() if key != "dev"},
            )
        for field in ("contract", "root", "state"):
            self.assertEqual(after_value[field], before_value[field])
        self.assertEqual(
            {key: value for key, value in after_value["git_binding"].items()
             if key not in {"marker_fact", "target_fact"}},
            {key: value for key, value in before_value["git_binding"].items()
             if key not in {"marker_fact", "target_fact"}},
        )

    def test_device_rebind_mapping_is_coherent_injective_and_requires_a_change(self) -> None:
        def binding(devices: tuple[int, int, int, int]) -> dict:
            facts = [
                {"dev": device, "ino": index + 1, "mode": 0o40755, "uid": 1000}
                for index, device in enumerate(devices)
            ]
            return {
                "contract": "asha.control-colocation-intent.v1",
                "root": "/repo",
                "root_fact": facts[0],
                "git_binding": {
                    "kind": "gitdir", "marker_fact": facts[1],
                    "marker_digest": "a" * 64, "target": "/metadata",
                    "target_fact": facts[2],
                },
                "jj_fact": facts[3],
            }

        stored = binding((1, 1, 2, 1))
        coherent = binding((11, 11, 22, 11))
        split = binding((11, 12, 22, 11))
        collapse = binding((11, 11, 11, 11))

        self.assertEqual(
            ColocationIntentStore._coherent_device_remap(stored, coherent),
            ((1, 11), (2, 22)),
        )
        self.assertIsNone(ColocationIntentStore._coherent_device_remap(stored, split))
        self.assertIsNone(ColocationIntentStore._coherent_device_remap(stored, collapse))
        self.assertIsNone(ColocationIntentStore._coherent_device_remap(stored, stored))
        changes = (
            ("root path", lambda value: value.__setitem__("root", "/other")),
            ("marker kind", lambda value: value["git_binding"].__setitem__(
                "kind", "directory",
            )),
            ("marker digest", lambda value: value["git_binding"].__setitem__(
                "marker_digest", "b" * 64,
            )),
            ("marker target", lambda value: value["git_binding"].__setitem__(
                "target", "/other-metadata",
            )),
            ("inode", lambda value: value["root_fact"].__setitem__("ino", 99)),
            ("mode", lambda value: value["root_fact"].__setitem__("mode", 0o40750)),
            ("uid", lambda value: value["root_fact"].__setitem__("uid", 99)),
        )
        for label, mutate in changes:
            changed = json.loads(json.dumps(coherent))
            mutate(changed)
            with self.subTest(non_device_change=label):
                self.assertIsNone(
                    ColocationIntentStore._coherent_device_remap(stored, changed)
                )

    def test_device_rebind_refuses_intent_and_mixed_non_device_change(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        intent_path = store.path(self.source)
        intent_value = json.loads(intent_path.read_bytes())
        old_dev = intent_value["root_fact"]["dev"] + 1000
        for fact in (
            intent_value["root_fact"],
            intent_value["git_binding"]["marker_fact"],
            intent_value["git_binding"]["target_fact"],
        ):
            fact["dev"] = old_dev
        intent_raw = json.dumps(
            intent_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        intent_path.write_bytes(intent_raw)
        self.assertEqual(store.classify(self.source).kind, "mismatch")
        self.assertEqual(intent_path.read_bytes(), intent_raw)

        intent_path.unlink()
        self.source.chmod(0o775)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        verified = json.loads(store.path(self.source).read_bytes())
        live_dev = verified["root_fact"]["dev"]
        mixed_raw = self._rewrite_stored_devices(store, live_dev + 1000)
        self.source.chmod(0o755)
        self.assertEqual(store.classify(self.source).kind, "mismatch")
        self.assertEqual(store.path(self.source).read_bytes(), mixed_raw)

    def test_strict_intent_decoder_rejects_boolean_in_every_filesystem_fact(self) -> None:
        fact_paths = (
            ("root_fact",),
            ("git_binding", "marker_fact"),
            ("git_binding", "target_fact"),
            ("jj_fact",),
        )
        for fact_path in fact_paths:
            for field in ("dev", "ino", "mode", "uid"):
                with self.subTest(fact_path=fact_path, field=field):
                    source = self.root / ("bool-" + "-".join(fact_path) + "-" + field)
                    source.mkdir()
                    subprocess.run(["git", "init", "-q", str(source)], check=True)
                    store = ColocationIntentStore(self.config)
                    store.begin(source)
                    (source / ".jj").mkdir()
                    store.mark_verified(source)
                    path = store.path(source)
                    value = json.loads(path.read_bytes())
                    fact = value
                    for component in fact_path:
                        fact = fact[component]
                    fact[field] = True
                    raw = json.dumps(
                        value, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8") + b"\n"
                    path.write_bytes(raw)
                    os.chmod(path, 0o600)

                    with self.assertRaisesRegex(JjError, "invalid"):
                        store.classify(source)

                    self.assertEqual(path.read_bytes(), raw)

    def test_boolean_device_record_never_classifies_or_reaches_repair(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        path = store.path(self.source)
        value = json.loads(path.read_bytes())
        for fact in self._fact_positions(value):
            fact["dev"] = True
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        adapter = mock.Mock()

        with mock.patch.object(
            store, "reauthenticate_device_rebind",
            side_effect=AssertionError("malformed record must never repair"),
        ) as repair, self.assertRaisesRegex(ValueError, "stale or binding-mismatched"):
            _ensure_colocated(
                adapter, RepositorySelection(self.source, plain_git=False), store,
            )

        repair.assert_not_called()
        self.assertEqual(path.read_bytes(), raw)

    def test_ensure_colocated_reauthenticates_device_rebind_through_stable_chain(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        current = json.loads(store.path(self.source).read_bytes())
        self._rewrite_stored_devices(store, current["root_fact"]["dev"] + 1000)
        state = object()
        adapter = mock.Mock()
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )
        adapter.pin_operation.side_effect = ["a" * 128, "a" * 128]
        adapter._git_semantic_state.side_effect = [state, state]
        adapter.working_copy_parent.return_value = "b" * 40
        adapter.git_head_exact.return_value = "b" * 40

        mutations = _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=False), store,
        )

        self.assertEqual(mutations, ())
        self.assertEqual(store.classify(self.source).kind, "verified")
        self.assertEqual(
            adapter._git_semantic_state.call_args_list,
            [mock.call(self.source, include_jj_refs=True)] * 2,
        )
        self.assertEqual(adapter.pin_operation.call_count, 2)
        adapter.init_colocated.assert_not_called()
        adapter.import_git.assert_not_called()

    def test_device_rebind_rechecks_exact_current_binding_before_replace(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        current = json.loads(store.path(self.source).read_bytes())
        before = self._rewrite_stored_devices(
            store, current["root_fact"]["dev"] + 1000,
        )
        assessment = store.classify(self.source)
        changed_binding = json.loads(json.dumps(assessment.current_binding))
        changed_binding["root_fact"]["dev"] += 1
        raced = SimpleNamespace(
            kind=assessment.kind, current_binding=changed_binding,
        )

        with mock.patch.object(
            store, "_assessment", side_effect=[assessment, raced],
        ), self.assertRaisesRegex(JjError, "facts changed.*preserved"):
            store.reauthenticate_device_rebind(self.source, assessment)

        self.assertEqual(store.path(self.source).read_bytes(), before)
        self.assertEqual(list(store.directory.glob(".*.tmp.*")), [])

    def test_device_rebind_refuses_oversized_rewrite_without_replacement(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        current = json.loads(store.path(self.source).read_bytes())
        before = self._rewrite_stored_devices(
            store, current["root_fact"]["dev"] + 1000,
        )
        assessment = store.classify(self.source)

        with mock.patch("lib.control.jj.MAX_COLOCATION_INTENT_BYTES", len(before) - 1), \
                self.assertRaisesRegex(JjError, "bounded size"):
            store.reauthenticate_device_rebind(self.source, assessment)

        self.assertEqual(store.path(self.source).read_bytes(), before)

    def test_verified_root_hardening_classifier_refuses_loosening_mixed_and_intent(self) -> None:
        cases = ((0o755, 0o775), (0o777, 0o754))
        for old_mode, new_mode in cases:
            with self.subTest(old=oct(old_mode), new=oct(new_mode)):
                source = self.root / f"source-{old_mode:o}-{new_mode:o}"
                source.mkdir(mode=old_mode)
                subprocess.run(["git", "init", "-q", str(source)], check=True)
                source.chmod(old_mode)
                store = ColocationIntentStore(self.config)
                store.begin(source)
                (source / ".jj").mkdir()
                store.mark_verified(source)
                source.chmod(new_mode)
                self.assertEqual(store.classify(source).kind, "mismatch")

        intent_source = self.root / "intent-source"
        intent_source.mkdir(mode=0o775)
        subprocess.run(["git", "init", "-q", str(intent_source)], check=True)
        intent_source.chmod(0o775)
        store = ColocationIntentStore(self.config)
        store.begin(intent_source)
        intent_source.chmod(0o755)
        self.assertEqual(store.classify(intent_source).kind, "mismatch")

    def test_root_hardening_exact_compare_preserves_preexisting_competing_bytes(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o775)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        self.source.chmod(0o755)
        assessment = store.classify(self.source)
        competing = assessment.raw.replace(b'"state":"verified"', b'"state": "verified"')
        store.path(self.source).write_bytes(competing)
        os.chmod(store.path(self.source), 0o600)

        with self.assertRaisesRegex(JjError, "changed.*preserved|cooperative"):
            store.reauthenticate_root_hardening(self.source, assessment)

        self.assertEqual(store.path(self.source).read_bytes(), competing)

    def test_two_control_intent_writers_serialize_on_the_shared_source_lock(self) -> None:
        first = ColocationIntentStore(self.config)
        second = ColocationIntentStore(self.config)
        entered = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []

        def compete() -> None:
            try:
                with second.mutation_lock(self.source):
                    finished.set()
            except BaseException as exc:
                errors.append(exc)

        with first.mutation_lock(self.source):
            thread = threading.Thread(target=compete)
            thread.start()
            entered.set()
            time.sleep(0.1)
            self.assertFalse(finished.is_set())
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])

    def test_ensure_colocated_reauthenticates_only_after_stable_all_ref_and_operation_pass(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o775)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        self.source.chmod(0o755)
        state = object()
        adapter = mock.Mock()
        adapter.preflight.return_value = RepositoryFacts(
            self.source, self.source / ".git",
        )
        adapter.pin_operation.side_effect = ["a" * 128, "a" * 128]
        adapter._git_semantic_state.side_effect = [state, state]
        adapter.working_copy_parent.return_value = "b" * 40
        adapter.git_head_exact.return_value = "b" * 40

        mutations = _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=True), store,
        )

        self.assertEqual(mutations, ())
        self.assertEqual(store.classify(self.source).kind, "verified")
        self.assertEqual(
            adapter._git_semantic_state.call_args_list,
            [mock.call(self.source, include_jj_refs=True)] * 2,
        )
        self.assertEqual(adapter.pin_operation.call_count, 2)
        adapter.git_head_exact.assert_called_once_with(self.source)

    def test_reauthentication_refuses_semantic_or_operation_drift_without_rewrite(self) -> None:
        for drift in ("semantic", "operation"):
            with self.subTest(drift=drift):
                source = self.root / drift
                source.mkdir(mode=0o775)
                subprocess.run(["git", "init", "-q", str(source)], check=True)
                source.chmod(0o775)
                store = ColocationIntentStore(self.config)
                store.begin(source)
                (source / ".jj").mkdir()
                store.mark_verified(source)
                before = store.path(source).read_bytes()
                source.chmod(0o755)
                adapter = mock.Mock()
                adapter.preflight.return_value = RepositoryFacts(source, source / ".git")
                adapter.working_copy_parent.return_value = "b" * 40
                adapter.git_head_exact.return_value = "b" * 40
                adapter._git_semantic_state.side_effect = (
                    [object(), object()] if drift == "semantic" else ["same", "same"]
                )
                adapter.pin_operation.side_effect = (
                    ["a" * 128, "a" * 128]
                    if drift == "semantic" else ["a" * 128, "c" * 128]
                )
                with self.assertRaisesRegex(ValueError, "changed.*reauthentication"):
                    _ensure_colocated(
                        adapter, RepositorySelection(source, plain_git=True), store,
                    )
                self.assertEqual(store.path(source).read_bytes(), before)

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_real_hardening_chain_preserves_dirty_staged_untracked_and_all_refs(self) -> None:
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / "mixed.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "tracked.txt", "mixed.txt"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        self.source.chmod(0o775)
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        adapter = JjAdapter()
        adapter.init_colocated(self.source)
        store.mark_verified(self.source)
        (self.source / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
        (self.source / "mixed.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "mixed.txt"],
            check=True, env=git_env,
        )
        (self.source / "mixed.txt").write_text("working\n", encoding="utf-8")
        (self.source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.source), "update-ref", "refs/jj/test-auth", head],
            check=True,
        )
        self.source.chmod(0o755)
        before = adapter._git_semantic_state(self.source, include_jj_refs=True)
        self.assertTrue(any(line.startswith(b"refs/jj/test-auth\x00") for line in before.refs))

        _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=False), store,
        )

        self.assertEqual(
            adapter._git_semantic_state(self.source, include_jj_refs=True), before,
        )
        self.assertEqual(store.classify(self.source).kind, "verified")
        self.assertEqual((self.source / "tracked.txt").read_text(), "unstaged\n")
        self.assertEqual((self.source / "mixed.txt").read_text(), "working\n")
        self.assertEqual((self.source / "untracked.txt").read_text(), "untracked\n")

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_real_device_rebind_preserves_semantics_operation_and_workspaces(self) -> None:
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "tracked.txt"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        adapter = JjAdapter()
        adapter.init_colocated(self.source)
        store.mark_verified(self.source)
        current = json.loads(store.path(self.source).read_bytes())
        old_record = self._rewrite_stored_devices(
            store, current["root_fact"]["dev"] + 1000,
        )
        (self.source / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        (self.source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(self.source), "update-ref", "refs/jj/test-device", head],
            check=True,
        )
        semantic_before = adapter._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        operation_before = adapter.pin_operation(self.source)
        workspaces_before = subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy",
             "workspace", "list"], check=True,
            capture_output=True, text=True,
        ).stdout

        _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=False), store,
        )

        self.assertNotEqual(store.path(self.source).read_bytes(), old_record)
        self.assertEqual(store.classify(self.source).kind, "verified")
        self.assertEqual(
            adapter._git_semantic_state(self.source, include_jj_refs=True),
            semantic_before,
        )
        self.assertEqual(adapter.pin_operation(self.source), operation_before)
        self.assertEqual(
            subprocess.run(
                ["jj", "-R", str(self.source), "--ignore-working-copy",
                 "workspace", "list"], check=True,
                capture_output=True, text=True,
            ).stdout,
            workspaces_before,
        )
        self.assertEqual((self.source / "tracked.txt").read_text(), "dirty\n")
        self.assertEqual((self.source / "untracked.txt").read_text(), "untracked\n")

    def test_manual_preexisting_valid_jj_without_control_intent_is_accepted(self) -> None:
        (self.source / ".jj").mkdir()
        adapter = mock.Mock()
        adapter.preflight.return_value = RepositoryFacts(self.source, self.source)

        mutations = _ensure_colocated(
            adapter, RepositorySelection(self.source, plain_git=False),
            ColocationIntentStore(self.config),
        )

        self.assertEqual(mutations, ())
        adapter.preflight.assert_called_once_with(self.source)

    def test_gitdir_marker_retarget_invalidates_verified_intent(self) -> None:
        work_a = self.root / "work-a"
        work_b = self.root / "work-b"
        metadata_a = self.root / "metadata-a"
        metadata_b = self.root / "metadata-b"
        for work, metadata in ((work_a, metadata_a), (work_b, metadata_b)):
            subprocess.run(
                ["git", "init", "-q", f"--separate-git-dir={metadata}", str(work)],
                check=True,
            )
        marker = work_a / ".git"
        marker_inode = marker.stat().st_ino
        original_marker = marker.read_bytes()
        store = ColocationIntentStore(self.config)
        store.begin(work_a.resolve())

        marker.write_text(f"gitdir: {metadata_b}\n", encoding="utf-8")
        with self.assertRaisesRegex(JjError, "Git marker.*binding|git.*bound"):
            store.read(work_a.resolve())

        marker.write_bytes(original_marker)
        self.assertEqual(store.read(work_a.resolve())["state"], "intent")
        (work_a / ".jj").mkdir()
        store.mark_verified(work_a.resolve())

        marker.write_text(f"gitdir: {metadata_b}\n", encoding="utf-8")

        self.assertEqual(marker.stat().st_ino, marker_inode)
        with self.assertRaisesRegex(JjError, "Git marker.*binding|git.*bound"):
            store.read(work_a.resolve())

    def test_verified_intent_rejects_replaced_or_linked_jj_marker(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        marker = self.source / ".jj"
        marker.mkdir()
        store.mark_verified(self.source)

        marker.rename(self.source / ".jj-old")
        marker.mkdir()
        with self.assertRaisesRegex(JjError, "jj fact"):
            store.read(self.source)

        shutil.rmtree(marker)
        marker.symlink_to(self.source / ".jj-old", target_is_directory=True)
        with self.assertRaisesRegex(JjError, "owned directory"):
            store.read(self.source)


class CliColocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        self.source.chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.workspace = self.config.workspace_root / "repo-key" / "auto-init"
        self.workspace.mkdir(parents=True)
        current = self.workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        self.task = task_record(
            task_id=TASK_ID,
            slug="auto-init",
            repository_root=str(self.source),
            workspace_path=str(self.workspace),
        )
        self.task["label"] = "Auto init"
        self.result = {
            "task": self.task,
            "run": self.task["runs"][0],
            "session": self.task["tmux"]["session"],
            "pane": self.task["runs"][0]["pane_id"],
            "workspace": {
                "path": self.task["jj"]["workspace_path"],
                "name": self.task["jj"]["workspace_name"],
                "change_id": self.task["jj"]["change_id"],
            },
        }

    def verified_device_candidate(self):
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / ".gitignore").write_text(
            "/.asha/\n/Memory/\n/Work/\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.source), "add", "tracked.txt", ".gitignore"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "branch", "-M", "main"],
            check=True, env=git_env,
        )
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "device-rebind-outer",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n"
            "# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        adapter = JjAdapter()
        adapter.init_colocated(self.source)
        store.mark_verified(self.source)
        path = store.path(self.source)
        value = json.loads(path.read_bytes())
        old_dev = value["root_fact"]["dev"] + 1000
        for fact in (
            value["root_fact"],
            value["git_binding"]["marker_fact"],
            value["git_binding"]["target_fact"],
            value["jj_fact"],
        ):
            fact["dev"] = old_dev
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.write_bytes(raw)
        os.chmod(path, 0o600)
        self.assertEqual(store.classify(self.source).kind,
                         "verified_device_rebind_candidate")
        return adapter, store, raw

    def workspace_inventory(self) -> list[str]:
        if not self.config.workspace_root.exists():
            return []
        return sorted(
            str(path.relative_to(self.config.workspace_root))
            for path in self.config.workspace_root.rglob("*")
        )

    def adapter(self):
        adapter = mock.Mock()
        adapter.discover_root.side_effect = JjError("not jj")

        def initialize(source, **_kwargs):
            (source / ".jj").mkdir(exist_ok=True)
            return {
                "kind": "jj-operation",
                "operation": "git init --colocate",
                "detail": "enabled and retained jj colocation",
            }

        adapter.init_colocated.side_effect = initialize
        adapter.preflight.return_value = RepositoryFacts(
            root=self.source, git_root=self.source,
        )
        adapter.working_copy_parent.return_value = "a" * 40
        adapter.git_head.return_value = "a" * 40
        adapter.git_head_exact.return_value = "a" * 40
        adapter.import_git.return_value = ({
            "kind": "jj-operation", "operation": "git import",
            "detail": "recorded a jj operation-log entry for git import",
        },)
        adapter.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/main",), "b" * 40, "attached-local",
        )
        return adapter

    def invoke(self, adapter, *, prepare=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        prepare_call = prepare or (lambda config, request, jj=None: self.task)
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.new_uuid", return_value=TASK_ID), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch(
                    "lib.control.cli.preflight_plain_git_enablement",
                    return_value=PlainGitPreEnablePlan(
                        "repo:test", "repo-key", self.workspace,
                        inspect_pre_enable_binding(self.source),
                        "refs/heads/main", "b" * 40, False, None,
                    ),
                ), \
                mock.patch(
                    "lib.control.cli.prepare_task_workspace", side_effect=prepare_call,
                ), mock.patch(
                    "lib.control.cli.launch_task", return_value=self.result,
                ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source),
                "--harness", "codex", "--goal", "Auto init", "--json",
            ], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_new_plain_git_task_initializes_once_and_reports_mutation_first(self) -> None:
        adapter = self.adapter()
        captured: dict[str, PrepareRequest] = {}

        def prepare(_config, request, jj=None):
            captured["request"] = request
            return self.task

        status, stdout, stderr = self.invoke(adapter, prepare=prepare)

        self.assertEqual(status, 0, stderr)
        adapter.init_colocated.assert_called_once_with(
            self.source, expected_binding=inspect_pre_enable_binding(self.source),
        )
        payload = json.loads(stdout)
        self.assertEqual(
            [item["operation"] for item in payload["source_mutations"]],
            ["git init --colocate", "git import"],
        )
        self.assertEqual(captured["request"].requested_base, DEFAULT_BASE_REVSET)
        self.assertEqual(captured["request"].resolved_base_commit_id, "b" * 40)
        self.assertIn("retained jj colocation", stderr)

    def test_plain_git_pre_enable_refusal_precedes_intent_init_and_task_state(self) -> None:
        adapter = self.adapter()
        adapter.init_colocated.side_effect = AssertionError("init must remain unreachable")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.new_uuid", return_value=TASK_ID), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch(
                    "lib.control.cli.preflight_plain_git_enablement",
                    side_effect=PreparationError("unsafe source; unchanged"),
                ) as preflight, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "HEAD",
                "--harness", "codex", "--goal", "Auto init", "--json",
            ], env=self.env)

        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("unsafe source; unchanged", stderr.getvalue())
        preflight.assert_called_once()
        adapter.init_colocated.assert_not_called()
        self.assertFalse((self.source / ".jj").exists())
        self.assertFalse(ColocationIntentStore(self.config).path(self.source).exists())
        with self.assertRaisesRegex(StoreError, "task not found"):
            TaskStore(self.config).read(TASK_ID)

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_device_rebind_omitted_base_resolves_before_complete_gate_and_rewrite(self) -> None:
        adapter, store, before_record = self.verified_device_candidate()
        expected_default = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "refs/heads/main^{commit}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        before_semantic = adapter._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        before_operation = adapter.pin_operation(self.source)
        before_workspaces = subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy",
             "workspace", "list"], check=True, capture_output=True, text=True,
        ).stdout
        before_inventory = self.workspace_inventory()
        order: list[str] = []
        captured_plan: list[PlainGitPreEnablePlan] = []
        real_task_read = TaskStore.read
        real_journal_read = CreationJournalStore.read
        real_preflight = preflight_plain_git_enablement
        real_revalidate = revalidate_plain_git_pre_enable_plan
        real_rebind = ColocationIntentStore.reauthenticate_device_rebind

        def task_read(instance, task_id):
            order.append("caller-replay")
            return real_task_read(instance, task_id)

        def journal_read(instance, task_id):
            order.append("journal-replay")
            return real_journal_read(instance, task_id)

        def gate(*args, **kwargs):
            order.append("pre-enable-enter")
            self.assertTrue(kwargs["existing_jj"])
            plan = real_preflight(*args, **kwargs)
            self.assertIsNotNone(plan.materialization_plan)
            self.assertIsNotNone(plan.context_compatibility)
            self.assertFalse(plan.destination.exists())
            captured_plan.append(plan)
            order.append("pre-enable-complete")
            return plan

        def revalidate(plan, *, jj=None):
            order.append("plan-revalidate")
            return real_revalidate(plan, jj=jj)

        def rebind(instance, root, assessment):
            order.append("record-rewrite")
            self.assertEqual(order[:4], [
                "caller-replay", "journal-replay", "pre-enable-enter",
                "pre-enable-complete",
            ])
            self.assertGreaterEqual(order.count("plan-revalidate"), 1)
            return real_rebind(instance, root, assessment)

        def stop_after_rebind(*_args, **kwargs):
            order.append("later-start-boundary")
            self.assertEqual(store.classify(self.source).kind, "verified")
            self.assertEqual(kwargs["initial_source_mutations"], ())
            request = kwargs["preflight_request"]
            self.assertIsInstance(request, PrepareRequest)
            self.assertEqual(request.repository, self.source)
            self.assertEqual(request.task_id, TASK_ID)
            self.assertEqual(request.requested_base, DEFAULT_BASE_REVSET)
            self.assertEqual(
                captured_plan[0].default_base_resolution,
                DefaultBaseResolution(
                    ("refs/heads/main",), expected_default, "attached-local",
                ),
            )
            self.assertEqual(
                request.resolved_base_commit_id,
                expected_default,
            )
            self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())
            self.assertFalse(
                CreationJournalStore(self.config).path(TASK_ID).exists()
            )
            self.assertFalse(captured_plan[0].destination.exists())
            raise PreparationError("forced refusal after authenticated rebind")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch.object(TaskStore, "read", autospec=True,
                                  side_effect=task_read), \
                mock.patch.object(CreationJournalStore, "read", autospec=True,
                                  side_effect=journal_read), \
                mock.patch("lib.control.cli.preflight_plain_git_enablement",
                           side_effect=gate), \
                mock.patch("lib.control.cli.revalidate_plain_git_pre_enable_plan",
                           side_effect=revalidate), \
                mock.patch.object(
                    ColocationIntentStore, "reauthenticate_device_rebind",
                    autospec=True, side_effect=rebind,
                ), mock.patch("lib.control.cli._start_new_task",
                              side_effect=stop_after_rebind) as start, \
                mock.patch("lib.control.cli.TmuxAdapter",
                           side_effect=AssertionError(
                               "tmux must remain unreachable",
                           )) as tmux, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source),
                "--task-id", TASK_ID, "--slug", "device-rebind-outer",
                "--harness", "codex", "--goal", "Outer rebind", "--json",
            ], env=self.env)

        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("forced refusal after authenticated rebind", stderr.getvalue())
        self.assertIn("record-rewrite", order)
        self.assertLess(order.index("record-rewrite"), order.index("later-start-boundary"))
        start.assert_called_once()
        tmux.assert_not_called()
        self.assertNotEqual(store.path(self.source).read_bytes(), before_record)
        self.assertEqual(store.classify(self.source).kind, "verified")
        self.assertEqual(self.workspace_inventory(), before_inventory)
        self.assertEqual(
            adapter._git_semantic_state(self.source, include_jj_refs=True),
            before_semantic,
        )
        self.assertEqual(adapter.pin_operation(self.source), before_operation)
        self.assertEqual(subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy",
             "workspace", "list"], check=True, capture_output=True, text=True,
        ).stdout, before_workspaces)

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_device_rebind_outer_pre_enable_refusal_preserves_record_and_state(self) -> None:
        adapter, store, before_record = self.verified_device_candidate()
        before_inventory = self.workspace_inventory()
        before_operation = adapter.pin_operation(self.source)
        (self.source / "Memory" / "decisions.md").unlink()
        before_semantic = adapter._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        stdout, stderr = io.StringIO(), io.StringIO()

        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli.preflight_plain_git_enablement",
                           wraps=preflight_plain_git_enablement) as preflight, \
                mock.patch.object(
                    ColocationIntentStore, "reauthenticate_device_rebind",
                    autospec=True,
                    side_effect=AssertionError("refused gate must not rewrite"),
                ) as rebind, \
                mock.patch("lib.control.cli._start_new_task",
                           side_effect=AssertionError("refused gate must not start")) as start, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "main",
                "--task-id", TASK_ID, "--slug", "device-rebind-refusal",
                "--harness", "codex", "--goal", "Outer refusal", "--json",
            ], env=self.env)

        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("Memory", stderr.getvalue())
        self.assertTrue(preflight.called)
        self.assertTrue(preflight.call_args.kwargs["existing_jj"])
        rebind.assert_not_called()
        start.assert_not_called()
        self.assertEqual(store.path(self.source).read_bytes(), before_record)
        self.assertEqual(self.workspace_inventory(), before_inventory)
        self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(TASK_ID).exists())
        self.assertEqual(
            adapter._git_semantic_state(self.source, include_jj_refs=True),
            before_semantic,
        )
        self.assertEqual(adapter.pin_operation(self.source), before_operation)

    @unittest.skipUnless(shutil.which("jj"), "jj is required")
    def test_device_rebind_outer_start_revalidates_forwarded_plan_against_late_git_replacement(
        self,
    ) -> None:
        adapter, store, before_record = self.verified_device_candidate()
        before_inventory = self.workspace_inventory()
        before_operation = adapter.pin_operation(self.source)
        before_semantic = adapter._git_semantic_state(
            self.source, include_jj_refs=True,
        )
        before_workspaces = subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy",
             "workspace", "list"], check=True, capture_output=True, text=True,
        ).stdout
        real_require = require_pre_enable_binding
        outer_binding_validated = False
        replacement_observed = False
        git_marker = self.source / ".git"
        held_git_marker = self.root / "authenticated-git-marker"

        def replace_git_marker_at_inner_boundary(root, expected):
            nonlocal outer_binding_validated, replacement_observed
            if not outer_binding_validated:
                result = real_require(root, expected)
                outer_binding_validated = True
                return result

            git_marker.rename(held_git_marker)
            shutil.copytree(held_git_marker, git_marker, symlinks=True)
            replacement_observed = True
            try:
                return real_require(root, expected)
            finally:
                shutil.rmtree(git_marker)
                held_git_marker.rename(git_marker)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch(
                    "lib.control.prepare.require_pre_enable_binding",
                    side_effect=replace_git_marker_at_inner_boundary,
                ), mock.patch.object(
                    ColocationIntentStore, "reauthenticate_device_rebind",
                    autospec=True,
                    side_effect=AssertionError(
                        "late Git replacement must refuse before record rewrite",
                    ),
                ) as rebind, mock.patch(
                    "lib.control.cli._start_new_task",
                    side_effect=AssertionError(
                        "late Git replacement must refuse before task start",
                    ),
                ) as start, mock.patch(
                    "lib.control.cli.TmuxAdapter",
                    side_effect=AssertionError("tmux must remain unreachable"),
                ) as tmux, contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "main",
                "--task-id", TASK_ID, "--slug", "device-rebind-late-binding",
                "--harness", "codex", "--goal", "Late binding refusal", "--json",
            ], env=self.env)

        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertTrue(outer_binding_validated)
        self.assertTrue(replacement_observed)
        self.assertIn("pre-enable repository binding changed", stderr.getvalue())
        self.assertIn("pre-enable plan invalidated", stderr.getvalue())
        rebind.assert_not_called()
        start.assert_not_called()
        tmux.assert_not_called()
        self.assertEqual(store.path(self.source).read_bytes(), before_record)
        self.assertEqual(
            store.classify(self.source).kind,
            "verified_device_rebind_candidate",
        )
        self.assertEqual(self.workspace_inventory(), before_inventory)
        self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(TASK_ID).exists())
        self.assertEqual(
            adapter._git_semantic_state(self.source, include_jj_refs=True),
            before_semantic,
        )
        self.assertEqual(adapter.pin_operation(self.source), before_operation)
        self.assertEqual(subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy",
             "workspace", "list"], check=True, capture_output=True, text=True,
        ).stdout, before_workspaces)

    def test_plain_git_explicit_base_oid_is_carried_without_jj_reinterpretation(self) -> None:
        adapter = self.adapter()
        resolved = "c" * 40
        captured: list[PrepareRequest] = []

        def prepare(_config, request, jj=None):
            captured.append(request)
            return self.task

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.new_uuid", return_value=TASK_ID), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch(
                    "lib.control.cli.preflight_plain_git_enablement",
                    return_value=PlainGitPreEnablePlan(
                        "repo:test", "repo-key", self.workspace,
                        inspect_pre_enable_binding(self.source),
                        "release", resolved, False, None,
                    ),
                ), \
                mock.patch("lib.control.cli.prepare_task_workspace", side_effect=prepare), \
                mock.patch("lib.control.cli.launch_task", return_value=self.result), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "release",
                "--harness", "codex", "--goal", "Auto init", "--json",
            ], env=self.env)

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].requested_base, "release")
        self.assertEqual(captured[0].resolved_base_commit_id, resolved)
        adapter.resolve_base.assert_not_called()

    def test_later_failure_reports_retained_source_enablement(self) -> None:
        adapter = self.adapter()

        status, stdout, stderr = self.invoke(
            adapter,
            prepare=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                PreparationError("synthetic preparation failure")
            ),
        )

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("synthetic preparation failure", stderr)
        self.assertIn("retained", stderr)
        adapter.init_colocated.assert_called_once_with(
            self.source, expected_binding=inspect_pre_enable_binding(self.source),
        )

    def test_direct_cli_interrupt_returns_130_and_retains_ambiguous_init(self) -> None:
        adapter = self.adapter()

        def interrupt(source, **_kwargs):
            (source / ".jj").mkdir(exist_ok=True)
            raise KeyboardInterrupt

        adapter.init_colocated.side_effect = interrupt

        status, stdout, stderr = self.invoke(adapter)

        self.assertEqual((status, stdout), (130, ""))
        self.assertIn("interrupted", stderr)
        self.assertIn("retained", stderr)
        intent = ColocationIntentStore(self.config).read(self.source)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["state"], "intent")

    def test_direct_cli_post_init_interrupt_returns_130_with_retained_intent(self) -> None:
        adapter = self.adapter()
        adapter.preflight.side_effect = KeyboardInterrupt

        status, stdout, stderr = self.invoke(adapter)

        self.assertEqual((status, stdout), (130, ""))
        self.assertIn("verification", stderr)
        self.assertIn("retained", stderr)
        self.assertEqual(
            ColocationIntentStore(self.config).read(self.source)["state"],
            "intent",
        )

    def test_ordinary_init_failure_remains_status_two(self) -> None:
        adapter = self.adapter()
        adapter.init_colocated.side_effect = JjError("ordinary init failure")

        status, stdout, stderr = self.invoke(adapter)

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("ordinary init failure", stderr)
        self.assertIn("retained", stderr)

    def test_partial_jj_is_refused_without_an_init_attempt(self) -> None:
        (self.source / ".jj").mkdir()
        adapter = self.adapter()
        adapter.preflight.side_effect = JjError("malformed jj metadata")
        adapter.init_colocated.side_effect = AssertionError("must not overwrite partial .jj")

        status, stdout, stderr = self.invoke(adapter)

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("existing .jj", stderr)
        self.assertIn("malformed jj metadata", stderr)
        adapter.init_colocated.assert_not_called()

    def test_linked_worktree_start_refuses_before_intent_or_init(self) -> None:
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "base.txt"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        linked = self.root / "linked-start"
        subprocess.run(
            ["git", "-C", str(self.source), "worktree", "add", "-q", "-b",
             "linked-start", str(linked)], check=True,
        )
        adapter = self.adapter()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(linked), "--harness", "codex",
                "--goal", "Refuse linked", "--json",
            ], env=self.env)

        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("linked Git worktree", stderr.getvalue())
        self.assertIn("primary worktree", stderr.getvalue())
        adapter.init_colocated.assert_not_called()
        self.assertIsNone(ColocationIntentStore(self.config).read(linked.resolve()))

    def test_plain_git_existing_task_id_replay_does_not_initialize(self) -> None:
        self.task["runs"][0]["harness"] = "codex"
        TaskStore(self.config).save(self.task)
        adapter = self.adapter()
        adapter.init_colocated.side_effect = AssertionError(
            "caller-ID replay must remain mutation-free"
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source),
                "--task-id", TASK_ID, "--base", "trunk()", "--harness", "codex",
                "--goal", "Auto init", "--json",
            ], env=self.env)

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["existing"])
        adapter.init_colocated.assert_not_called()
        self.assertFalse((self.source / ".jj").exists())

    def test_plain_git_omitted_base_replay_preserves_caller_identity(self) -> None:
        self.task["runs"][0]["harness"] = "codex"
        self.task["jj"]["requested_base"] = DEFAULT_BASE_REVSET
        TaskStore(self.config).save(self.task)
        adapter = self.adapter()
        adapter.preflight.side_effect = AssertionError(
            "identical caller-ID replay must precede all repository preflight"
        )
        adapter.init_colocated.side_effect = AssertionError(
            "identical caller-ID replay must remain mutation-free"
        )

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source),
                "--task-id", TASK_ID, "--harness", "codex",
                "--goal", "Auto init", "--json",
            ], env=self.env)

        self.assertEqual(status, 0, stderr.getvalue())
        self.assertTrue(json.loads(stdout.getvalue())["existing"])
        adapter.preflight.assert_not_called()
        adapter.init_colocated.assert_not_called()

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=self.adapter()), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source),
                "--task-id", TASK_ID, "--base", "refs/heads/main",
                "--harness", "codex", "--goal", "Auto init", "--json",
            ], env=self.env)
        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("requested base", stderr.getvalue())

    def test_concurrent_plain_git_starts_recheck_under_one_source_lock(self) -> None:
        adapter = self.adapter()
        init_calls = 0
        init_guard = threading.Lock()

        def initialize(source):
            nonlocal init_calls
            with init_guard:
                init_calls += 1
            time.sleep(0.05)
            (source / ".jj").mkdir()
            return {
                "kind": "jj-operation", "operation": "git init --colocate",
                "detail": "enabled and retained jj colocation",
            }

        adapter.init_colocated.side_effect = initialize
        selection = RepositorySelection(self.source, plain_git=True)
        task_ids = (
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        )
        barrier = threading.Barrier(2)
        results: list[tuple[dict[str, str], ...]] = []
        errors: list[BaseException] = []

        def start(task_id: str) -> None:
            try:
                store = TaskStore(self.config)
                intents = ColocationIntentStore(self.config)
                barrier.wait()
                with store.transaction_lock(task_id), intents.mutation_lock(self.source):
                    results.append(_ensure_colocated(
                        adapter, selection, intents,
                    ))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=start, args=(task_id,)) for task_id in task_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(init_calls, 1)
        self.assertEqual(sorted(len(item) for item in results), [0, 1])


class RecoveryAdoptionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o755)
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        task = task_record(
            task_id=TASK_ID, slug="retained-adopt", repository_root=str(self.source),
            workspace_path=str(
                self.config.workspace_root / "repo-key" / "retained-adopt"
            ),
        )
        task["label"] = "Resume exact retained task"
        task["lifecycle"] = "failed"
        task["runs"] = []
        task["jj"]["change_id"] = None
        task["jj"]["working_commit_id"] = None
        TaskStore(self.config).save(task)

    def invoke(self, extra: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout), mock.patch(
            "lib.control.cli.shutil.which", return_value="/usr/bin/codex",
        ):
            status = control_main(["task", "recover", TASK_ID, *extra], env=self.env)
        return status, stderr.getvalue()

    def test_adoption_requires_complete_explicit_authorization_before_controller_call(self) -> None:
        with mock.patch(
            "lib.control.cli.recover_task",
            side_effect=AssertionError("incomplete adoption must not run"),
        ):
            status, diagnostic = self.invoke([
                "--adopt", "--harness", "codex", "--role", "implementer",
                "--goal", "Resume exact retained task",
            ])
        self.assertEqual(status, 2)
        self.assertIn("--yes", diagnostic)

    def test_adoption_passes_exact_validated_launch_authorization(self) -> None:
        result = {
            "task": {"lifecycle": "running"},
            "journal": {"phase": "run-recorded"},
            "message": "adopted and launched",
            "recovery_commands": None,
        }
        with mock.patch("lib.control.cli.recover_task", return_value=result) as recover:
            status, diagnostic = self.invoke([
                "--adopt", "--yes", "--harness", "codex",
                "--role", "implementer", "--goal", "Resume exact retained task",
            ])
        self.assertEqual((status, diagnostic), (0, ""))
        self.assertEqual(recover.call_args.kwargs["adopt"], True)
        self.assertEqual(recover.call_args.kwargs["harness"], "codex")
        self.assertEqual(recover.call_args.kwargs["role"], "implementer")
        self.assertEqual(recover.call_args.kwargs["goal"], "Resume exact retained task")

    def test_adoption_goal_mismatch_refuses_before_recovery_controller(self) -> None:
        with mock.patch(
            "lib.control.cli.recover_task",
            side_effect=AssertionError("mismatched authorization must not run"),
        ):
            status, diagnostic = self.invoke([
                "--adopt", "--yes", "--harness", "codex",
                "--role", "implementer", "--goal", "different",
            ])
        self.assertEqual(status, 2)
        self.assertIn("exactly match", diagnostic)


class DoctorColocationIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        (self.source / ".asha").mkdir()
        (self.source / ".asha" / "config.json").write_text("{}\n", encoding="utf-8")
        (self.source / "Memory").mkdir()
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Active\n", encoding="utf-8",
        )
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)

    def doctor(self, root: Path, *, usable_jj: bool) -> dict[str, str]:
        adapter = mock.Mock()
        if usable_jj:
            adapter.discover_root.return_value = root
        else:
            adapter.discover_root.side_effect = JjError("not jj")
        adapter.preflight.return_value = RepositoryFacts(root, root)
        adapter.working_copy_parent.return_value = "a" * 40
        adapter.git_head.return_value = "a" * 40
        adapter.git_head_exact.return_value = "a" * 40
        with contextlib.chdir(root), \
                mock.patch("lib.control.doctor.shutil.which", return_value="/fake/jj"), \
                mock.patch("lib.control.doctor.JjAdapter", return_value=adapter):
            result = run_doctor(
                self.config, probes={"repository": DEFAULT_PROBES["repository"]},
            )
        return result["probes"][0]

    def test_doctor_refuses_ambiguous_intent_for_plain_and_usable_jj_roots(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)

        plain = self.doctor(self.source, usable_jj=False)
        self.assertEqual(plain["outcome"], "mismatch")
        self.assertIn("ambiguous", plain["detail"])
        self.assertIn("task start will refuse", plain["detail"])

        (self.source / ".jj").mkdir()
        usable = self.doctor(self.source, usable_jj=True)
        self.assertEqual(usable["outcome"], "mismatch")
        self.assertIn("ambiguous", usable["detail"])

    def test_doctor_accepts_verified_usable_intent_and_reports_stale_missing_jj(self) -> None:
        store = ColocationIntentStore(self.config)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)

        verified = self.doctor(self.source, usable_jj=True)
        self.assertEqual(verified["outcome"], "match")

        shutil.rmtree(self.source / ".jj")
        stale = self.doctor(self.source, usable_jj=False)
        self.assertEqual(stale["outcome"], "mismatch")
        self.assertIn("verified", stale["detail"])
        self.assertIn(".jj", stale["detail"])
        self.assertIn("task start will refuse", stale["detail"])

    def test_doctor_reports_root_hardening_candidate_as_repairable_without_rewrite(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o775)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        before = store.path(self.source).read_bytes()
        self.source.chmod(0o755)

        probe = self.doctor(self.source, usable_jj=True)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertIn("repairable", probe["detail"])
        self.assertIn("task start", probe["detail"])
        self.assertIn("read-only", probe["detail"])
        self.assertEqual(store.path(self.source).read_bytes(), before)

    def test_doctor_reports_device_rebind_candidate_as_repairable_without_rewrite(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o755)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        path = store.path(self.source)
        value = json.loads(path.read_bytes())
        old_dev = value["root_fact"]["dev"] + 1000
        for fact in (
            value["root_fact"],
            value["git_binding"]["marker_fact"],
            value["git_binding"]["target_fact"],
            value["jj_fact"],
        ):
            fact["dev"] = old_dev
        before = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.write_bytes(before)
        os.chmod(path, 0o600)

        probe = self.doctor(self.source, usable_jj=True)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertIn("device renumbering", probe["detail"])
        self.assertIn("repairable", probe["detail"])
        self.assertIn("read-only", probe["detail"])
        self.assertIn("only stored device observations", probe["detail"])
        self.assertEqual(path.read_bytes(), before)

    def test_doctor_does_not_call_unsafe_remaining_permissions_repairable(self) -> None:
        store = ColocationIntentStore(self.config)
        self.source.chmod(0o777)
        store.begin(self.source)
        (self.source / ".jj").mkdir()
        store.mark_verified(self.source)
        before = store.path(self.source).read_bytes()
        self.source.chmod(0o757)

        probe = self.doctor(self.source, usable_jj=True)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertNotIn("repairable", probe["detail"])
        self.assertIn("unsafe", probe["detail"])
        self.assertEqual(store.path(self.source).read_bytes(), before)

    def test_doctor_reports_gitdir_binding_mismatch(self) -> None:
        other_work = self.root / "other-work"
        metadata_a = self.root / "doctor-metadata-a"
        metadata_b = self.root / "doctor-metadata-b"
        subprocess.run(
            ["git", "init", "-q", f"--separate-git-dir={metadata_a}", str(other_work)],
            check=True,
        )
        subprocess.run(
            ["git", "init", "-q", f"--separate-git-dir={metadata_b}",
             str(self.root / "target-b")], check=True,
        )
        (other_work / ".asha").mkdir()
        (other_work / ".asha" / "config.json").write_text("{}\n", encoding="utf-8")
        (other_work / "Memory").mkdir()
        (other_work / "Memory" / "activeContext.md").write_text(
            "# Active\n", encoding="utf-8",
        )
        store = ColocationIntentStore(self.config)
        store.begin(other_work.resolve())
        (other_work / ".jj").mkdir()
        store.mark_verified(other_work.resolve())
        (other_work / ".git").write_text(
            f"gitdir: {metadata_b}\n", encoding="utf-8",
        )

        probe = self.doctor(other_work.resolve(), usable_jj=True)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertIn("binding", probe["detail"])
        self.assertIn("task start will refuse", probe["detail"])

    def test_doctor_refuses_real_linked_worktree_before_intent_creation(self) -> None:
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "-C", str(self.source), "add", ".asha", "Memory"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        linked = self.root / "doctor-linked"
        subprocess.run(
            ["git", "-C", str(self.source), "worktree", "add", "-q", "-b",
             "doctor-linked", str(linked)], check=True,
        )

        probe = self.doctor(linked.resolve(), usable_jj=False)

        self.assertEqual(probe["outcome"], "mismatch")
        self.assertIn("linked Git worktree", probe["detail"])
        self.assertIn("primary worktree", probe["detail"])
        self.assertIn("manual", probe["detail"])
        self.assertIsNone(ColocationIntentStore(self.config).read(linked.resolve()))


if __name__ == "__main__":
    unittest.main()
