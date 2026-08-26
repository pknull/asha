"""Durable per-run worker logs and the codex headless start flag.

Two properties are pinned here.  A worker run must leave a diagnosable record:
its pane output is piped into an ordinary file under the Control state
directory from the pane's first byte, and that file outlives the pane process.
A codex worker must be able to start at all inside a jj workspace, which
carries `.jj` and never its own `.git`.

Nothing here requires tmux, a TTY, or the network.
"""

from __future__ import annotations

import contextlib
import stat
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lib.control import launch as control_launch
from lib.control.config import load_config
from lib.control.harness import HarnessError, launch_argv
from lib.control.launch import launch_task
from lib.control.store import TaskStore, task_digest
from lib.control.tmux import TmuxAdapter, TmuxError
from lib.control.transaction import CreationJournalStore
from tests.python.test_control_increment3 import FakeTmux


class PipePaneAdapterTests(unittest.TestCase):
    """The tmux seam that opens the durable pipe."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.calls: list[tuple[list[str], dict]] = []

    def adapter(self, returncode: int = 0, stderr: bytes = b"") -> TmuxAdapter:
        def runner(argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return subprocess.CompletedProcess(argv, returncode, b"", stderr)

        return TmuxAdapter(socket="asha-control-test", runner=runner)

    def test_pipe_pane_issues_one_output_only_pipe_for_the_named_pane(self) -> None:
        destination = self.logs / "run.log"

        self.adapter().pipe_pane("%7", destination)

        self.assertEqual(
            [call[0] for call in self.calls],
            [[
                "tmux", "-L", "asha-control-test", "pipe-pane", "-o",
                "-t", "%7", f"cat >> {destination}",
            ]],
        )
        self.assertTrue(all(not call[1]["shell"] for call in self.calls))

    def test_pipe_pane_quotes_a_destination_the_shell_would_otherwise_split(self) -> None:
        directory = self.root / "log dir"
        directory.mkdir()
        destination = directory / "run.log"

        self.adapter().pipe_pane("%7", destination)

        self.assertEqual(self.calls[0][0][-1], f"cat >> '{destination}'")

    def test_the_piped_command_writes_bytes_that_outlive_the_writer(self) -> None:
        destination = self.logs / "run.log"
        self.adapter().pipe_pane("%7", destination)
        command = self.calls[0][0][-1]

        first = subprocess.run(
            ["/bin/sh", "-c", command], input=b"first line\n", check=True,
        )
        second = subprocess.run(
            ["/bin/sh", "-c", command], input=b"second line\n", check=True,
        )

        # Both writers have exited; the record they left is still on disk.
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(destination.read_bytes(), b"first line\nsecond line\n")

    def test_pipe_pane_refuses_an_invalid_pane_id_before_invoking_tmux(self) -> None:
        adapter = self.adapter()
        for pane in ("bad", "%", "", "%1;kill", None, "%1\n"):
            with self.subTest(pane=pane):
                with self.assertRaises(TmuxError):
                    adapter.pipe_pane(pane, self.logs / "run.log")
        self.assertEqual(self.calls, [])

    def test_pipe_pane_refuses_shell_and_tmux_format_metacharacters(self) -> None:
        adapter = self.adapter()
        names = (
            "a;id.log", "a$(id).log", "a`id`.log", "a|b.log", "a&b.log",
            "a>b.log", "a<b.log", "a'b.log", 'a"b.log', "a*b.log", "a?b.log",
            "a[b].log", "a{b}.log", "a!b.log", "a~b.log", "a\\b.log",
            "a\nb.log", "a\tb.log", "a\x00b.log", "a​b.log", "aä.log",
            # tmux expands the pipe-pane command as a FORMAT before /bin/sh
            # ever sees it, so '#' is as dangerous here as '$'.
            "a#{pane_id}.log", "a#(id).log", "a#b.log",
        )
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(TmuxError):
                    adapter.pipe_pane("%7", self.logs / name)
        self.assertEqual(self.calls, [])

    def test_pipe_pane_refuses_a_destination_that_is_not_canonical(self) -> None:
        adapter = self.adapter()
        candidates = (
            "relative/run.log",
            f"{self.logs}/../logs/run.log",
            f"{self.logs}//run.log",
            f"{self.logs}/",
            "",
            42,
            None,
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises(TmuxError):
                    adapter.pipe_pane("%7", candidate)
        self.assertEqual(self.calls, [])

    def test_pipe_pane_refuses_a_destination_reached_through_a_symlink(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.logs)

        with self.assertRaises(TmuxError):
            self.adapter().pipe_pane("%7", link / "run.log")
        self.assertEqual(self.calls, [])

    def test_pipe_pane_reports_a_failing_tmux_invocation(self) -> None:
        adapter = self.adapter(returncode=1, stderr=b"can't find pane: %7\n")

        with self.assertRaisesRegex(TmuxError, "can't find pane"):
            adapter.pipe_pane("%7", self.logs / "run.log")


class RecordingTmux(FakeTmux):
    """A FakeTmux that records the order of the two pane-touching calls."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.order: list[str] = []
        self.piped: list[str] = []

    def pipe_pane(self, pane_id, path):
        self.order.append("pipe-pane")
        self.piped.append(str(path))

    def respawn(self, pane_id, argv):
        self.order.append("respawn")
        super().respawn(pane_id, argv)


