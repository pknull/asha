from __future__ import annotations

import copy
import gc
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from lib.control import tui
from lib.control.config import load_config
from lib.control.harness import HarnessError, launch_argv
from lib.control.model import ModelError, validate_task
from lib.control.process import bounded_process
from lib.control.reconcile import Evidence, LiveAdapters, reconcile_task
from lib.control.socket_reaper import TmuxSocketReaper
from lib.control.store import TaskStore
from lib.control.text import prompt_character_allowed, terminal_text_is_complete
from lib.control.tmux import PaneFacts, TmuxError, _validate_argv
from lib.control.transaction import CreationJournalStore
from tests.python.test_control_config_model import task_record
from tests.python.test_control_finish_increment import _Curses, _Screen


class FinishReviewRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        self.env = {
            "HOME": str(home), "ASHA_CONFIG": str(root / "missing.json"),
            "ASHA_HOME": str(root / "asha"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.config = load_config(self.env)
        self.tasks = TaskStore(self.config)
        self.journals = CreationJournalStore(self.config)

    def test_task_label_accepts_only_the_same_complete_clusters_as_the_editor(self):
        supported = "界 e\u0301 🧑🏽\u200d💻 1️⃣ 🇺🇸"
        task = task_record(slug="unicode-label")
        task["label"] = supported
        self.assertEqual(validate_task(task)["label"], supported)
        for rejected in ("dangling\u200d", "a\u200db", "🏽", "🧑🏽🏽", "\ufe0f"):
            with self.subTest(rejected=rejected):
                invalid = copy.deepcopy(task)
                invalid["label"] = rejected
                with self.assertRaisesRegex(ModelError, "task label"):
                    validate_task(invalid)

    def test_nonprintable_visible_codepoints_are_rejected_at_every_text_boundary(self):
        launcher_root = Path(__file__).resolve().parents[2]
        for character in ("\u2028", "\u2029", "\ufdd0", "\ufffe"):
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                logical = f"a{character}b"
                self.assertFalse(prompt_character_allowed("a", character))
                self.assertFalse(terminal_text_is_complete(logical))

                invalid = task_record(slug="invalid-terminal-text")
                invalid["label"] = logical
                with self.assertRaisesRegex(ModelError, "task label"):
                    validate_task(invalid)
                with self.assertRaisesRegex(HarnessError, "extra arguments"):
                    launch_argv(launcher_root, "codex", (logical,))
                with self.assertRaisesRegex(TmuxError, "command argv"):
                    _validate_argv(["asha", logical])

                screen = _Screen(["a", character, "b", "\n"], height=8, width=40)
                self.assertEqual(tui._prompt_line(
                    screen, _Curses(), tui.TuiModel([]), "Goal: ",
                    maximum=100,
                ), "ab")
                rendered = "".join(
                    value[:limit] for _y, _x, value, limit in screen.drawn
                )
                self.assertNotIn(logical, rendered)

    def test_supported_terminal_clusters_pass_editor_model_harness_and_tmux_exactly(self):
        logical = "界 e\u0301 🧑🏽\u200d💻 1️⃣ 🇺🇸"
        prefix = ""
        for character in logical:
            with self.subTest(prefix=prefix, character=character):
                self.assertTrue(prompt_character_allowed(prefix, character))
                prefix += character
        self.assertTrue(terminal_text_is_complete(logical))

        task = task_record(slug="valid-terminal-text")
        task["label"] = logical
        self.assertEqual(validate_task(task)["label"], logical)
        launcher_root = Path(__file__).resolve().parents[2]
        self.assertEqual(
            launch_argv(launcher_root, "codex", (logical,))[-1], logical,
        )
        self.assertEqual(_validate_argv(["asha", logical])[-1], logical)
        self.assertEqual(tui._prompt_line(
            _Screen(["\n"], height=8, width=20), _Curses(),
            tui.TuiModel([]), "Goal: ", initial=logical, maximum=100,
        ), logical)

    def test_candidate_snapshot_enforces_one_aggregate_identity_and_display_budget(self):
        records = []
        for index in range(300):
            record = task_record(
                slug=f"repo-{index}",
                repository_root=str(self.config.home / f"repository-{index}-界"),
                workspace_path=str(self.config.workspace_root / "x" / f"repo-{index}"),
            )
            record["updated_at"] = f"2026-08-20T00:{index // 60:02d}:{index % 60:02d}Z"
            record["jj"]["requested_base"] = f"base-{index}-界"
            record["runs"][0]["role"] = f"role-{index}"
            records.append(record)
        store = mock.Mock()
        store.list.return_value = records

        snapshot = tui.freeze_start_candidates(
            self.config, store, cwd=self.config.home / "cwd",
            executable=lambda _name: None,
        )

        flattened = [*snapshot.repositories, *snapshot.harnesses]
        flattened.extend(tui.ModalCandidate(role) for role in snapshot.roles)
        for candidates in snapshot.bases.values():
            flattened.extend(candidates)
        self.assertLessEqual(len(flattened), 128)
        self.assertLessEqual(sum(
            len(item.value.encode("utf-8")) +
            len(item.display_value.encode("utf-8")) +
            len(item.detail.encode("utf-8"))
            for item in flattened
        ), 256 * 1024)
        store.list.assert_called_once_with()

    def test_retry_rereads_terminal_and_ownership_after_confirmation_before_uuid(self):
        terminal = task_record(slug="old")
        terminal["lifecycle"] = "ended"
        terminal["runs"][0]["state"] = "exited"
        active = copy.deepcopy(terminal)
        active["lifecycle"] = "running"
        active["runs"][0]["state"] = "working"
        model = tui.TuiModel([tui.lifecycle_row({**terminal, "lifecycle": "archived"})])
        store = mock.Mock()
        store.read.side_effect = [terminal, active]
        with mock.patch.object(tui, "_prompt_line", return_value="yes"), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "new_uuid") as allocate, \
                mock.patch.object(tui, "_supervise_start_process") as supervise:
            with self.assertRaisesRegex(ValueError, "terminal"):
                tui._retry_task(
                    stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                    config=self.config, env=self.env, store=store,
                    journals=self.journals, jj=mock.Mock(),
                    task_id=terminal["task_id"],
                )
        allocate.assert_not_called()
        supervise.assert_not_called()

