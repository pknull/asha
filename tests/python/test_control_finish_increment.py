from __future__ import annotations

import copy
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.store import TaskStore
from lib.control.transaction import CreationJournalStore
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control import tui
from tests.python.test_control_config_model import task_record
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class _Curses:
    KEY_RESIZE = 410
    KEY_UP = 259
    KEY_DOWN = 258
    KEY_ENTER = 343
    KEY_BACKSPACE = 263
    KEY_BTAB = 353
    error = RuntimeError


class _Screen:
    def __init__(self, keys=(), *, height=16, width=50):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.drawn: list[tuple[int, int, str, int]] = []
        self.cursor = None

    def getmaxyx(self):
        return self.height, self.width

    def getch(self):
        value = self.keys.pop(0)
        if isinstance(value, tuple):
            self.height, self.width = value
            return _Curses.KEY_RESIZE
        return value

    def get_wch(self):
        value = self.keys.pop(0)
        if isinstance(value, tuple):
            self.height, self.width = value
            return _Curses.KEY_RESIZE
        return value

    def erase(self):
        pass

    def clear(self):
        pass

    def move(self, y, x):
        self.cursor = (y, x)

    def clrtoeol(self):
        pass

    def addnstr(self, y, x, value, limit):
        self.drawn.append((y, x, value, limit))

    def refresh(self):
        pass

    def timeout(self, _value):
        pass


class ControlFinishIncrementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        home = root / "home"
        home.mkdir(mode=0o700)
        self.env = {
            "HOME": str(home),
            "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.config = load_config(self.env)
        self.tasks = TaskStore(self.config)
        self.journals = CreationJournalStore(self.config)
        preview = mock.patch.object(
            tui, "_default_base_candidate",
            return_value=(
                tui.ModalCandidate(
                    "", "current refs/heads/main @ " + "a" * 12,
                ),
                "a" * 40,
            ),
        )
        preview.start()
        self.addCleanup(preview.stop)

    def task(self, slug="work", *, state="working"):
        task = task_record(
            slug=slug,
            repository_root=str(self.config.home / slug),
            workspace_path=str(self.config.workspace_root / "repo" / slug),
        )
        task["lifecycle"] = "running"
        task["runs"][0]["state"] = state
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1",
             "task_id": task["task_id"], "state": state,
             "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation(
                state, task["runs"][0]["run_id"], "test", None,
                "fresh", state,
            ),
        )
        return task, row

    @staticmethod
    def independent_cells(value: str) -> int:
        # Deliberately narrow independent oracle for the clusters in this test.
        widths = {"界": 2, "🧑🏽\u200d💻": 2, "e\u0301": 1, "…": 1}
        total = 0
        while value:
            for cluster, width in sorted(widths.items(), key=lambda item: -len(item[0])):
                if value.startswith(cluster):
                    total += width
                    value = value[len(cluster):]
                    break
            else:
                total += 1
                value = value[1:]
        return total

    def test_modal_frame_is_cell_bounded_and_handles_tiny_resizes(self):
        candidates = tuple(
            tui.ModalCandidate(f"candidate-{index}-界", f"detail-{index}")
            for index in range(12)
        )
        for height in range(0, 7):
            for width in range(0, 9):
                with self.subTest(height=height, width=width):
                    frame = tui.modal_frame(
                        title="Start 🧑🏽\u200d💻", context="context",
                        label="Goal", hint="hint", value="e\u0301界",
                        candidates=candidates, selected=10,
                        height=height, width=width,
                    )
                    self.assertLessEqual(len(frame.rows), height)
                    self.assertTrue(all(
                        self.independent_cells(row) <= max(0, width - 1)
                        for row in frame.rows
                    ))
                    if frame.cursor is not None:
                        self.assertLess(frame.cursor[0], height)
                        self.assertLessEqual(frame.cursor[1], max(0, width - 1))
                    self.assertLessEqual(frame.visible_end - frame.visible_start, 8)

    def test_modal_frame_always_reserves_the_active_input_on_tiny_screens(self):
        for height in range(1, 5):
            for width in range(2, 7):
                with self.subTest(height=height, width=width):
                    frame = tui.modal_frame(
                        title="title", context="context", label="Goal",
                        hint="hint", value="x", candidates=(
                            tui.ModalCandidate("candidate", "detail"),
                        ), selected=0, height=height, width=width,
                    )
                    self.assertIsNotNone(frame.cursor)
                    self.assertLess(frame.cursor[0], len(frame.rows))
                    self.assertIn("x", frame.rows[frame.cursor[0]])

    def test_candidate_snapshot_is_frozen_bounded_ordered_and_deduplicated(self):
        older = task_record(
            slug="older", repository_root=str(self.config.home / "repo-a"),
            workspace_path=str(self.config.workspace_root / "x" / "older"),
        )
        older["updated_at"] = "2026-09-01T00:00:00.000000Z"
        older["jj"]["requested_base"] = "release"
        older["runs"][0]["role"] = "reviewer"
        newer = copy.deepcopy(older)
        newer["task_id"] = "22222222-2222-4222-8222-222222222222"
        newer["slug"] = "newer"
        newer["updated_at"] = "2026-10-01T00:00:00.000000Z"
        newer["jj"]["requested_base"] = "main"
        newer["runs"][0]["role"] = "debugger"
        second = task_record(
            slug="second", repository_root=str(self.config.home / "repo-b"),
            workspace_path=str(self.config.workspace_root / "x" / "second"),
        )
        second["updated_at"] = "2026-11-01T00:00:00.000000Z"
        second["jj"]["requested_base"] = "PR #12 head"
        for record in (older, newer, second):
            self.tasks.save(record)

        snapshot = tui.freeze_start_candidates(
            self.config, self.tasks, cwd=self.config.home / "repo-a",
            executable=lambda name: "/bin/true" if name == "codex" else None,
        )

        self.assertEqual(
            [item.value for item in snapshot.repositories[:2]],
            [str(self.config.home / "repo-a"), str(self.config.home / "repo-b")],
        )
        self.assertEqual(
            [item.value for item in snapshot.bases_for(str(self.config.home / "repo-a"))],
            ["", "main", "release"],
        )
        self.assertEqual(snapshot.roles[:3], ("implementer", "debugger", "reviewer"))
        self.assertEqual(snapshot.harnesses[0].value, self.config.default_harness)
        self.assertEqual(len(snapshot.harnesses), 4)
        self.assertIn("installed", next(
            item.detail for item in snapshot.harnesses if item.value == "codex"
        ))
        self.assertTrue(all(item.value != "PR #12 head" for item in snapshot.bases_for(
            str(self.config.home / "repo-b")
        )))

    def test_stateful_start_editor_keeps_command_keys_as_text_and_canonicalizes(self):
        observed = task_record(
            slug="observed-role", repository_root=str(self.config.home / "observed-repo"),
            workspace_path=str(self.config.workspace_root / "x" / "observed"),
        )
        observed["runs"][0]["role"] = "reviewer"
        self.tasks.save(observed)
        # Accept Repo/Base, replace Harness with mixed-case Codex, replace Role
        # with mixed-case observed prefix and Tab-complete, then type command keys.
        keys = [10, 10]
        keys += [127] * len(self.config.default_harness)
        keys += [ord(character) for character in "CoDeX"] + [10]
        keys += [127] * len("implementer")
        keys += [ord(character) for character in "REVIEW"] + [9, 10]
        keys += [ord(character) for character in "nAxd?"] + [10]
        screen = _Screen(keys, height=14, width=44)
        task_id = "55555555-5555-4555-8555-555555555555"

        with mock.patch.object(tui, "new_uuid", return_value=task_id), \
                mock.patch.object(tui, "_source_colocation_watch", return_value=(None, False)), \
                mock.patch.object(tui, "_supervise_start_process", return_value="started") as supervise:
            result = tui._start_form(
                screen, _Curses(), tui.TuiModel([]), self.env, self.config,
            )

        self.assertEqual(result, "started")
        argv = supervise.call_args.args[4]
        self.assertEqual(argv[argv.index("--harness") + 1], "codex")
        self.assertEqual(argv[argv.index("--role") + 1], "reviewer")
        self.assertEqual(argv[argv.index("--goal") + 1], "nAxd?")
        self.assertEqual(argv[argv.index("--task-id") + 1], task_id)
        self.assertIn("--detach", argv)
        self.assertIn("--json", argv)
        self.assertTrue(all(
            self.independent_cells(value) <= screen.width - 1
            for _y, _x, value, _limit in screen.drawn
        ))

    def test_start_editor_shift_tab_retains_fields_and_escape_never_starts_worker(self):
        # Repo -> Base -> back to Repo -> Base, then cancel at Harness.
        screen = _Screen([10, _Curses.KEY_BTAB, 10, 10, 27], width=24)
        with mock.patch.object(tui, "_supervise_start_process") as supervise:
            result = tui._start_form(
                screen, _Curses(), tui.TuiModel([]), self.env, self.config,
            )
        self.assertEqual(result, "task start cancelled")
        supervise.assert_not_called()

    def test_start_editor_resize_and_whole_cluster_backspace_preserve_logical_input(self):
        keys = [10, 10, 10, 10, (3, 4), ord("e"), 0x0301, 127,
                (12, 40), ord("界"), 10]
        screen = _Screen(keys, width=30)
        with mock.patch.object(tui, "_source_colocation_watch", return_value=(None, False)), \
                mock.patch.object(tui, "_supervise_start_process", return_value="started") as supervise:
            self.assertEqual(
                tui._start_form(screen, _Curses(), tui.TuiModel([]), self.env, self.config),
                "started",
            )
        argv = supervise.call_args.args[4]
        self.assertEqual(argv[argv.index("--goal") + 1], "界")

    def test_candidate_bounds_cap_count_and_utf8_display_bytes(self):
        count_limited = tui._bounded_modal_candidates(
            tui.ModalCandidate(str(index), "") for index in range(500)
        )
        byte_limited = tui._bounded_modal_candidates([
            tui.ModalCandidate("界" * (100 * 1024), ""),
            tui.ModalCandidate("界" * (100 * 1024), "second"),
        ])
        self.assertEqual(len(count_limited), 128)
        self.assertEqual(len(byte_limited), 0)

    def test_shared_modal_editor_navigates_candidates_and_backspaces_whole_cluster(self):
        selected = tui._prompt_line(
            _Screen([_Curses.KEY_DOWN, 10]), _Curses(), tui.TuiModel([]),
            "Action: ", candidates=(
                tui.ModalCandidate("i", "inspect"),
                tui.ModalCandidate("t", "terminate"),
            ), maximum=1,
        )
        cluster = tui._prompt_line(
            _Screen([ord("e"), 0x0301, 127, ord("x"), 10]),
            _Curses(), tui.TuiModel([]), "Confirm: ", maximum=8,
        )
        self.assertEqual(selected, "i")
        self.assertEqual(cluster, "x")

    def test_prompt_uses_wide_input_and_preserves_supported_unicode_clusters(self):
        class WideOnly(_Screen):
            def getch(self):
                raise AssertionError("byte-oriented getch must not be used")

        logical = "界e\u0301🧑🏽\u200d💻"
        screen = WideOnly([*logical, "\n"], width=12)
        result = tui._prompt_line(
            screen, _Curses(), tui.TuiModel([]), "Goal: ", maximum=200,
        )
        self.assertEqual(result, logical)

    def test_raw_candidate_identity_is_not_replaced_by_sanitized_display(self):
        raw = str(self.config.home / "repo\u200dname")
        sibling = str(self.config.home / "repo?name")
        candidates = tui._bounded_modal_candidates((
            tui.ModalCandidate(raw, "raw"),
            tui.ModalCandidate(sibling, "sibling"),
        ))
        self.assertEqual([item.value for item in candidates], [raw, sibling])
        self.assertNotEqual(candidates[0].display_value, candidates[0].value)
        selected = tui._prompt_line(
            _Screen(["\n"]), _Curses(), tui.TuiModel([]), "Repo: ",
            candidates=candidates, selected=0, maximum=4096,
        )
        self.assertEqual(selected, raw)

    def test_start_form_submits_exact_raw_repo_candidate_and_rejects_non_ascii_harness(self):
        raw = str(self.config.home / "repo\u200dname")
        snapshot = tui.StartCandidateSnapshot(
            repositories=(tui.ModalCandidate(raw, "raw"),),
            bases={raw: (tui.ModalCandidate("", "default"),)},
            harnesses=(tui.ModalCandidate("codex", "installed"),),
            roles=("implementer",),
        )
        self.assertIsNone(tui._canonical_field_value(
            2, "c😀odex", snapshot.harnesses,
        ))
        screen = _Screen([
            _Curses.KEY_DOWN, "\n", "\n",
            *([127] * len(self.config.default_harness)), *"codex", "\n",
            "\n", *"goal", "\n",
        ])
        with mock.patch.object(tui, "freeze_start_candidates", return_value=snapshot), \
                mock.patch.object(
                    tui, "_default_base_candidate",
                    return_value=(
                        tui.ModalCandidate(
                            "", "current refs/heads/main @ " + "a" * 12,
                        ),
                        "a" * 40,
                    ),
                ), \
                mock.patch.object(tui, "_source_colocation_watch", return_value=(None, False)), \
                mock.patch.object(tui, "_supervise_start_process", return_value="started") as supervise:
            self.assertEqual(tui._start_form(
                screen, _Curses(), tui.TuiModel([]), self.env, self.config,
            ), "started")
        argv = supervise.call_args.args[4]
        self.assertEqual(argv[argv.index("--repo") + 1], raw)

    def test_idle_finish_confirms_sigterm_and_uses_shared_stop_controller(self):
        task, row = self.task(state="idle")
        self.tasks.save(task)
        model = tui.TuiModel([row])
        stopped = {"task_id": task["task_id"], "run_id": task["runs"][0]["run_id"],
                   "signal": "TERM"}
        prompts = iter(["f", "yes"])

        with mock.patch.object(tui, "_prompt_line", side_effect=lambda *a, **k: next(prompts)) as prompt, \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_read_row", return_value=row), \
                mock.patch.object(tui, "stop_task", return_value=stopped) as stop:
            tui._context_actions(
                stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=self.config, env=self.env, store=self.tasks,
                journals=self.journals, jj=mock.Mock(), task_id=task["task_id"],
            )

        menu = prompt.call_args_list[0].kwargs["context"]
        confirmation = prompt.call_args_list[1].kwargs["context"]
        self.assertIn("Finish", menu)
        self.assertNotIn("Interrupt", menu)
        self.assertIn(task["slug"], confirmation)
        self.assertIn(task["runs"][0]["run_id"], confirmation)
        self.assertIn("SIGTERM", confirmation)
        self.assertIn("does not archive", confirmation)
        self.assertIn("workspace or change", confirmation)
        stop.assert_called_once()
        self.assertTrue(stop.call_args.kwargs["terminate"])
        self.assertIn("SIGTERM requested", model.message)
        self.assertNotIn("exited", model.message)

    def test_working_context_offers_interrupt_and_terminate_with_exact_confirmation(self):
        task, row = self.task(state="working")
        self.tasks.save(task)
        model = tui.TuiModel([row])
        with mock.patch.object(tui, "_prompt_line", side_effect=["I", "YES"]) as prompt, \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_read_row", return_value=row), \
                mock.patch.object(tui, "stop_task") as stop:
            message = tui._context_actions(
                stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=self.config, env=self.env, store=self.tasks,
                journals=self.journals, jj=mock.Mock(), task_id=task["task_id"],
            )
        self.assertIn("Interrupt", prompt.call_args_list[0].kwargs["context"])
        self.assertIn("Terminate", prompt.call_args_list[0].kwargs["context"])
        self.assertEqual(message, "signal cancelled")
        stop.assert_not_called()

    def test_unowned_signal_matrix_is_closed(self):
        self.assertEqual(
            tui._UNOWNED_SIGNAL_ACTIONS,
            {
                "starting": (("I", "Interrupt", False), ("t", "Terminate", True)),
                "working": (("I", "Interrupt", False), ("t", "Terminate", True)),
                "needs-input": (("I", "Interrupt", False), ("t", "Terminate", True)),
                "idle": (("f", "Finish", True),),
                "unknown": (("t", "Terminate", True),),
            },
        )

    def test_unknown_direct_terminate_uses_sigterm(self):
        task, row = self.task(state="unknown")
        self.tasks.save(task)
        model = tui.TuiModel([row])
        with mock.patch.object(tui, "_prompt_line", side_effect=["t", "yes"]), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_read_row", return_value=row), \
                mock.patch.object(tui, "stop_task", return_value={
                    "task_id": task["task_id"],
                    "run_id": task["runs"][0]["run_id"], "signal": "TERM",
                }) as stop:
            result = tui._context_actions(
                stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=self.config, env=self.env, store=self.tasks,
                journals=self.journals, jj=mock.Mock(), task_id=task["task_id"],
            )
        self.assertTrue(stop.call_args.kwargs["terminate"])
        self.assertIn("SIGTERM requested", result)

    def test_signal_refuses_stale_state_after_confirmation_without_controller_call(self):
        task, working = self.task(state="working")
        self.tasks.save(task)
        idle_task = copy.deepcopy(task)
        idle_task["runs"][0]["state"] = "idle"
        idle = tui.TuiRow.from_records(
            idle_task,
            {"contract": "asha.control-reconciliation.v1",
             "task_id": task["task_id"], "state": "idle", "blocker": None,
             "evidence": [], "runs": []},
            tui.StateObservation("idle", task["runs"][0]["run_id"], "test",
                                 None, "fresh", "idle"),
        )
        model = tui.TuiModel([working])
        with mock.patch.object(tui, "_prompt_line", side_effect=["I", "yes"]), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_read_row", side_effect=[working, working, idle]), \
                mock.patch.object(tui, "stop_task") as stop:
            with self.assertRaisesRegex(ValueError, "state changed"):
                tui._context_actions(
                    stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                    config=self.config, env=self.env, store=self.tasks,
                    journals=self.journals, jj=mock.Mock(), task_id=task["task_id"],
                )
        stop.assert_not_called()

    def test_real_curses_loop_drives_finish_confirmation_and_refresh(self):
        task, idle = self.task(state="idle")
        self.tasks.save(task)
        model = tui.TuiModel([idle])
        screen = _Screen([
            ord("x"), ord("f"), 10, ord("y"), ord("e"), ord("s"), 10,
            ord("q"),
        ], width=70)
        with mock.patch.object(tui, "_read_row", return_value=idle), \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "stop_task", return_value={
                    "task_id": task["task_id"],
                    "run_id": task["runs"][0]["run_id"], "signal": "TERM",
                }) as stop:
            status = tui._curses_loop(
                screen, _Curses(), model, self.config, self.env,
                self.tasks, self.journals, mock.Mock(),
            )
        self.assertEqual(status, 0)
        stop.assert_called_once()
        self.assertIn("current observed state: idle", model.message)

    def test_owned_active_only_submits_one_tui_stop_attempt_action(self):
        task, row = self.task(state="working")
        self.tasks.save(task)
        model = tui.TuiModel([row])
        initiative = {
            "initiative_id": "22222222-2222-4222-8222-222222222222",
            "slug": "owned-initiative", "state_revision": 3,
            "active_plan": {"digest": "a" * 64},
        }
        link = {"node_id": "node-a", "attempt_id": "33333333-3333-4333-8333-333333333333"}
        attempt = {"attempt_id": link["attempt_id"], "state": "running"}
        initiative_store = mock.Mock()
        initiative_store.read_attempt.return_value = {
            "attempt_id": link["attempt_id"], "state": "cancelled",
            "refusal": None,
        }
        binding = (initiative_store, initiative, link, attempt, {"state": "running"}, task)
        completed = {"action_id": "44444444-4444-4444-8444-444444444444",
                     "state": "completed", "outcome": '{"status":"cancelled"}'}

        with mock.patch.object(tui, "_prompt_line", side_effect=["s", "yes"]) as prompt, \
                mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch.object(tui, "_read_row", return_value=row), \
                mock.patch("lib.control.orchestration.actions.submit_action", return_value=completed) as submit, \
                mock.patch.object(tui, "stop_task", side_effect=AssertionError("raw stop forbidden")), \
                mock.patch("lib.control.orchestration.cli.reconcile_one_initiative",
                           side_effect=AssertionError("broad reconcile forbidden")):
            result = tui._context_actions(
                stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=self.config, env=self.env, store=self.tasks,
                journals=self.journals, jj=mock.Mock(), task_id=task["task_id"],
            )

        menu = prompt.call_args_list[0].kwargs["context"]
        self.assertIn("Stop attempt via initiative", menu)
        self.assertNotIn("Interrupt", menu)
        self.assertNotIn("Terminate", menu)
        self.assertEqual(submit.call_count, 1)
        document = submit.call_args.args[2]
        self.assertEqual(document["actor_id"], "tui")
        self.assertEqual(document["action_class"], "stop-attempt")
        self.assertEqual(document["payload"], {"attempt_id": link["attempt_id"]})
        self.assertIn(completed["action_id"], result)
        self.assertIn("attempt cancelled", result)
        self.assertIn("OS state observed: working", result)

    def test_broad_initiative_reconcile_reports_replayed_action_evidence(self):
        task, _row = self.task(state="working")
        self.tasks.save(task)
        initiative_store = mock.Mock()
        initiative_store.read_node.return_value = {"state": "ready"}
        initiative = {"initiative_id": "22222222-2222-4222-8222-222222222222",
                      "slug": "initiative"}
        link = {"node_id": "node-a", "attempt_id": "33333333-3333-4333-8333-333333333333"}
        binding = (initiative_store, initiative, link, {"state": "reported"}, {}, task)
        replayed_id = "44444444-4444-4444-8444-444444444444"
        payload = {
            "action_reconciliation": {"actions": [{
                "action_id": replayed_id, "state": "completed",
                "action_class": "dispatch-node",
            }]},
            "live_reconciliation": {"retries": []},
        }
        with mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch("lib.control.orchestration.cli.reconcile_one_initiative",
                           return_value=payload):
            message = tui._reconcile_task_initiative(
                env=self.env, store=self.tasks, task_id=task["task_id"],
            )
        self.assertIn(f"{replayed_id}:completed", message)
        self.assertNotIn("nothing dispatched", message)


