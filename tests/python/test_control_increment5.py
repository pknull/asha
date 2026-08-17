from __future__ import annotations

import contextlib
import copy
import io
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control import view
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import DiffSummary, JjAdapter, JjError, MAX_OUTPUT_BYTES
from lib.control.launch import LaunchError
from lib.control.reconcile import UnavailableAdapters, reconcile_task
from lib.control.tmux import PaneFacts
from lib.control.tui import (
    IntentKind,
    TuiModel,
    TuiRow,
    filter_rows,
    render,
    run_tui,
    sort_rows,
)
from tests.python.test_control_config_model import task_record


def reconciliation(task: dict, state: str, blocker: str | None = None) -> dict:
    return {
        "contract": "asha.control-reconciliation.v1",
        "task_id": task["task_id"],
        "state": state,
        "blocker": blocker,
        "evidence": [],
        "runs": [
            {
                "contract": "asha.control-run-reconciliation.v1",
                "run_id": run["run_id"],
                "state": state,
                "blocker": blocker,
                "evidence": [{
                    "source": "event",
                    "outcome": "match",
                    "detail": "verified tool-completed event snapshot",
                    "state": "working",
                    "stale": False,
                }] if state == "working" else [],
            }
            for run in task["runs"]
        ],
    }


def row(
    slug: str,
    state: str,
    *,
    repository: str | None = None,
    harness: str = "codex",
    lifecycle: str = "running",
) -> TuiRow:
    task = task_record(
        slug=slug,
        repository_root=repository or f"/repositories/{slug}",
        workspace_path=f"/workspaces/{slug}",
    )
    task["label"] = f"Goal for {slug}"
    task["lifecycle"] = lifecycle
    task["runs"][0]["harness"] = harness
    if lifecycle == "ended":
        task["runs"][0]["state"] = "exited"
    return TuiRow.from_records(task, reconciliation(task, state))


