from __future__ import annotations

import contextlib
import fcntl
import getopt
import io
import json
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.cli import _run_popup, main as control_main
from lib.control.config import load_config
from lib.control.jj import DefaultBaseResolution, RepositoryFacts
from lib.control.prepare import PrepareRequest
from lib.control.store import TaskStore
from lib.control.tmux import TmuxAdapter, TmuxError
from lib.control import tui as tui_module
from lib.control import view
from tests.python.test_control_config_model import task_record


REFUSAL = (
    "asha control: no tmux client is attached to this session; attach with: "
    "tmux attach-session -t asha-control-test-12345678"
)


def reconciliation(task: dict) -> dict:
    return {
        "contract": "asha.control-reconciliation.v1",
        "task_id": task["task_id"],
        "state": "working",
        "blocker": None,
        "evidence": [],
        "runs": [{
            "contract": "asha.control-run-reconciliation.v1",
            "run_id": task["runs"][0]["run_id"],
            "state": "working",
            "blocker": None,
            "evidence": [],
        }],
    }


class PopupClientUnitTests(unittest.TestCase):
    def test_popup_argv_binds_client_before_execution_and_dimensions(self) -> None:
        argv = TmuxAdapter(socket="asha-control").popup_argv(
            client="/dev/pts/7",
            session="asha-control-test-12345678",
            width="90%",
            height="85%",
        )

        self.assertEqual(
            argv,
            [
                "tmux", "-L", "asha-control", "display-popup",
                "-c", "/dev/pts/7", "-E", "-w", "90%", "-h", "85%", "--",
                sys.executable, "-I", "-S", "-c",
                "(__import__('os').environ.__setitem__('TMUX',''))or"
                "(__import__('os').execvp(__import__('sys').argv[1],"
                "__import__('sys').argv[1:]))",
                "tmux", "-L", "asha-control", "attach-session", "-t",
                "asha-control-test-12345678",
            ],
        )
        self.assertLess(argv.index("-c"), argv.index("-E"))
        separator = argv.index("--")
        self.assertNotIn("-e", argv[:separator])
        self.assertNotIn("TMUX=", argv[:separator])
        self.assertIn("environ.__setitem__('TMUX','')", argv[separator + 5])
        self.assertFalse(any(token.startswith("TMUX_PANE=") for token in argv))

    def test_popup_outer_options_parse_with_tmux_3_2_display_popup_grammar(self) -> None:
        argv = TmuxAdapter().popup_argv(
            client="/dev/pts/7", session="asha-control-test-12345678",
            width="90%", height="85%",
        )
        command = argv.index("display-popup")
        separator = argv.index("--")

        options, operands = getopt.getopt(
            argv[command + 1:separator], "Cc:d:Eh:t:w:x:y:",
        )

        self.assertEqual(operands, [])
        self.assertEqual(options, [
            ("-c", "/dev/pts/7"), ("-E", ""),
            ("-w", "90%"), ("-h", "85%"),
        ])

    def test_popup_child_exec_preserves_argv_status_signal_and_parent_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "tmux-probe"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, os, signal, sys\n"
                "with open(os.environ['POPUP_PROBE'], 'w') as stream:\n"
                "    json.dump({'argv': sys.argv[1:], 'tmux': os.environ.get('TMUX'), "
                "'pane': os.environ.get('TMUX_PANE')}, stream)\n"
                "if os.environ['POPUP_MODE'] == 'signal':\n"
                "    os.kill(os.getpid(), signal.SIGTERM)\n"
                "raise SystemExit(23)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            argv = TmuxAdapter(
                executable=str(executable), socket="asha-control",
            ).popup_argv(
                client="/dev/pts/7", session="asha-control-test-12345678",
                width="90%", height="85%",
            )
            child = argv[argv.index("--") + 1:]

            for mode, expected_status in (("exit", 23), ("signal", -signal.SIGTERM)):
                probe = root / f"{mode}.json"
                parent = {
                    **os.environ,
                    "TMUX": "/tmp/tmux/default,1,0",
                    "TMUX_PANE": "%7",
                    "POPUP_MODE": mode,
                    "POPUP_PROBE": str(probe),
                }
                result = subprocess.run(child, env=parent, check=False)
                evidence = json.loads(probe.read_text())
                self.assertEqual(result.returncode, expected_status)
                self.assertEqual(evidence, {
                    "argv": [
                        "-L", "asha-control", "attach-session", "-t",
                        "asha-control-test-12345678",
                    ],
                    "tmux": "",
                    "pane": "%7",
                })
                self.assertEqual(parent["TMUX"], "/tmp/tmux/default,1,0")
                self.assertEqual(parent["TMUX_PANE"], "%7")

    def test_run_popup_returns_nonzero_failure_without_printing_it(self) -> None:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = "/dev/pts/7"
        adapter.popup_argv.return_value = ["tmux", "display-popup"]
        parent = {
            "TMUX": "/tmp/tmux/default,1,0",
            "TMUX_PANE": "%7",
        }
        stderr = io.StringIO()
        with mock.patch(
            "lib.control.cli.subprocess.run",
            return_value=subprocess.CompletedProcess(["tmux"], 1),
        ), contextlib.redirect_stderr(stderr):
            result = _run_popup(
                adapter,
                SimpleNamespace(popup_width="90%", popup_height="85%"),
                "asha-control-test-12345678", "control-test", parent,
            )

        self.assertEqual(
            result,
            "asha control: popup attach failed with status 1; task control-test "
            "is still running; attach with: tmux attach-session -t "
            "asha-control-test-12345678",
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(parent, {
            "TMUX": "/tmp/tmux/default,1,0", "TMUX_PANE": "%7",
        })

    def test_caller_client_queries_the_calling_panes_session_then_first_client(self) -> None:
        responses = iter((
            subprocess.CompletedProcess(["tmux"], 0, b"operator\n", b""),
            subprocess.CompletedProcess(
                ["tmux"], 0, b"/dev/pts/7\n/dev/pts/9\n", b"",
            ),
        ))
        runner = mock.Mock(side_effect=lambda argv, **kwargs: next(responses))
        adapter = TmuxAdapter(socket="asha-control", runner=runner)

        self.assertEqual(adapter.caller_client("%12"), "/dev/pts/7")
        self.assertEqual(
            [call.args[0] for call in runner.call_args_list],
            [
                ["tmux", "-L", "asha-control", "display-message", "-p", "-t", "%12",
                 "#{session_name}"],
                ["tmux", "-L", "asha-control", "list-clients", "-t", "operator",
                 "-F", "#{client_tty}"],
            ],
        )
        self.assertTrue(all(not call.kwargs["shell"] for call in runner.call_args_list))

    def test_caller_client_returns_none_only_for_a_session_without_clients(self) -> None:
        responses = iter((
            subprocess.CompletedProcess(["tmux"], 0, b"operator\n", b""),
            subprocess.CompletedProcess(["tmux"], 0, b"", b""),
        ))
        adapter = TmuxAdapter(
            runner=lambda argv, **kwargs: next(responses),
        )

        self.assertIsNone(adapter.caller_client("%4"))

    def test_caller_client_rejects_invalid_pane_session_and_tty_outputs(self) -> None:
        no_run = mock.Mock(side_effect=AssertionError("tmux must not run"))
        with self.assertRaisesRegex(TmuxError, "pane id"):
            TmuxAdapter(runner=no_run).caller_client("bad-pane")
        no_run.assert_not_called()

        for session, tty, diagnostic in (
            (b"bad session\n", None, "session name"),
            (b"operator\n", b"pts/7\n", "client tty"),
            (b"operator\n", b"/dev/pts/7\x1b\n", "client tty"),
            (b"operator\n", b"/dev/pts/7\r\n", "client tty"),
        ):
            with self.subTest(diagnostic=diagnostic):
                outputs = [session] + ([] if tty is None else [tty])
                adapter = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(
                    argv, 0, outputs.pop(0), b"",
                ))
                with self.assertRaisesRegex(TmuxError, diagnostic):
                    adapter.caller_client("%4")

    def test_popup_argv_rejects_non_device_and_traversing_client_paths(self) -> None:
        for client in ("/tmp/pts/7", "/dev/", "/dev/../tmp/pts/7"):
            with self.subTest(client=client), self.assertRaisesRegex(
                TmuxError, "client tty",
            ):
                TmuxAdapter().popup_argv(
                    client=client,
                    session="asha-control-test-12345678",
                    width="90%",
                    height="85%",
                )

    def test_run_popup_refuses_without_callers_attached_client(self) -> None:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = None

        with mock.patch("lib.control.cli.subprocess.run") as run:
            message = _run_popup(
                adapter,
                SimpleNamespace(popup_width="90%", popup_height="85%"),
                "asha-control-test-12345678",
                "control-test",
                {"TMUX_PANE": "%7"},
            )

        self.assertEqual(message, REFUSAL)
        adapter.caller_client.assert_called_once_with("%7")
        adapter.popup_argv.assert_not_called()
        run.assert_not_called()

    def test_run_popup_refuses_when_tmux_pane_is_missing(self) -> None:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None

        with mock.patch("lib.control.cli.subprocess.run") as run:
            message = _run_popup(
                adapter,
                SimpleNamespace(popup_width="90%", popup_height="85%"),
                "asha-control-test-12345678",
                "control-test",
                {},
            )

        self.assertEqual(message, REFUSAL)
        adapter.caller_client.assert_not_called()
        adapter.popup_argv.assert_not_called()
        run.assert_not_called()