    def test_owned_stop_reports_action_attempt_refusal_and_os_as_distinct_facts(self):
        task = task_record(slug="owned-refused")
        task["lifecycle"] = "running"
        task["runs"][0]["state"] = "working"
        row = tui.TuiRow.from_records(
            task, {"contract": "asha.control-reconciliation.v1",
                   "task_id": task["task_id"], "state": "working",
                   "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("working", task["runs"][0]["run_id"],
                                 "test", None, "fresh", "test"),
        )
        initiative = {"initiative_id": "22222222-2222-4222-8222-222222222222",
                      "slug": "initiative", "state_revision": 1,
                      "active_plan": {"digest": "a" * 64}}
        link = {"node_id": "node", "attempt_id": "33333333-3333-4333-8333-333333333333"}
        attempt = {"attempt_id": link["attempt_id"], "state": "running"}
        initiative_store = mock.Mock()
        initiative_store.read_attempt.return_value = {
            "attempt_id": link["attempt_id"], "state": "running",
            "refusal": "attempt is protected",
        }
        binding = (initiative_store, initiative, link, attempt, {}, task)
        action = {"action_id": "44444444-4444-4444-8444-444444444444",
                  "state": "refused", "outcome": '{"status":"refused","reason":"policy"}'}
        with mock.patch.object(tui, "_prompt_line", return_value="yes"), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch.object(tui, "_fresh_active_row", return_value=row), \
                mock.patch("lib.control.orchestration.actions.submit_action", return_value=action):
            message = tui._stop_owned_attempt(
                initial_binding=binding, stdscr=mock.Mock(), curses_module=mock.Mock(),
                model=tui.TuiModel([row]), config=self.config, env=self.env,
                store=self.tasks, journals=self.journals, jj=mock.Mock(),
                task_id=task["task_id"],
            )
        self.assertIn("action refused", message)
        self.assertIn("attempt running", message)
        self.assertIn("refusal: attempt is protected", message)
        self.assertIn("OS state observed: working", message)