class PureModelTests(unittest.TestCase):
    def test_sorting_is_deterministic_and_filter_does_not_mutate_rows(self) -> None:
        supplied = [
            row("Zulu", "working", repository="/repo/bravo"),
            row("alpha", "needs-input", repository="/repo/alpha"),
            row("bravo", "working", repository="/repo/alpha"),
        ]
        before = copy.deepcopy(supplied)

        first = sort_rows(supplied)
        second = sort_rows(reversed(supplied))
        filtered = filter_rows(first, "BRAVO")

        self.assertEqual(
            [item.task["slug"] for item in first],
            ["alpha", "bravo", "Zulu"],
        )
        self.assertEqual(
            [item.task["task_id"] for item in first],
            [item.task["task_id"] for item in second],
        )
        self.assertEqual(
            {item.task["slug"] for item in filtered}, {"bravo", "Zulu"},
        )
        self.assertEqual(supplied, before)

        model = TuiModel(first)
        original_ids = [item.task["task_id"] for item in model.rows]
        model.set_filter("needs-input")
        self.assertEqual(
            [item.task["task_id"] for item in model.rows], original_ids,
        )
        self.assertEqual(len(model.filtered_rows), 1)

    def test_selection_movement_empty_single_first_last_and_clamping(self) -> None:
        empty = TuiModel([])
        self.assertIsNone(empty.selection)
        self.assertIsNone(empty.move_selection(1))
        self.assertIsNone(empty.move_selection(-1))

        single = TuiModel([row("only", "working")])
        single.move_selection(50)
        self.assertEqual(single.selection, 0)
        single.move_selection(-50)
        self.assertEqual(single.selection, 0)

        model = TuiModel([
            row("one", "working"), row("two", "working"), row("three", "working"),
        ])
        model.move_selection(-1)
        self.assertEqual(model.selection, 0)
        model.move_selection(1)
        self.assertEqual(model.selection, 1)
        model.move_selection(500)
        self.assertEqual(model.selection, 2)
        model.move_selection(1)
        self.assertEqual(model.selection, 2)
        model.move_selection(-500)
        self.assertEqual(model.selection, 0)

    def test_all_eight_keys_dispatch_and_unknown_keys_are_inert(self) -> None:
        model = TuiModel([row("keys", "working")])
        expected = {
            "ENTER": IntentKind.OPEN,
            "n": IntentKind.START,
            "r": IntentKind.RECONCILE,
            "d": IntentKind.DIFF,
            "/": IntentKind.FILTER,
            "q": IntentKind.QUIT,
            "?": IntentKind.HELP,
        }
        for key, kind in expected.items():
            with self.subTest(key=key):
                self.assertIs(model.dispatch_key(key).kind, kind)
        refused = model.dispatch_key("a")
        self.assertIs(refused.kind, IntentKind.NONE)
        self.assertIn("all exited", refused.reason or "")
        self.assertIs(model.dispatch_key("x").kind, IntentKind.NONE)
        self.assertIs(model.dispatch_key("DELETE").kind, IntentKind.NONE)

        opened = model.dispatch_key("\n")
        self.assertEqual(opened.task_id, model.selected_row.task["task_id"])
        self.assertEqual(opened.run_id, model.selected_row.task["runs"][-1]["run_id"])

    def test_resize_recomputes_visible_rows_including_too_small_height(self) -> None:
        model = TuiModel(
            [row(f"task-{number}", "working") for number in range(20)],
            height=16,
        )
        self.assertEqual(model.visible_capacity, 4)
        self.assertEqual(len(model.visible_rows), 4)

        visible = model.resize(11, 120)
        self.assertEqual(model.visible_capacity, 0)
        self.assertEqual(visible, ())

        model.resize(14, 80)
        self.assertEqual(model.visible_capacity, 2)
        self.assertEqual(len(model.visible_rows), 2)
        self.assertEqual((model.height, model.width), (14, 80))

    def test_render_has_required_columns_and_detail_projection_fields(self) -> None:
        selected = row("render-me", "working", repository="/repo/asha")
        model = TuiModel([selected], height=30, width=140)
        model.record_diff(
            selected.task["task_id"],
            DiffSummary("M lib/control/tui.py", "2026-08-15T12:34:56Z"),
        )

        lines = render(model)
        table = next(line for line in lines if "REPOSITORY" in line)
        for column in ("STATE", "TASK", "REPOSITORY", "CHANGE", "HARNESS", "AGE"):
            self.assertIn(column, table)
        output = "\n".join(lines)
        for field in ("Run:", "Tmux:", "Evidence:", "Workspace:", "Change:", "Blocker:"):
            self.assertIn(field, output)
        self.assertIn("2026-08-15T12:34:56Z", output)
        self.assertIn("M lib/control/tui.py", output)
        self.assertLessEqual(len(lines), model.height)
        self.assertTrue(all(len(line) <= model.width for line in lines))

    def test_archive_intent_uses_derived_terminal_run_state(self) -> None:
        exited = TuiModel([row("exited", "exited")])
        intent = exited.dispatch_key("a")
        self.assertIs(intent.kind, IntentKind.ARCHIVE)
        self.assertTrue(intent.requires_confirmation)

        running = TuiModel([row("running", "working")])
        intent = running.dispatch_key("a")
        self.assertIs(intent.kind, IntentKind.NONE)
        self.assertFalse(intent.requires_confirmation)
        self.assertEqual(
            intent.reason,
            "only a task whose runs have all exited can be archived",
        )

    def test_help_render_names_keys_status_evidence_and_limitations(self) -> None:
        model = TuiModel([row("help", "working")], height=20, width=140)
        model.help_visible = True

        output = "\n".join(render(model))

        for key in ("Enter", "n start", "r reconcile", "d diff", "a archive", "/ filter", "q quit", "? help"):
            self.assertIn(key, output)
        self.assertIn("evidence", output)
        self.assertIn("Limitations", output)
        self.assertIn("no destructive removal or automated integration", output)

    def test_no_intent_represents_removal_or_automated_integration(self) -> None:
        values = {kind.value for kind in IntentKind}
        for forbidden in ("remove", "delete", "merge", "rebase", "integrate", "push"):
            self.assertNotIn(forbidden, values)
        model = TuiModel([row("safe", "working")])
        for key in ("D", "m", "i", "p", "x"):
            self.assertIs(model.dispatch_key(key).kind, IntentKind.NONE)