class PopupClientCliTests(unittest.TestCase):
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
            "TMUX": "/tmp/tmux/default,1,0",
            "TMUX_PANE": "%7",
        }
        self.config = load_config(self.env)
        (self.root / "source").mkdir(mode=0o700)
        self.task = task_record(
            repository_root=str(self.root / "source"),
            workspace_path=str(
                self.config.workspace_root / "repo-123" / "control-test"
            ),
        )

    @staticmethod
    def no_client_adapter() -> mock.Mock:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = None
        return adapter

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(arguments, env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_start_refusal_prints_exact_message_and_keeps_task_running(self) -> None:
        adapter = self.no_client_adapter()
        result = {"task": self.task, "run": self.task["runs"][0]}

        # The source repository is a fixture path: stub the jj preflight, the
        # colocated-sync guard, and the ref import that a real start performs.
        jj = mock.Mock()
        jj.preflight.return_value = RepositoryFacts(
            root=self.root / "source", git_root=self.root / "source" / ".git",
        )
        jj.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/main",), self.task["jj"]["base_commit_id"],
            "attached-local",
        )
        jj.import_git.return_value = ()
        def retained_preflight(parsed, _config, _jj, source, task_id, **_kwargs):
            request = PrepareRequest(
                repository=source, requested_base=parsed["base"], task_id=task_id,
                slug=self.task["slug"], label=parsed["label"],
                source={"kind": "ad-hoc", "number": None, "url": None},
                resolved_base_commit_id=self.task["jj"]["base_commit_id"],
            )
            plan = mock.Mock()
            plan.default_base_resolution = None
            return request, plan

        with mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"), \
                mock.patch("lib.control.cli.JjAdapter", return_value=jj), \
                mock.patch("lib.control.cli._guard_colocated_sync"), \
                mock.patch("lib.control.cli._preflight_plain_git_start",
                           side_effect=retained_preflight), \
                mock.patch("lib.control.cli.revalidate_plain_git_pre_enable_plan"), \
                mock.patch("lib.control.cli.prepare_task_workspace", return_value=self.task), \
                mock.patch("lib.control.cli.launch_task", return_value=result), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.subprocess.run") as popup:
            status, _stdout, stderr = self.invoke([
                "task", "start", "--goal", "Do work",
            ])

        self.assertEqual(status, 0)
        self.assertEqual(stderr, REFUSAL + "\n")
        adapter.caller_client.assert_called_once_with("%7")
        adapter.popup_argv.assert_not_called()
        popup.assert_not_called()

    def test_start_popup_failure_prints_once_but_keeps_created_task_success(self) -> None:
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = "/dev/pts/7"
        adapter.popup_argv.return_value = ["tmux", "display-popup"]
        result = {"task": self.task, "run": self.task["runs"][0]}
        jj = mock.Mock()
        jj.preflight.return_value = RepositoryFacts(
            root=self.root / "source", git_root=self.root / "source" / ".git",
        )
        jj.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/main",), self.task["jj"]["base_commit_id"],
            "attached-local",
        )
        jj.import_git.return_value = ()

        def retained_preflight(parsed, _config, _jj, source, task_id, **_kwargs):
            request = PrepareRequest(
                repository=source, requested_base=parsed["base"], task_id=task_id,
                slug=self.task["slug"], label=parsed["label"],
                source={"kind": "ad-hoc", "number": None, "url": None},
                resolved_base_commit_id=self.task["jj"]["base_commit_id"],
            )
            plan = mock.Mock()
            plan.default_base_resolution = None
            return request, plan

        with mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"), \
                mock.patch("lib.control.cli.JjAdapter", return_value=jj), \
                mock.patch("lib.control.cli._guard_colocated_sync"), \
                mock.patch(
                    "lib.control.cli._preflight_plain_git_start",
                    side_effect=retained_preflight,
                ), mock.patch("lib.control.cli.revalidate_plain_git_pre_enable_plan"), \
                mock.patch("lib.control.cli.prepare_task_workspace", return_value=self.task), \
                mock.patch("lib.control.cli.launch_task", return_value=result), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch(
                    "lib.control.cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tmux"], 1),
                ):
            status, _stdout, stderr = self.invoke([
                "task", "start", "--goal", "Do work",
            ])

        expected = (
            "asha control: popup attach failed with status 1; task control-test "
            "is still running; attach with: tmux attach-session -t "
            "asha-control-test-12345678"
        )
        self.assertEqual(status, 0)
        self.assertEqual(stderr, expected + "\n")
        self.assertEqual(stderr.count(expected), 1)

    def test_attach_refusal_prints_same_exact_message_and_exits_two(self) -> None:
        TaskStore(self.config).save(self.task)
        adapter = self.no_client_adapter()
        target = view.AttachTarget(
            self.task["tmux"]["session"], self.task["tmux"]["window"], None,
        )

        with mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.view.attach_target", return_value=target), \
                mock.patch("lib.control.cli.subprocess.run") as popup:
            status, stdout, stderr = self.invoke([
                "task", "attach", self.task["slug"],
            ])

        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(stderr, REFUSAL + "\n")
        adapter.select_target.assert_called_once_with(
            self.task["tmux"]["session"], self.task["tmux"]["window"], None,
        )
        adapter.caller_client.assert_called_once_with("%7")
        adapter.popup_argv.assert_not_called()
        popup.assert_not_called()

    def test_attach_popup_failure_prints_once_and_exits_two(self) -> None:
        TaskStore(self.config).save(self.task)
        adapter = mock.Mock()
        adapter.executable = "tmux"
        adapter.socket = None
        adapter.caller_client.return_value = "/dev/pts/7"
        adapter.popup_argv.return_value = ["tmux", "display-popup"]
        target = view.AttachTarget(
            self.task["tmux"]["session"], self.task["tmux"]["window"], None,
        )

        with mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.view.attach_target", return_value=target), \
                mock.patch(
                    "lib.control.cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tmux"], 1),
                ):
            status, stdout, stderr = self.invoke([
                "task", "attach", self.task["slug"],
            ])

        expected = (
            "asha control: popup attach failed with status 1; task control-test "
            "is still running; attach with: tmux attach-session -t "
            "asha-control-test-12345678"
        )
        self.assertEqual((status, stdout, stderr), (2, "", expected + "\n"))
        self.assertEqual(stderr.count(expected), 1)


