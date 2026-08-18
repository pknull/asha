from __future__ import annotations

import contextlib
import copy
import errno
import io
import json
import os
import pty
import re
import shutil
import signal
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lib.control.cli import _parse_start, _run_popup, _start_command, main as control_main
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.events import read_snapshot, write_snapshot
from lib.control.harness import (
    HarnessError,
    boot_id,
    controller_env,
    launch_argv,
    pane_ancestry_ok,
    process_identity,
    process_start_ticks,
    stop_signal_allowed,
    verify_process,
)
from lib.control.jj import DEFAULT_BASE_REVSET, JjAdapter, RepositoryFacts, WorkspaceIdentity
from lib.control.launch import (
    LaunchError, archive_task, launch_task, recover_task, stop_task,
    unarchive_task,
)
from lib.control.model import RUN_CONTRACT, validate_run, validate_task
from lib.control.prepare import prepare_task_workspace
from lib.control.reconcile import Evidence, LiveAdapters, reconcile_task
from lib.control.store import StoreError, TaskStore, task_digest
from lib.control.tmux import (
    PaneFacts, TmuxAdapter, TmuxError, _validate_argv, _validate_pane_id,
)
from lib.control.transaction import (
    CreationJournalStore, JournalError, PHASES, PHASE_TRANSITIONS,
)
from tests.python.test_control_config_model import task_record


class TmuxAdapterTests(unittest.TestCase):
    def create_arguments(self) -> dict:
        return {
            "session": "asha-task-12345678",
            "window": "work",
            "start_directory": "/work/task",
            "environment": {"ASHA_CONTROL_MANAGED": "1", "RUN_MODE": "test"},
            "holder_argv": ["/repo/bin/asha", "claude", "--resume"],
            "session_options": {"@asha_managed": "1"},
            "pane_options": {"@asha_run_id": "11111111-1111-4111-8111-111111111111"},
            "pane_title": "Primary run",
        }

    def test_command_argv_rejects_tmux_command_separators_only_at_token_end(self) -> None:
        for argv in (["x", "goal;"], [";"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(
                TmuxError, "tmux command argv is invalid",
            ):
                _validate_argv(argv)
        self.assertEqual(_validate_argv(["a;b"]), ["a;b"])

    def test_create_task_session_is_one_chained_argv_only_invocation(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["tmux"], 0, b"%12\n", b"",
        ))
        adapter = TmuxAdapter(socket="asha-control", runner=run)

        pane_id = adapter.create_task_session(**self.create_arguments())

        self.assertEqual(pane_id, "%12")
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["tmux", "-L", "asha-control", "new-session"])
        self.assertEqual(
            argv[4:16],
            [
                "-d", "-P", "-F", "#{pane_id}",
                "-s", "asha-task-12345678", "-n", "work",
                "-c", "/work/task", "-e", "ASHA_CONTROL_MANAGED=1",
            ],
        )
        self.assertIn("RUN_MODE=test", argv)
        holder_separator = argv.index("--")
        self.assertEqual(
            argv[holder_separator + 1:holder_separator + 4],
            ["/repo/bin/asha", "claude", "--resume"],
        )
        self.assertIn(
            ["set-option", "-t", "asha-task-12345678:", "remain-on-exit", "on"],
            self._chained_commands(argv),
        )
        self.assertIn(
            ["set-option", "-t", "asha-task-12345678:", "automatic-rename", "off"],
            self._chained_commands(argv),
        )
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertTrue(all(";" not in token or token == ";" for token in argv))

    def test_remain_on_exit_precedes_other_chained_session_setup(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["tmux"], 0, b"%3\n", b"",
        ))

        TmuxAdapter(runner=run).create_task_session(**self.create_arguments())

        argv = run.call_args.args[0]
        commands = self._chained_commands(argv)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            commands[1],
            ["set-option", "-t", "asha-task-12345678:", "remain-on-exit", "on"],
        )
        self.assertEqual(
            commands[2],
            ["set-option", "-t", "asha-task-12345678:", "automatic-rename", "off"],
        )

    def test_respawn_is_argv_only(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["tmux"], 0, b"", b"",
        ))

        TmuxAdapter(runner=run).respawn("%9", ["/repo/bin/asha", "codex", "resume"])

        self.assertEqual(
            run.call_args.args[0],
            ["tmux", "respawn-pane", "-k", "-t", "%9", "--",
             "/repo/bin/asha", "codex", "resume"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_popup_argv_is_plain_tokens_with_socket_on_both_tmux_calls(self) -> None:
        argv = TmuxAdapter(socket="asha-control").popup_argv(
            client="/dev/pts/7", session="asha-task-12345678",
            width="90%", height="85%",
        )

        self.assertEqual(
            argv,
            [
                "tmux", "-L", "asha-control", "display-popup",
                "-c", "/dev/pts/7", "-E",
                "-w", "90%", "-h", "85%", "--",
                "tmux", "-L", "asha-control", "attach-session", "-t",
                "asha-task-12345678",
            ],
        )
        self.assertEqual(argv.count("asha-task-12345678"), 1)
        self.assertTrue(all("$(" not in token and "`" not in token and ";" not in token
                            for token in argv))
        self.assertFalse(any(" " in token for token in argv))
        self.assertFalse(any("tmux attach-session" in token for token in argv))

    def test_session_option_distinguishes_missing_from_other_failures(self) -> None:
        missing = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, b"", b"invalid option: @asha_managed\n",
        ))
        present = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, b"1\n", b"",
        ))
        failed = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, b"", b"permission denied\n",
        ))

        self.assertIsNone(missing.session_option("asha-task", "@asha_managed"))
        self.assertEqual(present.session_option("asha-task", "@asha_managed"), "1")
        with self.assertRaises(TmuxError):
            failed.session_option("asha-task", "@asha_managed")

    def test_pane_facts_preserve_empty_terminal_fields_and_stale_dead_pid(self) -> None:
        outputs = iter([
            b"%7\t1234\t0\t\t\tasha-task\twork\tPrimary\n",
            b"%7\t1234\t1\t17\t\tasha-task\twork\tPrimary\n",
            b"%7\t1234\t1\t\t15\tasha-task\twork\tPrimary\n",
        ])
        adapter = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, next(outputs), b"",
        ))

        live = adapter.pane_facts("%7")
        exited = adapter.pane_facts("%7")
        signalled = adapter.pane_facts("%7")

        self.assertEqual(live, PaneFacts("%7", 1234, False, None, None,
                                         "asha-task", "work", "Primary"))
        self.assertTrue(exited.dead)
        self.assertEqual(exited.pane_pid, 1234)
        self.assertEqual(exited.dead_status, 17)
        self.assertIsNone(exited.dead_signal)
        self.assertIsNone(signalled.dead_status)
        self.assertEqual(signalled.dead_signal, 15)

    def test_hostile_inputs_are_rejected_before_invocation(self) -> None:
        run = mock.Mock(side_effect=AssertionError("tmux must not run"))
        adapter = TmuxAdapter(runner=run)
        base = self.create_arguments()
        cases = []
        for field, value in (
            ("pane_title", "bad #{pane_pid}"),
            ("session", "bad session"),
            ("start_directory", "relative/path"),
        ):
            arguments = {**base, field: value}
            cases.append(lambda arguments=arguments: adapter.create_task_session(**arguments))
        for value in ("bad;value", "bad\nvalue", "bad\x7fvalue"):
            arguments = {**base, "session_options": {"@asha_managed": value}}
            cases.append(lambda arguments=arguments: adapter.create_task_session(**arguments))
        arguments = {**base, "environment": {"PATH; echo": "bad"}}
        cases.append(lambda: adapter.create_task_session(**arguments))
        cases.append(lambda: adapter.respawn("%1; kill-server", ["true"]))

        for case in cases:
            with self.subTest(case=case), self.assertRaises(TmuxError):
                case()
        self.assertEqual(run.call_count, 0)

    def test_environment_values_have_a_4096_bound_without_relaxing_options(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["tmux"], 0, b"%1\n", b"",
        ))
        adapter = TmuxAdapter(runner=run)
        arguments = self.create_arguments()
        arguments["environment"] = {"ASHA_CONTROL_STATE_DIR": "/" + "a" * 999}
        adapter.create_task_session(**arguments)
        self.assertIn(
            "ASHA_CONTROL_STATE_DIR=/" + "a" * 999,
            run.call_args.args[0],
        )
        too_long = self.create_arguments()
        too_long["environment"] = {"ASHA_CONTROL_STATE_DIR": "/" + "a" * 4096}
        with self.assertRaises(TmuxError):
            adapter.create_task_session(**too_long)
        long_option = self.create_arguments()
        long_option["session_options"] = {"@asha_managed": "a" * 201}
        with self.assertRaises(TmuxError):
            adapter.create_task_session(**long_option)

    def test_status_and_selection_methods_use_socket_prefixed_argv(self) -> None:
        responses = iter([
            subprocess.CompletedProcess(["tmux"], 0, b"4242\n", b""),
            subprocess.CompletedProcess(["tmux"], 0, b"", b""),
            subprocess.CompletedProcess(["tmux"], 1, b"", b"can't find session: missing\n"),
            subprocess.CompletedProcess(["tmux"], 0, b"", b""),
        ])
        run = mock.Mock(side_effect=lambda argv, **kwargs: next(responses))
        adapter = TmuxAdapter(socket="asha-control", runner=run)

        self.assertEqual(adapter.server_pid(), 4242)
        self.assertTrue(adapter.has_session("present"))
        self.assertFalse(adapter.has_session("missing"))
        adapter.select_target("present", "work", "%2")

        calls = [item.args[0] for item in run.call_args_list]
        self.assertTrue(all(call[:3] == ["tmux", "-L", "asha-control"] for call in calls))
        self.assertEqual(
            calls[-1][3:],
            ["select-window", "-t", "present:work", ";", "select-pane", "-t", "%2"],
        )

    def test_injected_runner_output_is_bounded(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["tmux"], 0, b"x" * 1025, b"",
        ))
        adapter = TmuxAdapter(runner=run)

        with self.assertRaisesRegex(TmuxError, "bounded"):
            adapter._run_bytes("tmux", ["display-message"], limit=1024)
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_integration_snippet_uses_option_ownership_not_prefix_identity(self) -> None:
        snippet = TmuxAdapter().integration_snippet(session_prefix="asha-")

        self.assertIn("@asha_managed", snippet)
        self.assertIn("asha-", snippet)
        self.assertNotIn("#{m:asha-", snippet)
        self.assertNotIn("#{==:#{session_name},asha-", snippet)

    @staticmethod
    def _chained_commands(argv: list[str]) -> list[list[str]]:
        commands: list[list[str]] = [[]]
        for token in argv:
            if token == ";":
                commands.append([])
            else:
                commands[-1].append(token)
        return commands


class HarnessAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.proc / "sys" / "kernel" / "random").mkdir(parents=True)
        self.boot = "11111111-2222-4333-8444-555555555555"
        (self.proc / "sys" / "kernel" / "random" / "boot_id").write_text(
            self.boot + "\n", encoding="ascii",
        )
        self.proc_patch = mock.patch("lib.control.harness.PROC_ROOT", self.proc)
        self.proc_patch.start()
        self.addCleanup(self.proc_patch.stop)

    def write_stat(self, pid: int, *, ppid: int, ticks: int,
                   comm: str = "asha") -> None:
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields_three_through_twenty_one = ["S", str(ppid), *(["0"] * 17)]
        (directory / "stat").write_text(
            f"{pid} ({comm}) "
            + " ".join([*fields_three_through_twenty_one, str(ticks), "0", "0"])
            + "\n",
            encoding="ascii",
        )

    def test_process_stat_uses_last_parenthesis_for_ticks_and_ppid(self) -> None:
        self.write_stat(818820, ppid=818815, ticks=987654,
                        comm="my (weird) prog")

        self.assertEqual(process_start_ticks(818820), 987654)
        self.assertTrue(pane_ancestry_ok(818820, 818815))
        self.assertFalse(pane_ancestry_ok(818820, 999999))

    def test_process_identity_is_missing_for_gone_pid(self) -> None:
        self.assertIsNone(process_start_ticks(77))
        self.assertIsNone(process_identity(77))

    def test_process_identity_satisfies_run_model_constraints(self) -> None:
        self.write_stat(1001, ppid=91, ticks=123456)

        identity = process_identity(1001)

        self.assertEqual(identity, f"boot:{self.boot}:start:123456")
        self.assertLessEqual(len(identity or ""), 200)
        validate_run({
            "contract": RUN_CONTRACT,
            "run_id": str(uuid.uuid4()),
            "harness": "claude",
            "role": "primary",
            "pane_id": "%1",
            "pid": 1001,
            "process_start_identity": identity,
            "harness_session_id": None,
            "state": "starting",
            "evidence": "test fixture",
            "evidence_at": "2026-08-15T12:00:00Z",
        })

    def test_verify_process_rejects_reused_pid_starttime(self) -> None:
        self.write_stat(1002, ppid=91, ticks=111)
        expected = process_identity(1002)
        self.write_stat(1002, ppid=91, ticks=222)

        self.assertFalse(verify_process(1002, expected or ""))

    def test_stop_signal_gate_rejects_dead_pane(self) -> None:
        values = self.signal_values()
        values["pane_dead"] = True
        self.assertFalse(stop_signal_allowed(**values))

    def test_stop_signal_gate_rejects_pid_different_from_pane_pid(self) -> None:
        values = self.signal_values()
        values["pane_pid"] = 2002
        self.assertFalse(stop_signal_allowed(**values))

    def test_stop_signal_gate_rejects_identity_mismatch(self) -> None:
        values = self.signal_values()
        values["expected_identity"] = f"boot:{self.boot}:start:999"
        self.assertFalse(stop_signal_allowed(**values))

    def test_stop_signal_gate_rejects_wrong_tmux_ancestry(self) -> None:
        values = self.signal_values()
        values["server_pid"] = 9999
        self.assertFalse(stop_signal_allowed(**values))

    def test_stop_signal_gate_accepts_all_matching_evidence(self) -> None:
        self.assertTrue(stop_signal_allowed(**self.signal_values()))

    def test_controller_env_is_exact_and_validated(self) -> None:
        task_id = "11111111-1111-4111-8111-111111111111"
        run_id = "22222222-2222-4222-8222-222222222222"
        state_dir = self.root / "state"

        self.assertEqual(
            controller_env(task_id=task_id, run_id=run_id, state_dir=state_dir),
            {
                "ASHA_CONTROL_TASK_ID": task_id,
                "ASHA_CONTROL_RUN_ID": run_id,
                "ASHA_CONTROL_STATE_DIR": str(state_dir),
                "ASHA_CONTROL_MANAGED": "1",
            },
        )
        with self.assertRaises(HarnessError):
            controller_env(task_id="not-a-uuid", run_id=run_id, state_dir=state_dir)
        with self.assertRaises(HarnessError):
            controller_env(task_id=task_id, run_id=run_id, state_dir=Path("relative"))

    def test_launch_argv_validates_root_harness_and_extra_arguments(self) -> None:
        executable = self.root / "bin" / "asha"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\n", encoding="ascii")
        executable.chmod(0o700)

        self.assertEqual(
            launch_argv(self.root, "codex", ("resume", "session-id")),
            [str(executable), "codex", "resume", "session-id"],
        )
        with self.assertRaises(HarnessError):
            launch_argv(self.root, "unknown")
        with self.assertRaises(HarnessError):
            launch_argv(self.root, "codex", ("bad\x00argument",))
        with self.assertRaisesRegex(HarnessError, "must not begin"):
            launch_argv(self.root, "codex", ("--operator-goal",))

    def test_process_lookup_race_is_treated_as_missing(self) -> None:
        with mock.patch.object(
            Path, "open", side_effect=OSError(errno.ESRCH, "process disappeared"),
        ):
            self.assertIsNone(process_start_ticks(9911))

    def test_boot_id_rejects_malformed_content(self) -> None:
        path = self.proc / "sys" / "kernel" / "random" / "boot_id"
        path.write_text("not-a-uuid\n", encoding="ascii")

        with self.assertRaises(HarnessError):
            boot_id()

    def signal_values(self) -> dict:
        self.write_stat(2001, ppid=3001, ticks=4001)
        return {
            "pid": 2001,
            "expected_identity": f"boot:{self.boot}:start:4001",
            "pane_pid": 2001,
            "server_pid": 3001,
            "pane_dead": False,
        }