    def test_indeterminate_owned_stop_names_action_attempt_os_and_explicit_reconcile(self):
        task = task_record(slug="owned-indeterminate")
        task["runs"][0]["state"] = "working"
        row = tui.TuiRow.from_records(
            task, {"contract": "asha.control-reconciliation.v1",
                   "task_id": task["task_id"], "state": "working",
                   "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("working", task["runs"][0]["run_id"],
                                 "test", None, "fresh", "test"),
        )
        initiative = {"initiative_id": "22222222-2222-4222-8222-222222222222",
                      "slug": "initiative", "state_revision": 1,
                      "active_plan": {"digest": "a" * 64}}
        link = {"node_id": "node", "attempt_id": "33333333-3333-4333-8333-333333333333"}
        attempt = {"attempt_id": link["attempt_id"], "state": "running"}
        initiative_store = mock.Mock()
        initiative_store.read_attempt.return_value = {
            "attempt_id": link["attempt_id"], "state": "running", "refusal": None,
        }
        binding = (initiative_store, initiative, link, attempt, {}, task)
        action_id = "44444444-4444-4444-8444-444444444444"
        action = {"action_id": action_id, "state": "indeterminate",
                  "outcome": '{"reason":"receipt unavailable"}'}
        with mock.patch.object(tui, "_prompt_line", return_value="yes"), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch.object(tui, "_fresh_active_row", return_value=row), \
                mock.patch("lib.control.orchestration.actions.submit_action", return_value=action):
            message = tui._stop_owned_attempt(
                initial_binding=binding, stdscr=mock.Mock(), curses_module=mock.Mock(),
                model=tui.TuiModel([row]), config=self.config, env=self.env,
                store=self.tasks, journals=self.journals, jj=mock.Mock(),
                task_id=task["task_id"],
            )
        self.assertIn(f"stop action {action_id} action indeterminate", message)
        self.assertIn("attempt running", message)
        self.assertIn("refusal: receipt unavailable", message)
        self.assertIn("explicitly reconcile initiative", message)
        self.assertIn("OS state observed: working", message)

    def test_dead_exact_owned_pane_overrides_older_idle_event(self):
        workspace = self.config.workspace_root / "repo" / "dead-owned"
        workspace.mkdir(parents=True)
        task = task_record(slug="dead-owned", workspace_path=str(workspace))
        task["runs"][0]["state"] = "idle"
        run = task["runs"][0]
        tmux_adapter = mock.Mock()
        tmux_adapter.has_session.return_value = True
        tmux_adapter.session_option.side_effect = ["1", task["task_id"]]
        tmux_adapter.pane_facts.return_value = PaneFacts(
            run["pane_id"], run["pid"], True, None, None,
            task["tmux"]["session"], task["tmux"]["window"], "owned",
        )
        adapters = LiveAdapters(config=self.config, tmux=tmux_adapter, jj=mock.Mock())
        adapters.jj_adapter.inspect_workspace.return_value = mock.Mock(
            change_id=task["jj"]["change_id"],
        )
        with mock.patch("lib.control.reconcile.read_snapshot", return_value={
            "task_id": task["task_id"], "state": "idle", "event": "turn-stopped",
            "observed_at": "2026-08-20T12:00:00Z",
        }):
            result = reconcile_task(task, adapters)
        self.assertEqual(result["state"], "exited")
        self.assertIsNone(result["blocker"])