class TuiPopupClientTests(unittest.TestCase):
    class FakeScreen:
        def clearok(self, _value) -> None:
            pass

        def touchwin(self) -> None:
            pass

        def refresh(self) -> None:
            pass

    class FakeCurses:
        def endwin(self) -> None:
            pass

    class FakeAdapter:
        socket = None
        executable = "tmux"

        def __init__(self) -> None:
            self.selected = None
            self.popup_called = False

        def select_target(self, session, window, pane_id) -> None:
            self.selected = (session, window, pane_id)

        def caller_client(self, pane) -> None:
            self.caller_pane = pane
            return None

        def popup_argv(self, **kwargs) -> list[str]:
            self.popup_called = True
            return ["tmux", "display-popup"]

    def test_open_intent_surfaces_no_client_refusal_in_status_line(self) -> None:
        task = task_record()
        row = tui_module.TuiRow.from_records(task, reconciliation(task))
        model = tui_module.TuiModel([row])
        intent = model.dispatch_key("ENTER")
        adapter = self.FakeAdapter()
        target = view.AttachTarget(
            task["tmux"]["session"], task["tmux"]["window"],
            task["runs"][0]["pane_id"],
        )

        reconciled = tui_module.TuiRow.from_records(task, reconciliation(task))
        with mock.patch.object(tui_module, "_adapter_for_task", return_value=adapter), \
                mock.patch.object(tui_module.view, "attach_target", return_value=target), \
                mock.patch.object(tui_module, "_read_row", return_value=reconciled) as read_row, \
                mock.patch("lib.control.cli.subprocess.run") as run:
            keep_running = tui_module._execute_intent(
                intent,
                stdscr=self.FakeScreen(),
                curses_module=self.FakeCurses(),
                model=model,
                config=SimpleNamespace(popup_width="90%", popup_height="85%"),
                env={"TMUX_PANE": "%7"},
                store=mock.Mock(),
                journals=mock.Mock(),
                jj=mock.Mock(),
            )

        self.assertTrue(keep_running)
        self.assertEqual(model.message, REFUSAL + "; live evidence reconciled")
        self.assertEqual(adapter.caller_pane, "%7")
        self.assertFalse(adapter.popup_called)
        run.assert_not_called()
        read_row.assert_called_once_with(
            mock.ANY, mock.ANY, mock.ANY, row.task, mock.ANY,
        )

    def test_popup_close_immediately_reconciles_the_displayed_row(self) -> None:
        task = task_record()
        row = tui_module.TuiRow.from_records(task, reconciliation(task))
        model = tui_module.TuiModel([row])
        refreshed_task = dict(task)
        refreshed = tui_module.TuiRow.from_records(
            refreshed_task, reconciliation(refreshed_task),
        )

        with mock.patch.object(tui_module, "_open_popup", return_value=None), \
                mock.patch.object(tui_module, "_read_row", return_value=refreshed) as read_row:
            keep_running = tui_module._execute_intent(
                model.dispatch_key("ENTER"),
                stdscr=self.FakeScreen(), curses_module=self.FakeCurses(),
                model=model, config=SimpleNamespace(), env={}, store=mock.Mock(),
                journals=mock.Mock(), jj=mock.Mock(),
            )

        self.assertTrue(keep_running)
        read_row.assert_called_once()
        self.assertEqual(model.rows[0], refreshed)
        self.assertIn("live evidence reconciled", model.message)

    def test_popup_failure_remains_truthful_after_direct_open_reconciliation(self) -> None:
        task = task_record()
        row = tui_module.TuiRow.from_records(task, reconciliation(task))
        model = tui_module.TuiModel([row])
        adapter = self.FakeAdapter()
        adapter.caller_client = mock.Mock(return_value="/dev/pts/7")
        target = view.AttachTarget(
            task["tmux"]["session"], task["tmux"]["window"], None,
        )
        with mock.patch.object(tui_module, "_adapter_for_task", return_value=adapter), \
                mock.patch.object(tui_module.view, "attach_target", return_value=target), \
                mock.patch.object(tui_module, "_read_row", return_value=row), \
                mock.patch(
                    "lib.control.cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tmux"], 1),
                ):
            keep_running = tui_module._execute_intent(
                model.dispatch_key("ENTER"), stdscr=self.FakeScreen(),
                curses_module=self.FakeCurses(), model=model,
                config=SimpleNamespace(popup_width="90%", popup_height="85%"),
                env={"TMUX_PANE": "%7"}, store=mock.Mock(),
                journals=mock.Mock(), jj=mock.Mock(),
            )

        self.assertTrue(keep_running)
        self.assertIn("popup attach failed with status 1", model.message)
        self.assertIn("tmux attach-session -t", model.message)
        self.assertNotIn("popup closed", model.message)

    def test_popup_failure_remains_truthful_after_actions_inspect(self) -> None:
        task = task_record()
        row = tui_module.TuiRow.from_records(task, reconciliation(task))
        model = tui_module.TuiModel([row])
        adapter = self.FakeAdapter()
        adapter.caller_client = mock.Mock(return_value="/dev/pts/7")
        target = view.AttachTarget(
            task["tmux"]["session"], task["tmux"]["window"], None,
        )
        store = mock.Mock()
        store.read.return_value = task
        with mock.patch.object(tui_module, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui_module, "_prompt_line", return_value="i"), \
                mock.patch.object(tui_module, "_adapter_for_task", return_value=adapter), \
                mock.patch.object(tui_module.view, "attach_target", return_value=target), \
                mock.patch.object(tui_module, "_read_row", return_value=row), \
                mock.patch(
                    "lib.control.cli.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tmux"], 1),
                ):
            result = tui_module._context_actions(
                stdscr=self.FakeScreen(), curses_module=self.FakeCurses(),
                model=model,
                config=SimpleNamespace(popup_width="90%", popup_height="85%"),
                env={"TMUX_PANE": "%7"}, store=store,
                journals=mock.Mock(), jj=mock.Mock(), task_id=task["task_id"],
            )

        self.assertIn("popup attach failed with status 1", result)
        self.assertIn("tmux attach-session -t", result)
        self.assertNotIn("popup closed", result)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class RealTmuxPopupClientTests(unittest.TestCase):
    _UNAVAILABLE_MARKERS = (
        "operation not permitted",
        "permission denied",
        "read-only file system",
        "not supported",
    )

    @staticmethod
    def _completed_diagnostic(result: subprocess.CompletedProcess) -> str:
        return "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        ) or "no diagnostic"

    @classmethod
    def _environment_unavailable(cls, diagnostic: str) -> bool:
        lowered = diagnostic.casefold()
        return any(marker in lowered for marker in cls._UNAVAILABLE_MARKERS)

    @staticmethod
    def _popup_capability_unavailable(diagnostic: str) -> bool:
        lowered = diagnostic.casefold()
        return "display-popup" in lowered and any(marker in lowered for marker in (
            "unknown command", "command not found", "no such command",
        ))

    @staticmethod
    def _control_mode_unavailable(diagnostic: str) -> bool:
        lowered = diagnostic.casefold()
        return any(marker in lowered for marker in (
            "unknown option -- c", "illegal option -- c",
            "open terminal failed", "not a terminal",
        ))

    def setUp(self) -> None:
        self.socket = f"asha-popup-client-{os.getpid()}"
        self.prefix = ["tmux", "-L", self.socket, "-f", "/dev/null"]
        capability = subprocess.run(
            [*self.prefix, "list-commands", "display-popup"],
            capture_output=True,
            text=True,
            check=False,
        )
        if capability.returncode != 0:
            diagnostic = self._completed_diagnostic(capability)
            if (self._environment_unavailable(diagnostic) or
                    self._popup_capability_unavailable(diagnostic)):
                self.skipTest(f"isolated tmux popup capability unavailable: {diagnostic}")
            self.fail(
                f"tmux popup capability probe failed ({capability.returncode}): "
                f"{diagnostic}"
            )
        created = subprocess.run(
            [*self.prefix, "new-session", "-d", "-P", "-F", "#{pane_id}",
             "-s", "caller", "--", "/bin/sleep", "30"],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            diagnostic = self._completed_diagnostic(created)
            if self._environment_unavailable(diagnostic):
                self.skipTest(f"isolated tmux session unavailable: {diagnostic}")
            self.fail(
                f"isolated tmux session creation failed ({created.returncode}): "
                f"{diagnostic}"
            )
        self.pane = created.stdout.strip()
        self.adapter = TmuxAdapter(socket=self.socket, config_file="/dev/null")
        self.addCleanup(subprocess.run, [*self.prefix, "kill-server"],
                        capture_output=True, check=False)

    def test_caller_client_distinguishes_detached_from_real_control_mode_client(self) -> None:
        self.assertIsNone(self.adapter.caller_client(self.pane))

        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        client_tty = os.ttyname(slave_fd)
        client = None
        try:
            client = subprocess.Popen(
                [*self.prefix, "-C", "attach-session", "-t", "caller"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env={**os.environ, "TERM": "xterm-256color"},
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            observed = None
            for _ in range(100):
                if client.poll() is not None:
                    break
                observed = self.adapter.caller_client(self.pane)
                if observed is not None:
                    break
                time.sleep(0.02)
            try:
                raw_diagnostic = os.read(master_fd, 16384)
            except BlockingIOError:
                raw_diagnostic = b""
            diagnostic = raw_diagnostic.decode("utf-8", errors="replace").strip()
            if observed is None:
                returncode = client.poll()
                detail = diagnostic or "no diagnostic"
                if (returncode is not None and
                        (self._environment_unavailable(detail) or
                         self._control_mode_unavailable(detail))):
                    self.skipTest(
                        f"tmux control-mode attachment unavailable: {detail}"
                    )
                self.fail(
                    "tmux control-mode client did not attach "
                    f"(returncode={returncode}): {detail}"
                )
            self.assertEqual(observed, client_tty)
        finally:
            subprocess.run(
                [*self.prefix, "detach-client", "-t", client_tty],
                capture_output=True,
                check=False,
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


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class RealTmuxPopupAttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    @staticmethod
    def _wait_for(predicate, *, seconds: float = 6.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.03)
        return None

    def _real_popup_case(self, socket: str | None) -> None:
        label = "default" if socket is None else "named"
        socket_root = self.root / label
        socket_root.mkdir(mode=0o700)
        result_path = socket_root / "popup-result.json"
        clean_env = dict(os.environ)
        clean_env["TMUX_TMPDIR"] = str(socket_root)
        clean_env.pop("TMUX", None)
        clean_env.pop("TMUX_PANE", None)
        socket_args = [] if socket is None else ["-L", socket]
        prefix = ["tmux", *socket_args, "-f", "/dev/null"]

        caller = subprocess.run(
            [*prefix, "new-session", "-d", "-P", "-F", "#{pane_id}",
             "-s", "caller", "--", "/bin/sleep", "60"],
            capture_output=True, text=True, check=False, env=clean_env,
        )
        if caller.returncode != 0:
            self.fail(f"isolated caller creation failed: {caller.stderr.strip()}")
        master_fd = -1
        slave_fd = -1
        caller_client = None
        try:
            caller_pane = caller.stdout.strip()
            if not caller_pane.startswith("%") or not caller_pane[1:].isdigit():
                self.fail(f"isolated caller returned invalid pane: {caller.stdout!r}")
            target = subprocess.run(
                [*prefix, "new-session", "-d", "-P", "-F",
                 "#{pane_id}\t#{pane_pid}", "-s", "target", "--",
                 "/bin/sleep", "60"],
                capture_output=True, text=True, check=False, env=clean_env,
            )
            if target.returncode != 0:
                self.fail(f"isolated target creation failed: {target.stderr.strip()}")
            target_fields = target.stdout.strip().split("\t")
            if (
                len(target_fields) != 2
                or not target_fields[0].startswith("%")
                or not target_fields[0][1:].isdigit()
                or not target_fields[1].isdigit()
            ):
                self.fail(f"isolated target returned invalid facts: {target.stdout!r}")
            target_pane, target_pid = target_fields

            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(
                slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0),
            )
            caller_client = subprocess.Popen(
                [*prefix, "attach-session", "-t", "caller"],
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env={**clean_env, "TERM": "xterm-256color"},
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            def caller_tty():
                observed = subprocess.run(
                    [*prefix, "list-clients", "-t", "caller", "-F", "#{client_tty}"],
                    capture_output=True, text=True, check=False, env=clean_env,
                )
                return observed.stdout.strip() if observed.returncode == 0 else None

            attached_caller = self._wait_for(caller_tty)
            if attached_caller is None:
                self.fail(f"isolated caller client did not attach: rc={caller_client.poll()}")

            repository = Path(__file__).resolve().parents[2]
            config_file = "/dev/null"
            script = (
                "import json,os,sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[1]); "
                "from lib.control.cli import _run_popup; "
                "from lib.control.tmux import TmuxAdapter; "
                "from types import SimpleNamespace; "
                "adapter=TmuxAdapter(socket=(sys.argv[2] or None),config_file=sys.argv[3]); "
                "result=_run_popup(adapter,SimpleNamespace(popup_width='90%',"
                "popup_height='85%'),'target','popup-test',os.environ); "
                "Path(sys.argv[4]).write_text(json.dumps({'result':result,"
                "'tmux':os.environ.get('TMUX'),'pane':os.environ.get('TMUX_PANE')}))"
            )
            respawn = subprocess.run(
                [*prefix, "respawn-pane", "-k", "-t", caller_pane, "--",
                 sys.executable, "-B", "-c", script, str(repository),
                 socket or "", config_file, str(result_path)],
                capture_output=True, text=True, check=False, env=clean_env,
            )
            self.assertEqual(respawn.returncode, 0, respawn.stderr)

            def target_client():
                observed = subprocess.run(
                    [*prefix, "list-clients", "-t", "target", "-F",
                     "#{client_tty}\t#{client_pid}"],
                    capture_output=True, text=True, check=False, env=clean_env,
                )
                if observed.returncode != 0 or not observed.stdout.strip():
                    return None
                return observed.stdout.strip().split("\t")

            attached_target = self._wait_for(target_client)
            if attached_target is None:
                premature = result_path.read_text() if result_path.exists() else "no result"
                self.fail(f"popup attach did not stay open: {premature}")
            target_tty, popup_pid = attached_target
            self.assertFalse(result_path.exists(), "_run_popup returned before detach")
            child_environment = {
                item.split(b"=", 1)[0]: item.split(b"=", 1)[1]
                for item in Path(f"/proc/{popup_pid}/environ").read_bytes().split(b"\0")
                if b"=" in item
            }
            self.assertEqual(child_environment.get(b"TMUX"), b"")

            detached = subprocess.run(
                [*prefix, "detach-client", "-t", target_tty],
                capture_output=True, text=True, check=False, env=clean_env,
            )
            self.assertEqual(detached.returncode, 0, detached.stderr)
            self.assertTrue(self._wait_for(result_path.exists))
            payload = json.loads(result_path.read_text())
            self.assertIsNone(payload["result"])
            self.assertTrue(payload["tmux"])
            self.assertEqual(payload["pane"], caller_pane)

            target_after = subprocess.run(
                [*prefix, "display-message", "-p", "-t", target_pane,
                 "#{pane_id}\t#{pane_pid}\t#{pane_dead}"],
                capture_output=True, text=True, check=False, env=clean_env,
            )
            self.assertEqual(
                target_after.stdout.strip(), f"{target_pane}\t{target_pid}\t0",
            )
        finally:
            subprocess.run(
                [*prefix, "kill-server"], capture_output=True, check=False,
                env=clean_env,
            )
            if caller_client is not None:
                try:
                    caller_client.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    caller_client.terminate()
                    caller_client.wait(timeout=2)
            if slave_fd >= 0:
                os.close(slave_fd)
            if master_fd >= 0:
                os.close(master_fd)

    def test_production_popup_attach_stays_open_for_default_and_named_sockets(self) -> None:
        for socket in (None, f"asha-popup-attach-{os.getpid()}"):
            with self.subTest(socket=socket or "default"):
                self._real_popup_case(socket)

    def test_malformed_successful_setup_output_still_kills_isolated_server(self) -> None:
        calls: list[list[str]] = []

        def run(argv, **_kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 0, "not-a-pane\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("tests.python.test_control_popup_client.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(AssertionError, "invalid pane"):
                self._real_popup_case(f"asha-popup-cleanup-{os.getpid()}")

        self.assertIn("kill-server", calls[-1])


if __name__ == "__main__":
    unittest.main()