class RealOwnedStopActionTests(ExecutionFixture, unittest.TestCase):
    def test_tui_submits_one_real_stop_attempt_without_scheduler_or_raw_stop(self):
        payloads = []

        def create_control(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"]["workspace_path"] = str(
                self.config.control.workspace_root / "owned" /
                payload["task"]["task_id"]
            )
            payload["workspace"]["path"] = payload["task"]["jj"]["workspace_path"]
            payloads.append(payload)
            return 0, json.dumps(payload).encode(), b""

        dispatch_document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=create_control,
        ):
            submit_action(self.store, self.initiative_id, dispatch_document)
        task = payloads[0]["task"]
        self.repo.chmod(0o755)
        control_config = self.config.control
        control_tasks = TaskStore(control_config)
        control_tasks.save(task)
        journals = CreationJournalStore(control_config)
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1",
             "task_id": task["task_id"], "state": "working",
             "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation(
                "working", task["runs"][0]["run_id"], "test", None,
                "fresh", "working",
            ),
        )
        model = tui.TuiModel([row])
        with mock.patch.object(tui, "_prompt_line", side_effect=["s", "yes"]), \
                mock.patch.object(tui, "_read_row", return_value=row), \
                mock.patch.object(
                    tui, "stop_task", side_effect=AssertionError("raw stop forbidden"),
                ), mock.patch(
                    "lib.control.orchestration.scheduler.dispatch",
                    side_effect=AssertionError("scheduler dispatch forbidden"),
                ), mock.patch(
                    "lib.control.orchestration.actions.capture_bytes",
                    return_value=(0, b"", b""),
                ):
            message = tui._context_actions(
                stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=control_config, env=self.env, store=control_tasks,
                journals=journals, jj=mock.Mock(), task_id=task["task_id"],
            )
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        stop_actions = [item for item in self.store.list_actions_snapshot(self.initiative_id)
                        if item["action_class"] == "stop-attempt"]
        self.assertEqual(attempts[0]["state"], "cancelled", stop_actions)
        self.assertEqual(len(stop_actions), 1)
        self.assertEqual(stop_actions[0]["actor_id"], "tui")
        self.assertIn(stop_actions[0]["action_id"], message)
        self.assertIn("OS state observed: working", message)