    def test_long_confirmation_wraps_every_safety_fact_around_a_short_input(self):
        slug = "s" * 64
        task_id = "11111111-1111-4111-8111-111111111111"
        run_id = "22222222-2222-4222-8222-222222222222"
        context = (
            f"Task: {slug} ({task_id})\n"
            f"Run: {run_id}\n"
            "Signal: SIGTERM\n"
            "Preservation: does not archive the task and does not remove its "
            "workspace or change.\n"
            "Authorization: type exact yes (lowercase)."
        )
        for width in (40, 80, 120):
            with self.subTest(width=width):
                screen = _Screen(["y", "e", "s", "\n"], height=24, width=width)
                answer = tui._prompt_line(
                    screen, _Curses(), tui.TuiModel([]), "Confirm [yes/N]: ",
                    title="Finish task", context=context, maximum=4,
                )
                rendered = "\n".join(value for _y, _x, value, _n in screen.drawn)
                compact = rendered.replace("\n", "")
                self.assertEqual(answer, "yes")
                for required in (
                    "Finish task", f"Task: {slug}", task_id, "Run:", run_id,
                    "Signal: SIGTERM", "does not archive", "workspace or change",
                    "type exact yes", "Confirm [yes/N]:",
                ):
                    self.assertIn(required, compact)
                self.assertTrue(all(
                    len(value[:limit]) <= width - 1
                    for _y, _x, value, limit in screen.drawn
                ))

    def test_tiny_confirmation_reserves_input_and_marks_omitted_context(self):
        for height in (1, 2, 3, 4):
            with self.subTest(height=height):
                frame = tui.modal_frame(
                    title="Finish task", context=("critical context " * 20),
                    label="", hint="", value="y", prompt="Confirm [yes/N]: ",
                    height=height, width=20,
                )
                self.assertEqual(frame.cursor[0], 0)
                self.assertIn("y", frame.rows[0])
                self.assertTrue(any("…" in row for row in frame.rows))
                self.assertTrue(all(len(row) <= 19 for row in frame.rows))