class WorkerRunLogTests(unittest.TestCase):
    """Every launch leaves a durable, discoverable per-run log."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        self.source = self.root / "source"
        self.source.mkdir()
        self.source.chmod(0o755)
        self.tasks = TaskStore(self.config)
        self.journals = CreationJournalStore(self.config)

    @staticmethod
    def timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    def prepared(self, index: int = 0) -> dict:
        task_id = str(uuid.uuid4())
        slug = f"worker-log-{index}-{task_id[:6]}"
        workspace = self.config.workspace_root / "repo-key" / slug
        workspace.mkdir(parents=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        timestamp = self.timestamp()
        task = {
            "contract": "asha.control-task.v1",
            "task_id": task_id,
            "slug": slug,
            "label": "Worker log fixture",
            "created_at": timestamp,
            "updated_at": timestamp,
            "lifecycle": "creating",
            "repository": {"root": str(self.source), "identity": "repo:" + "a" * 64},
            "source": {"kind": "ad-hoc", "number": None, "url": None},
            "jj": {
                "workspace_name": f"asha-{slug}-{task_id[:8]}",
                "workspace_path": str(workspace),
                "requested_base": "trunk()",
                "base_commit_id": "b" * 40,
                "change_id": "k" * 32,
                "working_commit_id": "c" * 40,
            },
            "tmux": {
                "socket": "default",
                "session": f"asha-{slug}-{task_id[:8]}",
                "window": "work",
            },
            "runs": [],
        }
        self.tasks.save(task)
        journal = {
            "contract": "asha.control-creation-journal.v1",
            "task_id": task_id,
            "invocation_id": "d" * 32,
            "phase": "intent",
            "launch_attempted": False,
            "config": {
                "workspace_root": str(self.config.workspace_root),
                "tasks_dir": str(self.config.tasks_dir),
                "runtime_dir": str(self.config.runtime_dir),
            },
            "repository": {
                "root": str(self.source),
                "identity": task["repository"]["identity"],
                "git_root": str(self.source),
                "repo_key": "repo-key",
            },
            "task": {
                "record_path": str(self.config.tasks_dir / f"{task_id}.json"),
                "slug": slug,
                "label": task["label"],
                "digest": task_digest(task),
                "failure": None,
            },
            "workspace": {
                "path": str(workspace),
                "name": task["jj"]["workspace_name"],
                "root_fact": None,
                "created_parents": [],
            },
            "jj": {
                "pinned_operation_id": "e" * 128,
                "base_commit_id": task["jj"]["base_commit_id"],
                "change_id": task["jj"]["change_id"],
                "working_commit_id": task["jj"]["working_commit_id"],
                "description": task["label"],
                "registration_state": "present",
                "last_registration": {
                    "change_id": task["jj"]["change_id"],
                    "working_commit_id": task["jj"]["working_commit_id"],
                },
            },
            "expected_materialization": {},
            "materialized_owned": None,
            "recovery_owned": None,
            "planned_context": None,
            "context_owned": {},
            "removal": {
                "entries_removed": 0, "root_removed": False, "parents_removed": 0,
            },
        }
        self.journals.save(journal)
        phases = (
            "task-recorded", "parent-intent", "parent-ready",
            "workspace-add-intent", "workspace-added", "workspace-recorded",
            "context-intent", "context-provisioning", "context-provisioned",
            "task-identity-intent", "task-identity-recorded", "ready-for-launch",
        )
        for phase in phases:
            previous = journal["phase"]
            journal["phase"] = phase
            self.journals.save(journal, expected_phase=previous)
        return task

    @contextlib.contextmanager
    def process_evidence(self):
        with mock.patch(
            "lib.control.launch.harness_api.process_identity",
            return_value="boot:11111111-2222-4333-8444-555555555555:start:99",
        ), mock.patch(
            "lib.control.launch.harness_api.pane_ancestry_ok", return_value=True,
        ):
            yield

    def launch(self, index: int = 0) -> tuple[RecordingTmux, dict, dict]:
        task = self.prepared(index)
        adapter = RecordingTmux()
        with self.process_evidence():
            result = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Do work",),
            )
        return adapter, task, result

    def test_run_log_path_is_derived_from_the_run_id_alone(self) -> None:
        run_id = str(uuid.uuid4())

        path = control_launch.run_log_path(self.config, run_id)

        self.assertEqual(path.name, f"{run_id}.log")
        self.assertEqual(path.parent.parent, self.config.tasks_dir.parent)
        # Under the Control state directory, never inside a task workspace.
        self.assertNotIn(self.config.workspace_root, [path, *path.parents])
        self.assertEqual(path, control_launch.run_log_path(self.config, run_id))
        with self.assertRaises(ValueError):
            control_launch.run_log_path(self.config, "not-a-uuid")

    def test_launch_opens_the_log_before_the_worker_command_is_respawned(self) -> None:
        adapter, _task, result = self.launch(1)

        expected = control_launch.run_log_path(self.config, result["run"]["run_id"])
        self.assertEqual(adapter.order, ["pipe-pane", "respawn"])
        self.assertEqual(adapter.piped, [str(expected)])

    def test_launch_creates_an_owner_only_log_file(self) -> None:
        _adapter, _task, result = self.launch(2)

        log = control_launch.run_log_path(self.config, result["run"]["run_id"])
        self.assertTrue(log.is_file())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(log.read_bytes(), b"")

    def test_launch_refuses_to_write_through_a_planted_log_symlink(self) -> None:
        task = self.prepared(8)
        target = self.root / "elsewhere"
        target.write_text("untouched", encoding="ascii")
        logs = self.config.tasks_dir.parent / "logs"
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        adapter = RecordingTmux()

        derive = control_launch.run_log_path

        def planted(config, run_id):
            path = derive(config, run_id)
            path.symlink_to(target)
            return path

        with self.process_evidence(), mock.patch(
            "lib.control.launch.run_log_path", side_effect=planted,
        ), mock.patch("lib.control.launch.rollback_prelaunch"):
            with self.assertRaisesRegex(ValueError, "run log could not be opened"):
                launch_task(
                    self.config, task, tmux=adapter, tasks=self.tasks,
                    journals=self.journals, harness="codex",
                    goal_args=("Do work",),
                )

        self.assertEqual(target.read_text(encoding="ascii"), "untouched")
        self.assertEqual(adapter.order, [])

    def test_the_run_record_carries_the_log_path(self) -> None:
        _adapter, task, result = self.launch(3)

        log = control_launch.run_log_path(self.config, result["run"]["run_id"])
        self.assertIn(f"log={log}", result["run"]["evidence"])
        persisted = self.tasks.read(task["task_id"])
        self.assertEqual(len(persisted["runs"]), 1)
        # A reader with only the durable record can find the log.
        recorded = persisted["runs"][0]["evidence"].split("log=", 1)[1]
        self.assertEqual(Path(recorded), log)

    def test_the_log_outlives_the_pane_and_the_tmux_session(self) -> None:
        adapter, task, result = self.launch(4)
        log = control_launch.run_log_path(self.config, result["run"]["run_id"])

        with log.open("ab") as handle:
            handle.write(b"worker output\n")
        adapter.dead = True
        adapter.kill_session(task["tmux"]["session"])

        self.assertTrue(adapter.killed)
        self.assertFalse(adapter.present)
        self.assertEqual(log.read_bytes(), b"worker output\n")

    def test_each_run_gets_its_own_log(self) -> None:
        _first_adapter, _first_task, first = self.launch(5)
        _second_adapter, _second_task, second = self.launch(6)

        logs = [
            control_launch.run_log_path(self.config, result["run"]["run_id"])
            for result in (first, second)
        ]
        self.assertNotEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertNotEqual(logs[0], logs[1])
        self.assertEqual(
            {log.parent for log in logs},
            {self.config.tasks_dir.parent / "logs"},
        )
        self.assertTrue(all(log.is_file() for log in logs))

    def test_the_launch_result_still_carries_only_its_frozen_v1_keys(self) -> None:
        # `_emit_start_result` spreads this dict straight into the
        # asha.control-task-start.v1 closed payload that the orchestration
        # scheduler validates by exact key set, so a convenience key added
        # here silently breaks every real dispatch.
        _adapter, _task, result = self.launch(7)

        self.assertEqual(
            set(result),
            {"task", "run", "session", "pane", "workspace", "workspace_trust"},
        )


class CodexHeadlessStartTests(unittest.TestCase):
    """`codex exec` must start inside a jj workspace, which has no .git."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.executable = self.root / "bin" / "asha"
        self.executable.parent.mkdir()
        self.executable.write_text("#!/bin/sh\n", encoding="ascii")
        self.executable.chmod(0o700)

    def test_codex_headless_argv_skips_the_git_repository_check(self) -> None:
        self.assertEqual(
            launch_argv(self.root, "codex", ("Do the thing",), headless=True),
            [str(self.executable), "codex", "exec", "--skip-git-repo-check",
             "Do the thing"],
        )

    def test_codex_headless_argv_keeps_the_flag_ahead_of_every_argument(self) -> None:
        argv = launch_argv(
            self.root, "codex", ("Do the thing", "--extra"), headless=True,
        )

        self.assertEqual(argv[:4], [
            str(self.executable), "codex", "exec", "--skip-git-repo-check",
        ])
        self.assertEqual(argv[4:], ["Do the thing", "--extra"])
        self.assertEqual(argv.count("--skip-git-repo-check"), 1)

    def test_interactive_codex_argv_is_unchanged(self) -> None:
        self.assertEqual(
            launch_argv(self.root, "codex", ("resume", "session-id")),
            [str(self.executable), "codex", "resume", "session-id"],
        )

    def test_claude_headless_argv_is_byte_identical_to_the_baseline(self) -> None:
        self.assertEqual(
            launch_argv(self.root, "claude", ("Do the thing",), headless=True),
            [str(self.executable), "claude", "-p", "Do the thing",
             "--permission-mode", "bypassPermissions"],
        )

    def test_interactive_claude_argv_is_unchanged(self) -> None:
        self.assertEqual(
            launch_argv(self.root, "claude", ("Do the thing",)),
            [str(self.executable), "claude", "Do the thing"],
        )

    def test_harnesses_without_a_headless_mode_are_still_refused(self) -> None:
        for harness in ("copilot", "opencode"):
            with self.subTest(harness=harness):
                with self.assertRaisesRegex(HarnessError, "no headless mode"):
                    launch_argv(self.root, harness, ("x",), headless=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