class FakeTmux:
    executable = "tmux"

    def __init__(self, *, collision: bool = False, owner: str | None = None,
                 managed: str | None = None) -> None:
        self.socket = "asha-control-test"
        self.present = collision
        self.session_options: dict[str, str] = {}
        if collision:
            if managed is not None:
                self.session_options["@asha_managed"] = managed
            if owner is not None:
                self.session_options["@asha_task_id"] = owner
        self.pane_options: dict[str, str] = {}
        self.killed = False
        self.respawned = False
        self.dead = False
        self.pid = 4242
        self.session = ""
        self.window = "work"

    def has_session(self, name):
        return self.present

    def session_option(self, name, option):
        return self.session_options.get(option)

    def pane_option(self, pane_id, option):
        return self.pane_options.get(option)

    def create_task_session(self, **kwargs):
        self.present = True
        self.session = kwargs["session"]
        self.window = kwargs["window"]
        self.session_options = dict(kwargs["session_options"])
        self.pane_options = dict(kwargs["pane_options"])
        return "%1"

    def respawn(self, pane_id, argv):
        _validate_argv(argv)
        self.respawned = True

    def pane_facts(self, pane_id):
        _validate_pane_id(pane_id)
        return PaneFacts(
            pane_id, self.pid, self.dead, None, None,
            self.session or "asha-control-test", self.window, "asha:codex:implementer",
        )

    def window_pane_facts(self, session, window):
        return PaneFacts(
            "%1", self.pid, self.dead, None, None,
            session, window, "asha:codex:implementer",
        )

    def server_pid(self):
        return 3131

    def kill_session(self, name):
        self.killed = True
        self.present = False


class DerivedRunAdapters:
    def __init__(self, state: str) -> None:
        self.state = state

    def tmux(self, task, run):
        if self.state in {"exited", "failed"}:
            return Evidence("tmux", "missing", "owned pane has exited")
        return Evidence("tmux", "match", "owned pane matched")

    def process(self, task, run):
        if self.state in {"exited", "failed"}:
            return Evidence(
                "process", "missing", "process exited", state=self.state,
            )
        return Evidence("process", "match", "process identity matched")

    def jj(self, task):
        return Evidence("jj", "match", "workspace identity matched")

    def event(self, task, run):
        return Evidence(
            "event", "match", "verified session-ended event snapshot",
            state=self.state,
        )


class LaunchFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
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
        slug = f"launch-{index}-{task_id[:6]}"
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
            "label": "Launch fixture",
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

    def test_launch_phase_graph_contains_only_the_declared_edges(self) -> None:
        expected = {
            "ready-for-launch": {"tmux-intent", "rollback-intent", "preserved"},
            "tmux-intent": {"tmux-session-created", "rollback-intent", "preserved"},
            "tmux-session-created": {"launch-attempted", "rollback-intent", "preserved"},
            "launch-attempted": {"run-recorded", "preserved"},
            "run-recorded": set(),
        }
        for phase, allowed in expected.items():
            self.assertEqual(set(PHASE_TRANSITIONS[phase]), allowed)
            for candidate in PHASES - {phase} - allowed:
                self.assertNotIn(candidate, PHASE_TRANSITIONS[phase])

    def test_success_records_running_and_starting_in_exactly_one_task_save(self) -> None:
        task = self.prepared()
        adapter = FakeTmux()
        with self.process_evidence(), mock.patch.object(
            self.tasks, "save", wraps=self.tasks.save,
        ) as save:
            result = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Do work",),
            )
        self.assertEqual(save.call_count, 1)
        self.assertEqual(result["task"]["lifecycle"], "running")
        self.assertEqual(result["run"]["state"], "starting")
        self.assertEqual(len(result["task"]["runs"]), 1)
        self.assertEqual(self.journals.read(task["task_id"])["phase"], "run-recorded")
        self.assertEqual(result["session"], task["tmux"]["session"])

    def test_failure_injector_boundaries_rollback_before_exec_and_preserve_after(self) -> None:
        boundaries = {
            "validated": False,
            "session-available": False,
            "journal:tmux-intent": False,
            "tmux-intent": False,
            "tmux-created": False,
            "tmux-verified": False,
            "journal:tmux-session-created": False,
            "tmux-session-created": False,
            "journal:launch-attempted": True,
            "launch-attempted": True,
            "respawned": True,
            "process-identified": True,
            "run-saved": True,
            "before:run-recorded": True,
        }
        for index, (boundary, post_launch) in enumerate(boundaries.items(), 1):
            with self.subTest(boundary=boundary):
                task = self.prepared(index)
                adapter = FakeTmux()

                def inject(observed, expected=boundary):
                    if observed == expected:
                        raise RuntimeError("injected boundary")

                with self.process_evidence(), mock.patch(
                    "lib.control.launch.rollback_prelaunch",
                ) as rollback, self.assertRaises(LaunchError) as caught:
                    launch_task(
                        self.config, task, tmux=adapter, tasks=self.tasks,
                        journals=self.journals, harness="codex",
                        goal_args=("Do work",), failure_injector=inject,
                    )
                if post_launch:
                    self.assertFalse(rollback.called)
                    self.assertFalse(adapter.killed)
                    self.assertTrue(Path(task["jj"]["workspace_path"]).exists())
                    self.assertEqual(self.tasks.read(task["task_id"])["lifecycle"], "failed")
                    self.assertEqual(self.journals.read(task["task_id"])["phase"], "preserved")
                    self.assertIn(f"asha task show {task['task_id']}", str(caught.exception))
                    self.assertIn("tmux -L asha-control-test attach-session", str(caught.exception))
                else:
                    rollback.assert_called_once_with(self.config, task["task_id"])

    def test_foreign_and_existing_owned_sessions_are_refused_and_untouched(self) -> None:
        cases = ((None, None, "foreign"), ("1", str(uuid.uuid4()), "foreign"))
        for index, (managed, owner, message) in enumerate(cases, 30):
            with self.subTest(managed=managed, owner=owner):
                task = self.prepared(index)
                adapter = FakeTmux(collision=True, managed=managed, owner=owner)
                with mock.patch("lib.control.launch.rollback_prelaunch"), self.assertRaisesRegex(
                    LaunchError, message,
                ):
                    launch_task(
                        self.config, task, tmux=adapter, tasks=self.tasks,
                        journals=self.journals, harness="codex", goal_args=("Goal",),
                    )
                self.assertFalse(adapter.killed)
                self.assertTrue(adapter.present)
                self.assertEqual(self.journals.read(task["task_id"])["phase"], "preserved")

        task = self.prepared(40)
        adapter = FakeTmux(collision=True, managed="1", owner=task["task_id"])
        with mock.patch("lib.control.launch.rollback_prelaunch"), self.assertRaisesRegex(
            LaunchError, "recovery is not implemented",
        ):
            launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )
        self.assertFalse(adapter.killed)

    def test_second_mutating_launch_is_refused_without_task_or_tmux_mutation(self) -> None:
        task = self.prepared(50)
        adapter = FakeTmux()
        with self.process_evidence():
            launched = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )["task"]
        digest = task_digest(launched)
        with self.assertRaisesRegex(LaunchError, "requires lifecycle creating"):
            launch_task(
                self.config, launched, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Again",),
            )
        self.assertEqual(task_digest(self.tasks.read(task["task_id"])), digest)
        self.assertFalse(adapter.killed)

    def test_symlinked_asha_root_is_resolved_before_launch_argv(self) -> None:
        task = self.prepared(60)
        adapter = FakeTmux()
        alias = self.root / "asha-alias"
        alias.symlink_to(Path(__file__).resolve().parents[2], target_is_directory=True)
        with self.process_evidence(), mock.patch.dict(os.environ, {"ASHA_ROOT": str(alias)}):
            result = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )
        self.assertEqual(result["task"]["lifecycle"], "running")

    def test_trailing_semicolon_goal_is_rejected_before_tmux_respawn(self) -> None:
        task = self.prepared(61)
        adapter = FakeTmux()
        with self.assertRaisesRegex(LaunchError, "tmux command argv is invalid"):
            launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex",
                goal_args=("investigate;", "kill-server"),
            )
        self.assertFalse(adapter.respawned)

    def test_stop_is_identity_gated_and_archive_persists_reconciled_terminal_edge(self) -> None:
        task = self.prepared(70)
        adapter = FakeTmux()
        with self.process_evidence():
            running = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )["task"]
        sent = mock.Mock()
        with mock.patch(
            "lib.control.launch.harness_api.stop_signal_allowed", return_value=True,
        ):
            interrupted = stop_task(
                self.config, running, tmux=adapter, tasks=self.tasks, signaler=sent,
            )
            terminated = stop_task(
                self.config, running, tmux=adapter, tasks=self.tasks,
                terminate=True, signaler=sent,
            )
        self.assertEqual(interrupted["signal"], "INT")
        self.assertEqual(terminated["signal"], "TERM")
        self.assertEqual(sent.call_args_list[0].args[1], signal.SIGINT)
        self.assertEqual(sent.call_args_list[1].args[1], signal.SIGTERM)
        self.assertFalse(adapter.killed)

        workspace = Path(running["jj"]["workspace_path"])
        run = running["runs"][0]
        write_snapshot(
            self.config,
            task_id=running["task_id"],
            run_id=run["run_id"],
            event="prompt-submitted",
            harness=run["harness"],
            harness_session_id=run["harness_session_id"],
            exit_status=None,
            pane_id=run["pane_id"],
        )
        terminal_adapters = DerivedRunAdapters("exited")
        presentation = mock.Mock()
        archived = archive_task(
            self.config, running, tasks=self.tasks,
            adapters=terminal_adapters, journals=self.journals,
            presentation=presentation,
        )
        self.assertEqual(archived["lifecycle"], "archived")
        self.assertEqual(archived["runs"][0]["state"], "exited")
        self.assertIn("process=missing", archived["runs"][0]["evidence"])
        self.assertEqual(self.tasks.read(task["task_id"]), archived)
        self.assertIsNone(read_snapshot(self.config, run["run_id"]))
        presentation.set_server_summary.assert_called_once_with(
            "asha last-event-only: no snapshots", deadline_seconds=5,
        )
        self.assertTrue(workspace.exists())
        ended = unarchive_task(self.config, archived, tasks=self.tasks)
        self.assertEqual(ended["lifecycle"], "ended")
        archived_again = archive_task(self.config, ended, tasks=self.tasks)
        self.assertEqual(archived_again["lifecycle"], "archived")
        with self.assertRaisesRegex(LaunchError, "already archived"):
            archive_task(self.config, archived, tasks=self.tasks)

    def test_archive_refuses_live_reconciliation_without_changing_record(self) -> None:
        task = self.prepared(71)
        adapter = FakeTmux()
        with self.process_evidence():
            running = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )["task"]
        before = task_digest(running)
        with self.assertRaisesRegex(
            LaunchError, "runs have all exited.* is working",
        ):
            archive_task(
                self.config, running, tasks=self.tasks,
                adapters=DerivedRunAdapters("working"), journals=self.journals,
            )
        self.assertEqual(task_digest(self.tasks.read(task["task_id"])), before)

    def test_archive_accepts_reconciled_failed_terminal_run(self) -> None:
        task = self.prepared(72)
        adapter = FakeTmux()
        with self.process_evidence():
            running = launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
            )["task"]
        archived = archive_task(
            self.config, running, tasks=self.tasks,
            adapters=DerivedRunAdapters("failed"), journals=self.journals,
        )
        self.assertEqual(archived["runs"][0]["state"], "failed")
        self.assertEqual(archived["lifecycle"], "archived")

    def test_keyboard_interrupt_after_respawn_preserves_and_propagates(self) -> None:
        task = self.prepared(73)
        adapter = FakeTmux()

        def interrupt(boundary: str) -> None:
            if boundary == "respawned":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            launch_task(
                self.config, task, tmux=adapter, tasks=self.tasks,
                journals=self.journals, harness="codex", goal_args=("Goal",),
                failure_injector=interrupt,
            )
        stored = self.tasks.read(task["task_id"])
        journal = self.journals.read(task["task_id"])
        self.assertEqual(stored["lifecycle"], "failed")
        self.assertEqual(journal["phase"], "preserved")
        self.assertTrue(journal["launch_attempted"])
        self.assertFalse(adapter.killed)