    def test_signal_confirmation_passes_structured_bounded_identity_and_safety_context(self):
        task = task_record(slug="s" * 64)
        task["lifecycle"] = "running"
        task["runs"][0]["state"] = "idle"
        row = tui.TuiRow.from_records(
            task, {"contract": "asha.control-reconciliation.v1",
                   "task_id": task["task_id"], "state": "idle",
                   "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation(
                "idle", task["runs"][0]["run_id"], "test", None,
                "fresh", "test",
            ),
        )
        with mock.patch.object(tui, "_fresh_active_row", return_value=row), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_prompt_line", return_value=None) as prompt, \
                mock.patch.object(tui, "stop_task") as stop:
            message = tui._signal_task_action(
                key="f", expected_run_id=task["runs"][0]["run_id"],
                stdscr=mock.Mock(), curses_module=mock.Mock(),
                model=tui.TuiModel([row]), config=self.config, env=self.env,
                store=self.tasks, journals=self.journals, jj=mock.Mock(),
                task_id=task["task_id"],
            )
        self.assertEqual(message, "signal cancelled")
        self.assertEqual(prompt.call_args.args[3], "Confirm [yes/N]: ")
        self.assertIn("Finish task", prompt.call_args.kwargs["title"])
        context = prompt.call_args.kwargs["context"]
        for required in (
            task["slug"], task["task_id"], task["runs"][0]["run_id"],
            "SIGTERM", "does not archive", "workspace or change",
            "exact yes",
        ):
            self.assertIn(required, context)
        stop.assert_not_called()


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class RealConfirmationCursesTests(unittest.TestCase):
    """Real curses/PTY rendering of structured long confirmations."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.socket = f"asha-confirm-{os.getpid()}-{time.time_ns()}"
        self.env = {**os.environ, "TERM": "xterm-256color"}
        self.enterContext(TmuxSocketReaper(self.socket, environ=self.env))
        capability = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null",
             "list-commands", "new-session"],
            capture_output=True, text=True, check=False, env=self.env,
        )
        if capability.returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        self.helper = self.root / "confirm.py"
        repository = Path(__file__).resolve().parents[2]
        context = (
            "Task: " + "s" * 64 + " (11111111-1111-4111-8111-111111111111)\n"
            "Run: 22222222-2222-4222-8222-222222222222\n"
            "Signal: SIGTERM\n"
            "Preservation: does not archive the task and does not remove its "
            "workspace or change.\n"
            "Authorization: type exact yes (lowercase)."
        )
        self.helper.write_text(
            "import curses,sys\n"
            f"sys.path.insert(0, {str(repository)!r})\n"
            "from lib.control import tui\n"
            "def main(screen):\n"
            " curses.curs_set(1)\n"
            " model=tui.TuiModel([])\n"
            " answer=tui._prompt_line(screen,curses,model,'Confirm [yes/N]: ',"
            f"title='Finish task',context={context!r},maximum=4)\n"
            " open(sys.argv[1],'w',encoding='utf-8').write(repr(answer))\n"
            "curses.wrapper(main)\n",
            encoding="utf-8",
        )
        subprocess.run([
            "tmux", "-L", self.socket, "-f", "/dev/null", "new-session",
            "-d", "-s", "anchor", "/bin/sleep", "120",
        ], env=self.env, check=True)

    def _launch(self, width: int, name: str) -> Path:
        result = self.root / f"{name}.result"
        subprocess.run([
            "tmux", "-L", self.socket, "-f", "/dev/null", "new-session",
            "-d", "-x", str(width), "-y", "24", "-s", name,
            "/usr/bin/python3", str(self.helper), str(result),
        ], env=self.env, check=True)
        return result

    def _capture(self, name: str) -> str:
        return subprocess.run([
            "tmux", "-L", self.socket, "-f", "/dev/null", "capture-pane",
            "-p", "-t", name,
        ], env=self.env, capture_output=True, text=True, check=True).stdout

    def _wait_visible(self, name: str, width: int) -> str:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rendered = self._capture(name)
            if "exact yes" in rendered:
                compact = rendered.replace("\n", "")
                for required in (
                    "Finish task", "11111111-1111-4111-8111-111111111111",
                    "22222222-2222-4222-8222-222222222222", "Signal: SIGTERM",
                    "does not archive", "workspace or change",
                    "Confirm [yes/N]:",
                ):
                    self.assertIn(required, compact)
                self.assertTrue(all(len(line) <= width - 1 for line in rendered.splitlines()))
                return rendered
            time.sleep(0.05)
        self.fail(f"confirmation never became visible at width {width}: {rendered!r}")

    def test_real_curses_confirmation_width_resize_yes_and_escape_matrix(self):
        for width, answer, expected in ((40, "Escape", "None"),
                                        (80, "yes", "'yes'"),
                                        (120, "yes", "'yes'")):
            with self.subTest(width=width):
                name = f"width-{width}"
                result = self._launch(width, name)
                self._wait_visible(name, width)
                subprocess.run([
                    "tmux", "-L", self.socket, "-f", "/dev/null", "send-keys",
                    "-t", name, answer, *([] if answer == "Escape" else ["Enter"]),
                ], env=self.env, check=True)
                deadline = time.monotonic() + 5
                observed = None
                while time.monotonic() < deadline:
                    if result.exists():
                        observed = result.read_text(encoding="utf-8")
                        if observed == expected:
                            break
                    time.sleep(0.05)
                self.assertTrue(result.exists())
                self.assertEqual(observed, expected)

        result = self._launch(40, "resize")
        self._wait_visible("resize", 40)
        subprocess.run([
            "tmux", "-L", self.socket, "-f", "/dev/null", "resize-window",
            "-t", "resize", "-x", "120", "-y", "24",
        ], env=self.env, check=True)
        self._wait_visible("resize", 120)
        subprocess.run([
            "tmux", "-L", self.socket, "-f", "/dev/null", "send-keys",
            "-t", "resize", "yes", "Enter",
        ], env=self.env, check=True)
        deadline = time.monotonic() + 5
        observed = None
        while time.monotonic() < deadline:
            if result.exists():
                observed = result.read_text(encoding="utf-8")
                if observed == "'yes'":
                    break
            time.sleep(0.05)
        self.assertEqual(observed, "'yes'")


class BoundedProcessRepairTests(unittest.TestCase):
    def test_broken_stdin_pipe_reaps_and_closes_every_descriptor_without_resource_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with self.assertRaisesRegex(ValueError, "command input failed"):
                bounded_process(
                    ["/usr/bin/python3", "-c", "import os,time;os.close(0);time.sleep(.2)"],
                    cwd=None, limit=1024, error_type=ValueError,
                    deadline_seconds=1, input_data=b"x" * (4 * 1024 * 1024),
                )
            gc.collect()


if __name__ == "__main__":
    unittest.main()