class PreflightDegradeTests(unittest.TestCase):
    class FakeCurses:
        class error(Exception):
            pass

        def __init__(self, setup_error: bool = False) -> None:
            self.setup_error = setup_error
            self.setup_calls = 0
            self.wrapper_calls = 0

        def setupterm(self) -> None:
            self.setup_calls += 1
            if self.setup_error:
                raise self.error("missing terminfo")

        def wrapper(self, *args, **kwargs):
            self.wrapper_calls += 1
            raise AssertionError("curses wrapper must not be initialized")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    def test_no_tty_exits_two_with_json_fallback_before_curses_initialization(self) -> None:
        curses = self.FakeCurses()
        stderr = io.StringIO()

        status = run_tui(
            {}, stdout=io.StringIO(), stderr=stderr, curses_module=curses,
        )

        self.assertEqual(status, 2)
        self.assertEqual(curses.setup_calls, 0)
        self.assertEqual(curses.wrapper_calls, 0)
        self.assertIn("asha task list --json", stderr.getvalue())
        self.assertEqual(len(stderr.getvalue().splitlines()), 1)

    def test_setupterm_failure_exits_two_without_curses_initialization(self) -> None:
        curses = self.FakeCurses(setup_error=True)
        stderr = io.StringIO()

        status = run_tui(
            {}, stdout=self.Tty(), stderr=stderr, curses_module=curses,
        )

        self.assertEqual(status, 2)
        self.assertEqual(curses.setup_calls, 1)
        self.assertEqual(curses.wrapper_calls, 0)
        self.assertIn("asha task list --json", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_signal_handlers_are_installed_before_wrapper_and_restored_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            env = {
                "HOME": str(home),
                "ASHA_CONFIG": str(root / "missing.json"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            }
            curses = self.FakeCurses()
            observed: dict[int, object] = {}

            def wrapper(*args, **kwargs):
                curses.wrapper_calls += 1
                for signum in (signal.SIGTERM, signal.SIGHUP):
                    observed[signum] = signal.getsignal(signum)
                return 0

            previous = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGTERM, signal.SIGHUP)
            }
            curses.wrapper = wrapper

            status = run_tui(
                env, stdout=self.Tty(), stderr=io.StringIO(), curses_module=curses,
            )

            self.assertEqual(status, 0)
            self.assertEqual(curses.wrapper_calls, 1)
            for signum in (signal.SIGTERM, signal.SIGHUP):
                self.assertTrue(callable(observed[signum]))
                self.assertIs(signal.getsignal(signum), previous[signum])


class DoctorTuiProbeTests(unittest.TestCase):
    class FakeCurses:
        class error(Exception):
            pass

        def __init__(self, failure: bool) -> None:
            self.failure = failure
            self.calls = 0

        def setupterm(self) -> None:
            self.calls += 1
            if self.failure:
                raise self.error("terminfo unavailable")

    def test_doctor_checks_curses_setupterm_without_initializing_a_screen(self) -> None:
        for failure, outcome in ((False, "match"), (True, "unavailable")):
            with self.subTest(failure=failure):
                curses = self.FakeCurses(failure)
                with mock.patch.dict(sys.modules, {"curses": curses}):
                    result = run_doctor(
                        None, probes={"tui": DEFAULT_PROBES["tui"]},
                    )["probes"][0]
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(curses.calls, 1)
                self.assertFalse(hasattr(curses, "initscr"))


class PopupIntegrationTests(unittest.TestCase):
    """The TUI Enter path must reach the real _run_popup with a matching
    signature.  A MagicMock would accept any arity, so this drives the actual
    cli._run_popup (only its subprocess call is stubbed): if _open_popup and
    _run_popup ever disagree on arguments again, this raises TypeError exactly
    as the live controller did."""

    class FakeCurses:
        def endwin(self) -> None:
            pass

    class FakeAdapter:
        socket = None
        executable = "tmux"

        def select_target(self, session, window, pane_id) -> None:
            self.selected = (session, window, pane_id)

        def caller_client(self, pane) -> str:
            return "/dev/pts/7"

        def popup_argv(self, *, client, session, width, height) -> list[str]:
            return ["tmux", "display-popup", "-c", client, "-E", "-t", session]

    def test_enter_reaches_real_run_popup_with_matching_signature(self) -> None:
        from types import SimpleNamespace
        from lib.control import tui as tui_module
        from lib.control import cli as cli_module

        popup_row = row("popup-task", "working")
        adapter = self.FakeAdapter()
        target = view.AttachTarget(
            session=popup_row.task["tmux"]["session"],
            window=popup_row.task["tmux"]["window"],
            pane_id=None,
        )
        config = SimpleNamespace(popup_width="80%", popup_height="80%")
        completed = SimpleNamespace(returncode=0)
        with mock.patch.object(tui_module, "_adapter_for_task", return_value=adapter), \
                mock.patch.object(tui_module.view, "attach_target", return_value=target), \
                mock.patch.object(tui_module, "_repaint_after_suspend"), \
                mock.patch.object(cli_module.subprocess, "run", return_value=completed) as run:
            # Must not raise: the real _run_popup binds (adapter, config,
            # session, slug).  Regression guard for the missing-slug crash.
            tui_module._open_popup(
                self.FakeCurses(), self.FakeCurses(), config, popup_row, None,
                {"TMUX_PANE": "%7"},
            )
        run.assert_called_once()


