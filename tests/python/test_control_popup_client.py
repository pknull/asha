from __future__ import annotations

import contextlib
import io
import os
import pty
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.cli import _run_popup, main as control_main
from lib.control.config import load_config
from lib.control.jj import RepositoryFacts
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
                "tmux", "-L", "asha-control", "attach-session", "-t",
                "asha-control-test-12345678",
            ],
        )
        self.assertLess(argv.index("-c"), argv.index("-E"))

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
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
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
        jj.import_git.return_value = ()
        with mock.patch("lib.control.cli._repo_argument", return_value=self.root / "source"), \
                mock.patch("lib.control.cli.JjAdapter", return_value=jj), \
                mock.patch("lib.control.cli._guard_colocated_sync"), \
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


if __name__ == "__main__":
    unittest.main()