@unittest.skipUnless(shutil.which("jj"), "jj is required")
class RecoverTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture_type = __import__(
            "tests.python.test_control_increment2",
            fromlist=["RealJjPreparationTests"],
        ).RealJjPreparationTests
        self.fixture = fixture_type(
            methodName="test_success_uses_exact_base_and_preserves_source",
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config

    def prepared(self, slug: str) -> dict:
        return prepare_task_workspace(
            self.config, self.fixture.request(slug), jj=JjAdapter(),
        )

    def test_recover_prelaunch_tmux_phases_kills_only_owned_session_and_rolls_back(self) -> None:
        for index, boundary in enumerate(("tmux-intent", "tmux-session-created")):
            with self.subTest(boundary=boundary):
                task = self.prepared(f"recover-tmux-{index}")
                adapter = FakeTmux()

                def interrupt(observed: str, expected=boundary) -> None:
                    if observed == expected:
                        raise KeyboardInterrupt

                with mock.patch(
                    "lib.control.launch._rollback_before_launch", return_value=None,
                ), self.assertRaises(KeyboardInterrupt):
                    launch_task(
                        self.config, task, tmux=adapter,
                        tasks=TaskStore(self.config),
                        journals=CreationJournalStore(self.config),
                        harness="codex", goal_args=("Goal",),
                        failure_injector=interrupt,
                    )

                result = recover_task(
                    self.config, task, tasks=TaskStore(self.config),
                    journals=CreationJournalStore(self.config),
                    tmux=adapter, jj=JjAdapter(),
                )
                self.assertEqual(result["message"], "rolled back")
                self.assertEqual(result["task"]["lifecycle"], "failed")
                self.assertEqual(result["journal"]["phase"], "rolled-back")
                self.assertEqual(
                    adapter.killed, boundary == "tmux-session-created",
                )

    def test_recover_launch_attempted_preserves_possible_live_process_without_kill(self) -> None:
        task = self.prepared("recover-launch-attempted")
        adapter = FakeTmux()

        def interrupt(observed: str) -> None:
            if observed == "respawned":
                raise KeyboardInterrupt

        with mock.patch(
            "lib.control.launch._preserve_after_launch", return_value=task,
        ), self.assertRaises(KeyboardInterrupt):
            launch_task(
                self.config, task, tmux=adapter, tasks=TaskStore(self.config),
                journals=CreationJournalStore(self.config), harness="codex",
                goal_args=("Goal",), failure_injector=interrupt,
            )

        result = recover_task(
            self.config, task, tasks=TaskStore(self.config),
            journals=CreationJournalStore(self.config), tmux=adapter,
            jj=JjAdapter(),
        )
        self.assertEqual(result["task"]["lifecycle"], "failed")
        self.assertEqual(result["journal"]["phase"], "preserved")
        self.assertIn("may still be live", result["message"])
        self.assertIn("asha task show", result["recovery_commands"] or "")
        self.assertFalse(adapter.killed)


class LiveAdapterEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        self.task = task_record(
            repository_root=str(root / "source"), workspace_path=str(workspace),
        )
        self.tmux = FakeTmux(collision=True, managed="1", owner=self.task["task_id"])
        self.tmux.session = self.task["tmux"]["session"]
        self.tmux.window = self.task["tmux"]["window"]
        self.tmux.pid = self.task["runs"][0]["pid"]

        class MatchingJj:
            def inspect_workspace(inner, path, name, require_empty=False):
                return WorkspaceIdentity(
                    name, self.task["jj"]["change_id"],
                    self.task["jj"]["working_commit_id"],
                    (self.task["jj"]["base_commit_id"],), "fixture",
                )

        self.adapters = LiveAdapters(tmux=self.tmux, jj=MatchingJj())

    def test_dead_pane_wins_over_stale_pid_and_never_derives_working(self) -> None:
        self.tmux.dead = True
        self.task["runs"][0]["state"] = "working"
        process = self.adapters.process(self.task, self.task["runs"][0])
        self.assertEqual(process.outcome, "missing")
        derived = reconcile_task(self.task, self.adapters)
        self.assertNotEqual(derived["state"], "working")
        self.assertIn(derived["state"], {"stale", "exited"})

    def test_pid_reuse_is_mismatch_and_malformed_proc_is_unavailable(self) -> None:
        run = self.task["runs"][0]
        with mock.patch("lib.control.reconcile.harness_api.verify_process", return_value=False):
            self.assertEqual(self.adapters.process(self.task, run).outcome, "mismatch")
        with mock.patch(
            "lib.control.reconcile.harness_api.verify_process",
            side_effect=HarnessError("malformed stat"),
        ):
            self.assertEqual(self.adapters.process(self.task, run).outcome, "unavailable")

    def test_unsupported_event_keeps_matched_live_process_unknown(self) -> None:
        run = self.task["runs"][0]
        with mock.patch("lib.control.reconcile.harness_api.verify_process", return_value=True):
            self.assertEqual(self.adapters.event(self.task, run).outcome, "unavailable")
            self.assertEqual(reconcile_task(self.task, self.adapters)["state"], "unknown")


class Increment3DoctorTests(unittest.TestCase):
    def test_transactions_probe_matches_empty_registry_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            config = load_config({
                "HOME": str(home),
                "ASHA_CONFIG": str(root / "missing.json"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            })
            with mock.patch(
                "subprocess.run",
                side_effect=AssertionError("transactions probe must not spawn"),
            ):
                result = run_doctor(
                    config,
                    probes={"transactions": DEFAULT_PROBES["transactions"]},
                )
        self.assertEqual(result["probes"][0]["outcome"], "match")

    def test_tmux_probe_uses_private_socket_no_config_and_never_starts_session(self) -> None:
        responses = iter([
            (0, b"tmux 3.4\n", b""),
            (0, b"display-popup (popup) [-BCE]\n", b""),
        ])

        def status(adapter, args):
            self.assertNotIn("new-session", args)
            self.assertNotIn("set-option", args)
            return next(responses)

        with mock.patch("lib.control.doctor.shutil.which", return_value="/usr/bin/tmux"), \
                mock.patch.object(TmuxAdapter, "_run_status", autospec=True, side_effect=status) as run:
            result = run_doctor(None)
        probe = next(item for item in result["probes"] if item["name"] == "tmux")
        self.assertEqual(probe["outcome"], "match")
        self.assertEqual(run.call_args_list[0].args[1], ["-V"])
        capability_adapter = run.call_args_list[1].args[0]
        self.assertRegex(capability_adapter.socket, r"^asha-doctor-probe-[0-9]+$")
        self.assertEqual(capability_adapter.config_file, "/dev/null")
        self.assertEqual(
            run.call_args_list[1].args[1], ["list-commands", "display-popup"],
        )


class Increment3CliGrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        home = root / "home"
        home.mkdir()
        self.env = {
            "HOME": str(home), "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.root = root

    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(args, env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def fake_result(self):
        config = load_config(self.env)
        workspace = config.workspace_root / "repo-key" / "do-work"
        workspace.mkdir(parents=True, exist_ok=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        task = task_record(
            slug="do-work", repository_root=str(self.root / "source"),
            workspace_path=str(workspace),
        )
        task["tmux"]["session"] = "asha-do-work-12345678"
        return {
            "task": task, "run": task["runs"][0],
            "session": task["tmux"]["session"], "pane": task["runs"][0]["pane_id"],
            "workspace": {
                "path": task["jj"]["workspace_path"],
                "name": task["jj"]["workspace_name"],
                "change_id": task["jj"]["change_id"],
            },
        }

    def fake_start_jj(self):
        source = self.root / "source"

        class FakeJj:
            def preflight(inner, requested):
                return RepositoryFacts(root=source, git_root=source)

            def working_copy_parent(inner, requested):
                return "a" * 40

            def git_head(inner, requested):
                return "a" * 40

            def import_git(inner, requested):
                return ()

        return FakeJj()

    def test_mutual_exclusion_and_missing_goal(self) -> None:
        cases = (
            (["task", "start", "--harness", "codex", "--agent", "claude", "--goal", "x"],
             "mutually exclusive"),
            (["task", "start"], "requires --goal"),
            (["task", "start", "--goal", "x", "--", "y"], "mutually exclusive"),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                status, stdout, stderr = self.invoke(args)
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn(expected, stderr)

    def test_goal_arguments_reject_trailing_tmux_separator_with_remedy(self) -> None:
        remedy = (
            "goal arguments must not end with ';' (tmux treats a trailing "
            "semicolon as a command separator); rephrase the goal or pass it "
            "as one --goal string that does not end in ';'"
        )
        for argv in (
            ["--repo", str(self.root), "--goal", "flake;"],
            ["--", "fix", "it;"],
        ):
            with self.subTest(argv=argv), self.assertRaisesRegex(
                ValueError, re.escape(remedy),
            ):
                _parse_start(argv)
        self.assertEqual(_parse_start(["--goal", "a; b"])["goal_args"], ("a; b",))

    def test_start_preflight_refuses_missing_harness_and_invalid_role_before_prepare(self) -> None:
        prepare = mock.Mock(side_effect=AssertionError("prepare must not be called"))
        with mock.patch("lib.control.cli.prepare_task_workspace", prepare), \
                mock.patch("lib.control.cli.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not installed or not on PATH"):
                _start_command([
                    "--harness", "codex", "--goal", "Do work",
                ], self.env)
        prepare.assert_not_called()

        prepare.reset_mock()
        with mock.patch("lib.control.cli.prepare_task_workspace", prepare), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"):
            with self.assertRaisesRegex(ValueError, "invalid restricted grammar"):
                _start_command([
                    "--harness", "codex", "--role", "bad role",
                    "--goal", "Do work",
                ], self.env)
        prepare.assert_not_called()

    def test_start_sigterm_handler_raises_keyboard_interrupt_and_is_restored(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(args, env):
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch(
            "lib.control.cli._start_command_inner", side_effect=terminate,
        ):
            status, stdout, stderr = self.invoke([
                "task", "start", "--goal", "Work",
            ])
        self.assertEqual((status, stdout), (130, ""))
        self.assertIn("interrupted", stderr)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_defaults_json_discipline_and_start_text_field_order(self) -> None:
        result = self.fake_result()
        captured: dict[str, Any] = {}

        def prepare(config, request, jj=None):
            captured["request"] = request
            return result["task"]

        def launch(config, task, **kwargs):
            captured["launch"] = kwargs
            return result

        patches = (
            mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"),
            mock.patch("lib.control.cli.JjAdapter", return_value=self.fake_start_jj()),
            mock.patch("lib.control.cli.prepare_task_workspace", side_effect=prepare),
            mock.patch("lib.control.cli.launch_task", side_effect=launch),
            mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/claude"),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            status, stdout, stderr = self.invoke([
                "task", "start", "--goal", "Do work", "--detach", "--json",
            ])
        self.assertEqual((status, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["contract"], "asha.control-task-start.v1")
        self.assertEqual(
            payload["attach"], "tmux attach-session -t asha-do-work-12345678",
        )
        self.assertEqual(captured["request"].requested_base, DEFAULT_BASE_REVSET)
        self.assertEqual(captured["launch"]["harness"], "claude")

        captured.clear()
        with mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"), \
                mock.patch("lib.control.cli.JjAdapter", return_value=self.fake_start_jj()), \
                mock.patch("lib.control.cli.prepare_task_workspace", side_effect=prepare), \
                mock.patch("lib.control.cli.launch_task", side_effect=launch), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/claude"):
            status, stdout, stderr = self.invoke([
                "task", "start", "--goal", "Do work", "--detach",
            ])
        self.assertEqual((status, stderr), (0, ""))
        fields = [line.split(":", 1)[0] + ":" for line in stdout.splitlines()]
        self.assertEqual(fields, [
            "Task:", "Task ID:", "Workspace:", "jj name:", "Change:", "Tmux:", "Run:",
        ])

    def test_json_start_implies_detach_and_never_opens_popup(self) -> None:
        result = self.fake_result()

        def prepare(config, request, jj=None):
            return result["task"]

        env = {**self.env, "TMUX": "/tmp/tmux/default,1,0"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"), \
                mock.patch("lib.control.cli.JjAdapter", return_value=self.fake_start_jj()), \
                mock.patch("lib.control.cli.prepare_task_workspace", side_effect=prepare), \
                mock.patch("lib.control.cli.launch_task", return_value=result), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/claude"), \
                mock.patch("lib.control.cli._run_popup") as popup, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--goal", "Do work", "--json",
            ], env=env)
        self.assertEqual((status, stderr.getvalue()), (0, ""))
        popup.assert_not_called()
        self.assertIn("attach", json.loads(stdout.getvalue()))

    def test_nonzero_popup_status_is_advisory_after_successful_start(self) -> None:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = "/dev/pts/7"
        adapter.popup_argv.return_value = ["tmux", "display-popup"]
        stderr = io.StringIO()
        with mock.patch(
            "lib.control.cli.subprocess.run",
            return_value=subprocess.CompletedProcess(["tmux"], 1),
        ), contextlib.redirect_stderr(stderr):
            _run_popup(
                adapter, load_config(self.env), "asha-task-12345678", "do-work",
                {"TMUX_PANE": "%7"},
            )
        self.assertEqual(
            stderr.getvalue(),
            "asha control: popup closed with status 1; task do-work is still "
            "running (attach: tmux attach-session -t asha-task-12345678)\n",
        )

    def test_control_tmux_prints_only_the_snippet(self) -> None:
        status, stdout, stderr = self.invoke(["control", "tmux"])
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("@asha_managed", stdout)
        self.assertFalse((Path(self.env["HOME"]) / ".tmux.conf").exists())

    def test_attach_verifies_run_ownership_selects_target_and_prints_exact_command(self) -> None:
        result = self.fake_result()
        config = load_config(self.env)
        store = TaskStore(config)
        store.save(result["task"])
        run = result["run"]

        class AttachTmux(FakeTmux):
            def __init__(inner):
                super().__init__(
                    collision=True, managed="1", owner=result["task"]["task_id"],
                )
                inner.socket = None
                inner.pane_options = {"@asha_run_id": run["run_id"]}
                inner.session = result["task"]["tmux"]["session"]
                inner.window = result["task"]["tmux"]["window"]
                inner.pid = run["pid"]
                inner.selected = None

            def select_target(inner, session, window, pane_id=None):
                inner.selected = (session, window, pane_id)

        adapter = AttachTmux()
        with mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter):
            status, stdout, stderr = self.invoke([
                "task", "attach", result["task"]["slug"], "--run", run["run_id"],
            ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            stdout.strip(),
            f"tmux attach-session -t {result['task']['tmux']['session']}",
        )
        self.assertEqual(adapter.selected[2], run["pane_id"])

    def test_archive_and_recovery_cli_refusals_exit_two(self) -> None:
        result = self.fake_result()
        store = TaskStore(load_config(self.env))
        store.save(result["task"])
        with mock.patch(
            "lib.control.cli.LiveAdapters",
            return_value=DerivedRunAdapters("working"),
        ):
            status, stdout, stderr = self.invoke([
                "task", "archive", result["task"]["slug"],
            ])
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("runs have all exited", stderr)

        for verb in ("recover", "unarchive"):
            with self.subTest(verb=verb):
                status, stdout, stderr = self.invoke([
                    "task", verb, result["task"]["slug"],
                ])
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn("nothing to recover" if verb == "recover" else "archived", stderr)

    def test_failed_rolled_back_task_can_be_archived_and_unarchived(self) -> None:
        result = self.fake_result()
        task = copy.deepcopy(result["task"])
        task["lifecycle"] = "failed"
        task["runs"] = []
        store = TaskStore(load_config(self.env))
        store.save(task)
        status, stdout, stderr = self.invoke(["task", "archive", task["slug"]])
        self.assertEqual((status, stderr), (0, ""), stderr)
        self.assertIn("Archived task", stdout)
        self.assertEqual(store.read(task["task_id"])["lifecycle"], "archived")
        status, stdout, stderr = self.invoke(["task", "unarchive", task["slug"]])
        self.assertEqual((status, stderr), (0, ""), stderr)
        self.assertEqual(store.read(task["task_id"])["lifecycle"], "failed")
        # A failed task that still records a preserved live run is refused.
        live = copy.deepcopy(result["task"])
        live["lifecycle"] = "failed"
        live["runs"][-1]["state"] = "working"
        live["task_id"] = str(uuid.uuid4())
        live["slug"] = "failed-live"
        store.save(live)
        status, stdout, stderr = self.invoke(["task", "archive", live["slug"]])
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("preserved live run", stderr)

    def test_archive_then_unarchive_cli_each_prints_one_success_line(self) -> None:
        result = self.fake_result()
        TaskStore(load_config(self.env)).save(result["task"])
        with mock.patch(
            "lib.control.cli.LiveAdapters",
            return_value=DerivedRunAdapters("exited"),
        ):
            status, stdout, stderr = self.invoke([
                "task", "archive", result["task"]["slug"],
            ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(len(stdout.splitlines()), 1)

        status, stdout, stderr = self.invoke([
            "task", "unarchive", result["task"]["slug"],
        ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(len(stdout.splitlines()), 1)


@unittest.skipUnless(shutil.which("jj"), "jj is required")
class SessionPrefixPreparationTests(unittest.TestCase):
    def test_custom_session_prefix_is_honored_without_renaming_jj_workspace(self) -> None:
        RealJjPreparationTests = __import__(
            "tests.python.test_control_increment2", fromlist=["RealJjPreparationTests"],
        ).RealJjPreparationTests
        fixture = RealJjPreparationTests(methodName="test_success_uses_exact_base_and_preserves_source")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        object.__setattr__(fixture.config, "session_prefix", "crew-")
        request = fixture.request("prefix-proof")

        prepared = prepare_task_workspace(fixture.config, request)

        self.assertEqual(
            prepared["tmux"]["session"],
            f"crew-prefix-proof-{request.task_id[:8]}",
        )
        self.assertEqual(
            prepared["jj"]["workspace_name"],
            f"asha-prefix-proof-{request.task_id[:8]}",
        )
        self.assertRegex(
            prepared["tmux"]["session"], r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        )
        validate_task(prepared)


@unittest.skipUnless(shutil.which("tmux") and shutil.which("jj"), "tmux and jj are required")
class RealTmuxLaunchTests(unittest.TestCase):
    """Disposable jj repositories and one private, no-config tmux server."""

    def setUp(self) -> None:
        self.socket = f"asha-control-test-{os.getpid()}"
        capability = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null",
             "list-commands", "display-popup"],
            capture_output=True, text=True, check=False,
        )
        if capability.returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(self.source)], check=True, env=git_env)
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / ".gitignore").write_text(
            "/.asha/\n/Memory/\n/Work/\n*.ignored\n", encoding="utf-8",
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
            ["jj", "git", "init", "--colocate", str(self.source)],
            check=True, capture_output=True, text=True,
        )
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "increment-three",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        self.source.chmod(0o755)
        self.config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        self.adapter = TmuxAdapter(
            socket=self.socket, config_file=Path("/dev/null"),
        )
        self.asha_root = self.root / "fake-asha"
        (self.asha_root / "bin").mkdir(parents=True)
        launcher = self.asha_root / "bin" / "asha"
        launcher.write_text("#!/bin/sh\nexec /bin/sleep 3600\n", encoding="utf-8")
        launcher.chmod(0o700)

    def tearDown(self) -> None:
        subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "kill-server"],
            capture_output=True, check=False,
        )

    def prepare(self, slug: str = "live-launch", *, base: str = "@-") -> dict:
        request = __import__(
            "lib.control.prepare", fromlist=["PrepareRequest"]
        ).PrepareRequest(
            repository=self.source, requested_base=base, task_id=str(uuid.uuid4()),
            slug=slug, label="Live launch",
        )
        return __import__(
            "lib.control.prepare", fromlist=["prepare_task_workspace"]
        ).prepare_task_workspace(self.config, request)

    def launch(self, task: dict, **kwargs):
        with mock.patch.dict(os.environ, {"ASHA_ROOT": str(self.asha_root)}):
            return launch_task(
                self.config, task, tmux=self.adapter, harness="codex",
                goal_args=("Live goal",), **kwargs,
            )

    def test_start_creates_explicit_base_owned_session_run_and_registry_record(self) -> None:
        prepared = self.prepare()
        result = self.launch(prepared)
        stored = TaskStore(self.config).read(prepared["task_id"])
        self.assertEqual(stored, result["task"])
        self.assertEqual(stored["lifecycle"], "running")
        self.assertEqual(len(stored["runs"]), 1)
        self.assertTrue(self.adapter.has_session(stored["tmux"]["session"]))
        self.assertEqual(
            self.adapter.session_option(stored["tmux"]["session"], "@asha_task_id"),
            stored["task_id"],
        )
        identity = __import__("lib.control.jj", fromlist=["JjAdapter"]).JjAdapter().inspect_workspace(
            Path(stored["jj"]["workspace_path"]), stored["jj"]["workspace_name"],
        )
        self.assertEqual(identity.parent_commit_ids, (stored["jj"]["base_commit_id"],))

    def test_prelaunch_failure_removes_only_verified_owned_artifacts(self) -> None:
        prepared = self.prepare("live-pre-failure")

        def inject(phase):
            if phase == "tmux-session-created":
                raise RuntimeError("injected")

        with self.assertRaises(LaunchError):
            self.launch(prepared, failure_injector=inject)
        self.assertFalse(self.adapter.has_session(prepared["tmux"]["session"]))
        self.assertFalse(Path(prepared["jj"]["workspace_path"]).exists())

    def test_recover_after_interrupted_respawn_reports_live_process_via_real_tmux(self) -> None:
        prepared = self.prepare("live-recover")

        def interrupt(phase):
            if phase == "respawned":
                raise KeyboardInterrupt

        with mock.patch(
            "lib.control.launch._preserve_after_launch", return_value=prepared,
        ), self.assertRaises(KeyboardInterrupt):
            self.launch(prepared, failure_injector=interrupt)
        stored = TaskStore(self.config).read(prepared["task_id"])
        self.assertEqual(stored["lifecycle"], "creating")
        self.assertEqual(stored["runs"], [])
        self.assertTrue(self.adapter.has_session(stored["tmux"]["session"]))

        result = recover_task(
            self.config, stored, tasks=TaskStore(self.config),
            journals=CreationJournalStore(self.config), tmux=self.adapter,
        )
        self.assertEqual(result["task"]["lifecycle"], "failed")
        self.assertEqual(result["journal"]["phase"], "preserved")
        self.assertIn("may still be live", result["message"])
        self.assertIn("attach-session", result["message"])
        # Recovery never kills: the owned session and its pane survive.
        self.assertTrue(self.adapter.has_session(stored["tmux"]["session"]))
        facts = self.adapter.window_pane_facts(
            stored["tmux"]["session"], stored["tmux"]["window"],
        )
        self.assertFalse(facts.dead)
        with self.assertRaises(TmuxError):
            self.adapter.window_pane_facts(stored["tmux"]["session"], "not-the-window")

    def test_postlaunch_failure_preserves_workspace_and_live_run_facts(self) -> None:
        prepared = self.prepare("live-post-failure")

        def inject(phase):
            if phase == "process-identified":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(LaunchError, "asha task show"):
            self.launch(prepared, failure_injector=inject)
        stored = TaskStore(self.config).read(prepared["task_id"])
        self.assertEqual(stored["lifecycle"], "failed")
        self.assertEqual(len(stored["runs"]), 1)
        self.assertTrue(Path(stored["jj"]["workspace_path"]).exists())
        self.assertTrue(self.adapter.has_session(stored["tmux"]["session"]))

    def test_popup_command_returns_while_task_process_stays_alive(self) -> None:
        prepared = self.prepare("live-popup")
        result = self.launch(prepared)
        master_fd, slave_fd = pty.openpty()
        client = None
        client_target = ""
        try:
            client = subprocess.Popen(
                ["tmux", "-L", self.socket, "-f", "/dev/null",
                 "attach-session", "-t", result["session"]],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env={**os.environ, "TERM": "xterm-256color"},
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            for _ in range(100):
                clients = subprocess.run(
                    ["tmux", "-L", self.socket, "-f", "/dev/null",
                     "list-clients", "-F", "#{client_tty}"],
                    capture_output=True, text=True, check=False,
                )
                if clients.returncode == 0 and clients.stdout.strip():
                    client_target = clients.stdout.splitlines()[0]
                    break
                __import__("time").sleep(0.02)
            self.assertTrue(client_target, "isolated tmux client did not attach")
            popup = subprocess.run(
                ["tmux", "-L", self.socket, "-f", "/dev/null", "display-popup",
                 "-t", client_target, "-E", "--", "/bin/sleep", "0.1"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(popup.returncode, 0, popup.stderr)
            facts = self.adapter.pane_facts(result["pane"])
            self.assertFalse(facts.dead)
            self.assertTrue(verify_process(
                result["run"]["pid"], result["run"]["process_start_identity"],
            ))
        finally:
            if client_target:
                subprocess.run(
                    ["tmux", "-L", self.socket, "-f", "/dev/null",
                     "detach-client", "-t", client_target],
                    capture_output=True, check=False,
                )
            if client is not None:
                try:
                    client.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    client.terminate()
                    client.wait(timeout=2)
            if slave_fd >= 0:
                os.close(slave_fd)
            os.close(master_fd)

    def test_exiting_control_cli_does_not_change_task_state(self) -> None:
        prepared = self.prepare("live-cli-exit")
        result = self.launch(prepared)
        before = task_digest(result["task"])
        stdout, stderr = io.StringIO(), io.StringIO()
        env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(control_main(["control"], env=env), 2)
        self.assertEqual(task_digest(TaskStore(self.config).read(prepared["task_id"])), before)

    def test_killed_pane_reconciles_never_as_working(self) -> None:
        prepared = self.prepare("live-killed")
        result = self.launch(prepared)
        os.kill(result["run"]["pid"], 9)
        for _ in range(100):
            if self.adapter.pane_facts(result["pane"]).dead:
                break
            __import__("time").sleep(0.02)
        derived = reconcile_task(
            result["task"], LiveAdapters(tmux=self.adapter),
        )
        # SIGKILL leaves tmux with pane_dead=1 and pane_dead_signal=9; the
        # soak fix reconciles that to the terminal `failed`, never `working`.
        self.assertEqual(derived["state"], "failed")
        self.assertIsNone(derived["blocker"])
        self.assertNotEqual(derived["state"], "working")

    def test_colliding_foreign_session_is_refused_and_left_running(self) -> None:
        prepared = self.prepare("live-collision")
        subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "new-session",
             "-d", "-s", prepared["tmux"]["session"], "--", "sleep", "3600"],
            check=True,
        )
        with self.assertRaisesRegex(LaunchError, "foreign tmux session collision"):
            self.launch(prepared)
        self.assertTrue(self.adapter.has_session(prepared["tmux"]["session"]))
        self.assertIsNone(self.adapter.session_option(
            prepared["tmux"]["session"], "@asha_managed",
        ))

    def test_second_mutating_run_is_refused(self) -> None:
        prepared = self.prepare("live-second")
        result = self.launch(prepared)
        with self.assertRaises(LaunchError):
            self.launch(result["task"])
        self.assertEqual(len(TaskStore(self.config).read(prepared["task_id"])["runs"]), 1)

    def test_unsupported_event_is_unknown_not_false_semantic_state(self) -> None:
        prepared = self.prepare("live-event")
        result = self.launch(prepared)
        derived = reconcile_task(result["task"], LiveAdapters(tmux=self.adapter))
        self.assertEqual(derived["state"], "unknown")

    def test_custom_prefix_and_hostile_values(self) -> None:
        object.__setattr__(self.config, "session_prefix", "crew-")
        prepared = self.prepare("custom-prefix")
        self.assertTrue(prepared["tmux"]["session"].startswith("crew-"))
        self.assertTrue(re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", prepared["tmux"]["session"],
        ))
        validate_task(prepared)
        before = self.adapter.has_session(prepared["tmux"]["session"])
        hostile = {
            "session": prepared["tmux"]["session"], "window": "work",
            "start_directory": prepared["jj"]["workspace_path"],
            "environment": {"ASHA_CONTROL_STATE_DIR": "bad#{pane_pid}"},
            "holder_argv": ["sleep", "3600"],
            "session_options": {"@asha_managed": "1"},
            "pane_options": {"@asha_run_id": str(uuid.uuid4())},
            "pane_title": "bad;title",
        }
        with self.assertRaises(TmuxError):
            self.adapter.create_task_session(**hostile)
        self.assertEqual(self.adapter.has_session(prepared["tmux"]["session"]), before)


if __name__ == "__main__":
    unittest.main()