@unittest.skipUnless(shutil.which("tmux") and shutil.which("jj"), "tmux and jj required")
class RealControlFinishPtyTests(unittest.TestCase):
    """Full disposable curses→Control→tmux signal/archive product path."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.source = self.root / "source"
        self.source.mkdir()
        self.source.chmod(0o755)
        self.state = self.root / "state"
        self.data = self.root / "data"
        self.runtime = self.root / "runtime"
        for path in (self.state, self.data, self.runtime):
            path.mkdir(mode=0o700)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps({"control": {"default_harness": "codex"}}) + "\n",
            encoding="utf-8",
        )
        self.config_path.chmod(0o600)
        self.env = {
            **os.environ,
            "HOME": str(self.home), "ASHA_CONFIG": str(self.config_path),
            "XDG_STATE_HOME": str(self.state), "XDG_DATA_HOME": str(self.data),
            "XDG_RUNTIME_DIR": str(self.runtime), "TERM": "xterm-256color",
        }
        git_env = {
            **self.env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.source)],
                       check=True, env=git_env)
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.source / ".gitignore").write_text(
            "/.asha/\n/Memory/\n/Work/\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.source), "add", "."],
                       check=True, env=git_env)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "base"],
                       check=True, env=git_env)
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "control-finish-pty",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        self.before = self._git_semantics()
        self.signal_log = self.root / "signals"
        self.fake_root = self.root / "fake-asha"
        (self.fake_root / "bin").mkdir(parents=True)
        (self.fake_root / "lib").symlink_to(
            Path(__file__).resolve().parents[2] / "lib", target_is_directory=True,
        )
        harness = self.fake_root / "fake_harness.py"
        harness.write_text(
            "import os,signal,subprocess,sys,time\n"
            "log=os.environ['ASHA_TEST_SIGNAL_LOG']\n"
            "asha=os.path.join(os.environ['ASHA_ROOT'],'bin','asha')\n"
            "def event(name):\n"
            " subprocess.run([asha,'control','event','--event',name,'--harness','codex',"
            "'--session-id','fake-session','--pane-id',os.environ['TMUX_PANE'],'--json'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)\n"
            "def on_int(_s,_f):\n"
            " open(log,'a',encoding='utf-8').write('INT\\n'); event('turn-stopped')\n"
            "def on_term(_s,_f):\n"
            " open(log,'a',encoding='utf-8').write('TERM\\n'); raise SystemExit(0)\n"
            "signal.signal(signal.SIGINT,on_int); signal.signal(signal.SIGTERM,on_term)\n"
            "time.sleep(1); event('prompt-submitted')\n"
            "while True: time.sleep(0.1)\n",
            encoding="utf-8",
        )
        launcher = self.fake_root / "bin" / "asha"
        launcher.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = codex ]; then shift; exec /usr/bin/python3 \"$ASHA_ROOT/fake_harness.py\" \"$@\"; fi\n"
            "exec /usr/bin/python3 -B -I -c 'import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module(\"control.cli\",run_name=\"__main__\")' \"$ASHA_ROOT/lib\" \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        self.env.update({
            "ASHA_ROOT": str(self.fake_root),
            "ASHA_TEST_SIGNAL_LOG": str(self.signal_log),
            "PATH": str(self.fake_root / "bin") + os.pathsep + self.env["PATH"],
        })
        self.socket = f"asha-finish-{os.getpid()}-{time.time_ns()}"
        subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "new-session", "-d",
             "-s", "anchor", "/bin/sleep", "120"],
            check=True, env=self.env,
        )
        self.addCleanup(lambda: subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "kill-server"],
            capture_output=True, check=False,
        ))
        server_pid = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "display-message",
             "-p", "#{pid}"], capture_output=True, text=True, check=True,
        ).stdout.strip()
        socket_path = f"/tmp/tmux-{os.getuid()}/{self.socket}"
        self.env.update({
            "TMUX": f"{socket_path},{server_pid},0",
        })
        self.config = load_config(self.env)
        self.tasks = TaskStore(self.config)

    def _git_semantics(self):
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            capture_output=True, check=True,
        ).stdout
        status = subprocess.run(
            ["git", "-C", str(self.source), "status", "--porcelain=v2", "-z"],
            capture_output=True, check=True,
        ).stdout
        refs = subprocess.run(
            ["git", "-C", str(self.source), "for-each-ref", "--format=%(refname) %(objectname)"],
            capture_output=True, check=True,
        ).stdout.splitlines()
        return head, status, tuple(line for line in refs if not line.startswith(b"refs/jj/"))

    def _wait(self, predicate, *, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        detail = getattr(self, "captured", b"")[-4000:].decode("utf-8", errors="replace")
        self.fail(f"bounded PTY product path timed out; pane tail: {detail!r}")

    def _drain(self):
        if not hasattr(self, "master"):
            return
        while select.select([self.master], [], [], 0)[0]:
            try:
                raw = os.read(self.master, 65536)
                self.captured = (getattr(self, "captured", b"") + raw)[-256 * 1024:]
            except OSError:
                return

    def _send(self, value: bytes, *, settle=0.3):
        os.write(self.master, value)
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            self._drain()
            time.sleep(0.01)

    def test_full_start_interrupt_finish_archive_scope_path(self):
        self.master, slave = pty.openpty()
        self.captured = b""
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        child = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-I", "-c",
             "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
             "runpy.run_module('control.cli',run_name='__main__')",
             str(self.fake_root / "lib"), "control"],
            cwd=self.source, env=self.env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self.addCleanup(lambda: child.poll() is None and child.terminate())
        logical_goal = "PTY 界 e\u0301 🧑🏽\u200d💻"
        try:
            self._send(("n\r\r\r\r" + logical_goal + "\r").encode("utf-8"), settle=1)
            task = self._wait(lambda: self.tasks.list()[0] if self.tasks.list() else None)
            task_id = task["task_id"]
            task = self._wait(
                lambda: (current if (current := self.tasks.read(task_id))["lifecycle"]
                         == "running" and current["runs"] else None),
                timeout=45,
            )
            # The event snapshot is deliberately presentation evidence; it
            # does not rewrite the durable launch record's initial state.
            time.sleep(1.5)
            self._send(b"r", settle=1)
            self._send(b"xI\ryes\r", settle=1)
            self._wait(lambda: self.signal_log.exists() and
                       self.signal_log.read_text().splitlines().count("INT") == 1)
            time.sleep(0.5)
            self._send(b"r", settle=1)
            self._send(b"xf\ryes\r", settle=1)
            self._wait(lambda: self.signal_log.read_text().splitlines().count("TERM") == 1)
            self._send(b"r", settle=2)
            self._send(b"ayes\r", settle=1)
            archived = self._wait(
                lambda: (current if (current := self.tasks.read(task_id))["lifecycle"]
                         == "archived" else None),
            )
            self._send(b"Aq", settle=0.5)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            os.close(self.master)
        self.assertEqual(self.signal_log.read_text().splitlines(), ["INT", "TERM"])
        self.assertEqual(len(self.tasks.list()), 1)
        self.assertEqual(len(archived["runs"]), 1)
        self.assertEqual(archived["label"], logical_goal)
        self.assertTrue(Path(archived["jj"]["workspace_path"]).is_dir())
        self.assertIsNotNone(archived["jj"]["change_id"])
        self.assertFalse(tui.PruneRecordStore(self.config).path(task_id).exists())
        self.assertEqual(self._git_semantics(), self.before)
        rendered = self.captured.decode("utf-8", errors="replace")
        for state in ("starting", "working", "idle", "exited", "archived"):
            self.assertIn(state, rendered)

    def test_direct_terminate_path_sends_only_one_sigterm(self):
        self.master, slave = pty.openpty()
        self.captured = b""
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        child = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-I", "-c",
             "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
             "runpy.run_module('control.cli',run_name='__main__')",
             str(self.fake_root / "lib"), "control"],
            cwd=self.source, env=self.env, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        try:
            self._send(b"n\r\r\r\rterminate directly\r", settle=1)
            task = self._wait(lambda: self.tasks.list()[0] if self.tasks.list() else None)
            task_id = task["task_id"]
            self._wait(lambda: self.signal_log.exists() or b"working" in self.captured,
                       timeout=45)
            time.sleep(1.5)
            self._send(b"r", settle=1)
            self._send(b"xt\ryes\r", settle=1)
            self._wait(lambda: self.signal_log.exists() and
                       self.signal_log.read_text().splitlines().count("TERM") == 1)
            self._send(b"r", settle=2)
            terminal = self._wait(
                lambda: (current if (current := self.tasks.read(task_id))["lifecycle"]
                         == "ended" else None),
            )
            self._send(b"q", settle=0.2)
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            os.close(self.master)
        self.assertEqual(self.signal_log.read_text().splitlines(), ["TERM"])
        self.assertEqual(len(terminal["runs"]), 1)
        self.assertTrue(Path(terminal["jj"]["workspace_path"]).is_dir())
        self.assertFalse(tui.PruneRecordStore(self.config).path(task_id).exists())


if __name__ == "__main__":
    unittest.main()