class SharedViewTests(unittest.TestCase):
    def test_task_summary_matches_the_pre_extraction_cli_shape_exactly(self) -> None:
        task = task_record(slug="summary")
        result = reconciliation(task, "working", "wait for operator")

        self.assertEqual(view.task_summary(task, result), {
            "task_id": task["task_id"],
            "slug": task["slug"],
            "label": task["label"],
            "lifecycle": task["lifecycle"],
            "status": "working",
            "updated_at": task["updated_at"],
            "repository": {
                "root": task["repository"]["root"],
                "identity": task["repository"]["identity"],
            },
            "run_count": len(task["runs"]),
            "blocker": "wait for operator",
        })

    def test_reconciliation_composition_and_locking_match_pre_extraction_behavior(self) -> None:
        task = task_record(slug="parity")
        expected = reconcile_task(task, UnavailableAdapters())
        events: list[str] = []

        class Store:
            @contextlib.contextmanager
            def transaction_lock(inner, task_id):
                events.append(f"lock:{task_id}")
                yield

            def read(inner, task_id):
                events.append(f"read:{task_id}")
                return copy.deepcopy(task)

        journals = mock.Mock()
        actual_task, actual = view.locked_reconciliation(
            Store(), journals, task["task_id"], UnavailableAdapters(), JjAdapter(),
        )

        self.assertEqual(actual_task, task)
        self.assertEqual(actual, expected)
        self.assertEqual(events, [f"lock:{task['task_id']}", f"read:{task['task_id']}"])
        journals.read.assert_not_called()

    def test_attach_target_checks_session_run_and_pane_ownership(self) -> None:
        task = task_record(slug="attach")
        run = task["runs"][0]

        class Adapter:
            def has_session(inner, session):
                return True

            def session_option(inner, session, option):
                return "1" if option == "@asha_managed" else task["task_id"]

            def pane_option(inner, pane_id, option):
                return run["run_id"]

            def pane_facts(inner, pane_id):
                return PaneFacts(
                    pane_id, run["pid"], False, None, None,
                    task["tmux"]["session"], task["tmux"]["window"], "fixture",
                )

        target = view.attach_target(task, run["run_id"], adapter=Adapter())
        self.assertEqual(
            (target.session, target.window, target.pane_id),
            (task["tmux"]["session"], task["tmux"]["window"], run["pane_id"]),
        )
        with self.assertRaisesRegex(LaunchError, "does not belong"):
            view.attach_target(task, "11111111-1111-4111-8111-111111111111", adapter=Adapter())


class JjDiffSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve() / "workspace"
        self.workspace.mkdir()

    def test_diff_summary_is_bounded_argv_only_and_snapshots_explicitly(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess(
            ["jj"], 0, b"M lib/control/tui.py\n", b"",
        ))

        result = JjAdapter(runner=runner).diff_summary(self.workspace)

        self.assertEqual(result.summary, "M lib/control/tui.py")
        self.assertRegex(result.refreshed_at, r"^20[0-9]{2}-.*Z$")
        self.assertEqual(
            runner.call_args.args[0],
            ["jj", "-R", str(self.workspace), "diff", "--summary"],
        )
        self.assertNotIn("--ignore-working-copy", runner.call_args.args[0])
        self.assertFalse(runner.call_args.kwargs["shell"])

        oversized = mock.Mock(return_value=subprocess.CompletedProcess(
            ["jj"], 0, b"x" * (MAX_OUTPUT_BYTES + 1), b"",
        ))
        with self.assertRaisesRegex(JjError, "bounded"):
            JjAdapter(runner=oversized).diff_summary(self.workspace)
        self.assertFalse(oversized.call_args.kwargs["shell"])

    def test_hostile_noncanonical_relative_and_symlink_paths_are_refused(self) -> None:
        runner = mock.Mock()
        alias = self.workspace.parent / "workspace-alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        hostile = (
            Path("$(touch-pwned)"),
            self.workspace / ".." / "workspace",
            alias,
            self.workspace / "missing;rm-all",
            self.workspace.parent / "hostile\nworkspace",
        )
        for path in hostile:
            with self.subTest(path=path), self.assertRaisesRegex(JjError, "canonical"):
                JjAdapter(runner=runner).diff_summary(path)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
