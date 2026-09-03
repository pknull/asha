from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control import tui
from lib.control.config import load_config
from lib.control.tui import ModalCandidate, TuiModel


class FakeCurses:
    KEY_RESIZE = 410
    KEY_UP = 259
    KEY_DOWN = 258
    KEY_ENTER = 343
    KEY_BACKSPACE = 263
    KEY_BTAB = 353
    A_BOLD = 1
    A_REVERSE = 2
    A_DIM = 4
    A_UNDERLINE = 8
    error = RuntimeError

    def __init__(self, cursor=0):
        self.cursor = cursor
        self.cursor_changes: list[int] = []

    def curs_set(self, visibility):
        previous = self.cursor
        self.cursor = visibility
        self.cursor_changes.append(visibility)
        return previous


class FakeScreen:
    def __init__(self, keys=(), *, height=18, width=80):
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.writes: list[tuple[int, int, str, int, int]] = []
        self.cursor = None

    def getmaxyx(self):
        return self.height, self.width

    def get_wch(self):
        return self.keys.pop(0)

    def getch(self):
        return self.keys.pop(0)

    def erase(self):
        pass

    def move(self, y, x):
        self.cursor = (y, x)

    def clrtoeol(self):
        pass

    def addnstr(self, y, x, value, limit, attribute=0):
        self.writes.append((y, x, value, limit, attribute))

    def refresh(self):
        pass

    def timeout(self, _value):
        pass

    @property
    def text(self):
        return "\n".join(item[2] for item in self.writes)


# The start form defaults its repository to the process cwd and previews that
# repository's default base. Neither may reach the runner's real checkout, or
# these tests pass only when they happen to run from inside a Git repository.
_STUB_DEFAULT_BASE = (
    ModalCandidate("", "current main @ 0123456789ab"), "0123456789ab",
)


def _stub_snapshot() -> "tui.StartCandidateSnapshot":
    repository = str(Path.cwd().resolve())
    return tui.StartCandidateSnapshot(
        repositories=(ModalCandidate(repository, "current"),),
        bases={repository: ()},
        harnesses=(
            ModalCandidate("codex", "installed"),
            ModalCandidate("claude", "installed"),
        ),
        roles=("implementer",),
    )


class ControlTuiFocusTests(unittest.TestCase):
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
            "ASHA_HOME": str(root / "asha"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.config = load_config(self.env)

    def test_tree_footer_names_navigation_even_when_it_is_the_only_row(self):
        model = TuiModel([], height=1, width=40)
        lines = tui.render(model)
        self.assertEqual(len(lines), 1)
        self.assertIn("[NAVIGATION]", lines[0])

    def test_modal_frame_marks_typing_and_active_input_in_plain_text(self):
        frame = tui.modal_frame(
            title="Open Room", context="Controls: Enter submit; Esc cancel",
            label="Room name", hint="", value="Draft",
            candidates=(ModalCandidate("Draft", "existing"),), selected=0,
            height=6, width=40,
        )
        self.assertIn("[TYPING]", frame.rows[frame.cursor[0]])
        self.assertIn("> Room name: Draft", frame.rows[frame.cursor[0]])
        self.assertEqual(frame.row_roles[frame.cursor[0]], "input")
        self.assertIn("selected", frame.row_roles)

    def test_modal_draw_emphasizes_input_dims_context_and_keeps_candidate_distinct(self):
        curses_module = FakeCurses()
        screen = FakeScreen()
        frame = tui.modal_frame(
            title="Choose", context="background", label="Harness", hint="",
            value="codex", candidates=(ModalCandidate("codex", "installed"),),
            selected=0, height=8, width=50,
        )
        tui._draw_modal_frame(screen, curses_module, frame)
        by_role = {
            role: attribute
            for role, (_y, _x, _value, _limit, attribute)
            in zip(frame.row_roles, screen.writes)
        }
        self.assertEqual(
            by_role["input"], FakeCurses.A_REVERSE | FakeCurses.A_BOLD,
        )
        self.assertEqual(by_role["context"], FakeCurses.A_DIM)
        self.assertEqual(
            by_role["selected"], FakeCurses.A_BOLD | FakeCurses.A_UNDERLINE,
        )

    def test_single_prompt_owns_cursor_and_restores_previous_state_on_escape_and_error(self):
        curses_module = FakeCurses(cursor=0)
        answer = tui._prompt_line(
            FakeScreen([27]), curses_module, TuiModel([]), "Reason: ",
        )
        self.assertIsNone(answer)
        self.assertEqual(curses_module.cursor_changes, [1, 0])

        class BrokenScreen(FakeScreen):
            def getmaxyx(self):
                raise ValueError("terminal disappeared")

        curses_module = FakeCurses(cursor=0)
        with self.assertRaisesRegex(ValueError, "terminal disappeared"):
            tui._prompt_line(
                BrokenScreen(), curses_module, TuiModel([]), "Reason: ",
            )
        self.assertEqual(curses_module.cursor_changes, [1, 0])

    def test_prompt_timeout_does_not_repaint_the_tree_or_prompt(self):
        immediate = FakeScreen([27])
        polled = FakeScreen([-1, 27])

        tui._prompt_line(immediate, FakeCurses(), TuiModel([]), "Reason: ")
        tui._prompt_line(polled, FakeCurses(), TuiModel([]), "Reason: ")

        self.assertEqual(polled.writes, immediate.writes)

    def test_start_and_room_form_timeouts_do_not_redraw_the_modal(self):
        with mock.patch.object(
            tui, "freeze_start_candidates", return_value=_stub_snapshot(),
        ):
            immediate_start = FakeScreen([27])
            polled_start = FakeScreen([-1, 27])
            self.assertEqual(
                tui._start_form(
                    immediate_start, FakeCurses(), TuiModel([]),
                    self.env, self.config,
                ),
                "task start cancelled",
            )
            self.assertEqual(
                tui._start_form(
                    polled_start, FakeCurses(), TuiModel([]),
                    self.env, self.config,
                ),
                "task start cancelled",
            )
        self.assertEqual(polled_start.writes, immediate_start.writes)

        project, payload = self._room_payload()
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(project.parent)], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=payload,
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ):
            immediate_room = FakeScreen([27])
            polled_room = FakeScreen([-1, 27])
            self.assertEqual(
                tui._open_room_form(
                    immediate_room, FakeCurses(), TuiModel([]),
                    self.config, self.env,
                ),
                "room open cancelled",
            )
            self.assertEqual(
                tui._open_room_form(
                    polled_room, FakeCurses(), TuiModel([]),
                    self.config, self.env,
                ),
                "room open cancelled",
            )
        self.assertEqual(polled_room.writes, immediate_room.writes)

    def test_idle_input_poll_does_not_repaint_the_tree(self):
        model = TuiModel([])
        model._ensure_screen()
        screen = FakeScreen([-1, ord("q")])
        runner = mock.Mock()
        runner.poll.side_effect = [None, None]

        with mock.patch.object(tui, "_paint") as paint:
            status = tui._curses_loop(
                screen, FakeCurses(), model, self.config, self.env,
                mock.Mock(skipped=[]), mock.Mock(), mock.Mock(),
                refresher=runner,
            )

        self.assertEqual(status, 0)
        paint.assert_called_once()
        runner.start.assert_called_once_with()
        runner.stop.assert_called_once_with()

    def test_cursor_visibility_is_nested_safe(self):
        curses_module = FakeCurses(cursor=0)
        with tui._visible_cursor(curses_module):
            with tui._visible_cursor(curses_module):
                self.assertEqual(curses_module.cursor, 1)
        self.assertEqual(curses_module.cursor_changes, [1, 1, 1, 0])

    def _room_payload(self):
        project = Path(self.env["HOME"]) / "Novel"
        (project / ".asha").mkdir(parents=True, exist_ok=True)
        (project / "Memory").mkdir(exist_ok=True)
        (project / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "novel-project", "name": "My Novel",
        }), encoding="utf-8")
        return project, {"projects": [{
            "root": str(project), "name": "My Novel", "directory": "Novel",
            "project_id": "novel-project", "asha_project": True,
        }]}

    def test_room_is_one_stateful_four_field_form_with_backward_retention(self):
        project, payload = self._room_payload()
        keys = [10, *"Draft", 9, FakeCurses.KEY_BTAB, *" 2", 9, 10,
                *"Revise chapter one", 10]
        screen = FakeScreen(keys)
        curses_module = FakeCurses()
        launched = {"name": "Draft 2", "project_name": "My Novel"}
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(project.parent)], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=payload,
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch(
            "lib.control.rooms.open_room", return_value=launched,
        ) as open_call, mock.patch.object(tui, "_refresh_initiatives"):
            result = tui._open_room_form(
                screen, curses_module, TuiModel([]), self.config, self.env,
            )

        self.assertIn("started detached", result)
        self.assertEqual(open_call.call_args.kwargs["project"], str(project))
        self.assertEqual(open_call.call_args.kwargs["name"], "Draft 2")
        self.assertEqual(open_call.call_args.kwargs["harness"], "codex")
        self.assertEqual(open_call.call_args.kwargs["prompt"], "Revise chapter one")
        self.assertEqual(curses_module.cursor_changes, [1, 0])
        self.assertIn("[TYPING]", screen.text)
        self.assertIn("Tab next", screen.text)
        self.assertIn("Shift-Tab previous", screen.text)

    def test_room_required_field_error_is_drawn_beside_field_and_shortcuts_are_text(self):
        project, payload = self._room_payload()
        screen = FakeScreen([10, 10, *"oX?", 27])
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(project.parent)], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=payload,
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch("lib.control.rooms.open_room") as open_call:
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertEqual(result, "room open cancelled")
        self.assertIn("Room name is required", screen.text)
        self.assertIn("> Room name: oX?", screen.text)
        open_call.assert_not_called()

    def test_room_project_accepts_exact_initialized_path_outside_empty_index(self):
        project = Path(self.env["HOME"]) / "Obsidian" / "AAS"
        (project / ".asha").mkdir(parents=True)
        (project / "Memory").mkdir()
        (project / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "aas-project", "name": "AAS",
        }), encoding="utf-8")
        screen = FakeScreen([
            *str(project), 10, *"AAS room", 9, 10, *"Work creatively", 10,
        ])
        launched = {"name": "AAS room", "project_name": "AAS"}
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value={"projects": []},
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch(
            "lib.control.rooms.open_room", return_value=launched,
        ) as open_call, mock.patch.object(tui, "_refresh_initiatives"):
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertIn("started detached", result)
        self.assertEqual(open_call.call_args.kwargs["project"], str(project))

    def test_room_project_validation_is_local_and_retains_invalid_draft(self):
        invalid = str(Path(self.env["HOME"]) / "not-initialized")
        screen = FakeScreen([*invalid, 10, 27])
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value={"projects": []},
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch("lib.control.rooms.open_room") as open_call:
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertEqual(result, "room open cancelled")
        self.assertIn("Error beside Project", screen.text)
        self.assertIn(invalid, screen.text)
        self.assertIn("run session-init there", screen.text)
        open_call.assert_not_called()

    def test_task_tab_accepts_the_explicitly_highlighted_candidate(self):
        snapshot = tui.StartCandidateSnapshot(
            repositories=(ModalCandidate(str(Path.cwd()), "current"),),
            bases={str(Path.cwd()): (ModalCandidate("main", "branch"),)},
            harnesses=(
                ModalCandidate("codex", "installed"),
                ModalCandidate("claude", "installed"),
            ),
            roles=("implementer",),
        )
        screen = FakeScreen([
            10, 10, FakeCurses.KEY_DOWN, 9, 10, *"goal", 10,
        ])
        with mock.patch.object(tui, "freeze_start_candidates", return_value=snapshot), \
                mock.patch.object(tui, "_default_base_candidate", return_value=_STUB_DEFAULT_BASE), \
                mock.patch.object(tui, "_source_colocation_watch", return_value=(None, False)), \
                mock.patch.object(tui, "_supervise_start_process", return_value="started") as start:
            result = tui._start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )

        self.assertEqual(result, "started")
        argv = start.call_args.args[4]
        self.assertEqual(argv[argv.index("--harness") + 1], "codex")

    def test_room_tab_accepts_highlight_and_canonicalizes_room_name(self):
        first, first_payload = self._room_payload()
        second = first.parent / "Second"
        (second / ".asha").mkdir(parents=True)
        (second / "Memory").mkdir()
        (second / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "second-project", "name": "Second",
        }), encoding="utf-8")
        payload = {"projects": [*first_payload["projects"], {
            "root": str(second), "name": "Second", "directory": "Second",
            "project_id": "second-project", "asha_project": True,
        }]}
        screen = FakeScreen([
            FakeCurses.KEY_DOWN, 9, *"Draft   Room", 9, 10,
            *"Revise carefully", 10,
        ])
        launched = {"name": "Draft Room", "project_name": "Second"}
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(first.parent)], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=payload,
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch(
            "lib.control.rooms.open_room", return_value=launched,
        ) as open_call, mock.patch.object(tui, "_refresh_initiatives"):
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertIn("started detached", result)
        self.assertEqual(open_call.call_args.kwargs["project"], str(second))
        self.assertEqual(open_call.call_args.kwargs["name"], "Draft Room")

    def test_room_form_bounds_navigation_to_the_same_128_candidates_it_draws(self):
        projects = []
        for index in range(130):
            project = Path(self.env["HOME"]) / "many" / f"project-{index:03d}"
            (project / ".asha").mkdir(parents=True)
            (project / "Memory").mkdir()
            project_id = f"project-{index:03d}"
            (project / ".asha/config.json").write_text(json.dumps({
                "initialized": True, "memory_version": 2,
                "project_id": project_id, "name": project_id,
            }), encoding="utf-8")
            projects.append({
                "root": str(project), "name": project_id,
                "directory": project.name, "project_id": project_id,
                "asha_project": True,
            })
        screen = FakeScreen([
            *([FakeCurses.KEY_DOWN] * 129), 10, *"Bounded", 9, 10,
            *"Stay visible", 10,
        ], height=24)
        launched = {"name": "Bounded", "project_name": "project-127"}
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(Path(self.env["HOME"]) / "many")], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value={"projects": projects},
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch(
            "lib.control.rooms.open_room", return_value=launched,
        ) as open_call, mock.patch.object(tui, "_refresh_initiatives"):
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertIn("started detached", result)
        self.assertEqual(
            open_call.call_args.kwargs["project"], projects[127]["root"],
        )
        self.assertNotIn(projects[128]["root"], screen.text)
        self.assertNotIn(projects[129]["root"], screen.text)

    def test_tab_on_final_task_goal_stays_in_form_until_enter(self):
        screen = FakeScreen([10, 10, 10, 10, *"goal", 9, 27])
        with mock.patch.object(
            tui, "freeze_start_candidates", return_value=_stub_snapshot(),
        ), mock.patch.object(
            tui, "_default_base_candidate", return_value=_STUB_DEFAULT_BASE,
        ), mock.patch.object(tui, "_supervise_start_process") as start:
            result = tui._start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )

        self.assertEqual(result, "task start cancelled")
        self.assertIn("Goal", screen.text)
        self.assertIn("Enter submits", screen.text)
        start.assert_not_called()

    def test_tab_on_final_room_prompt_stays_in_form_until_enter(self):
        project, payload = self._room_payload()
        screen = FakeScreen([10, *"Draft", 9, 10, *"prompt", 9, 27])
        with mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(project.parent)], "test"),
        ), mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=payload,
        ), mock.patch(
            "lib.control.rooms.room_harness_available",
            side_effect=lambda name, _env: name == "codex",
        ), mock.patch("lib.control.rooms.open_room") as open_call:
            result = tui._open_room_form(
                screen, FakeCurses(), TuiModel([]), self.config, self.env,
            )

        self.assertEqual(result, "room open cancelled")
        self.assertIn("Opening prompt", screen.text)
        self.assertIn("Enter submits", screen.text)
        open_call.assert_not_called()

    def test_invalid_room_names_are_local_and_retain_project_and_name(self):
        project, payload = self._room_payload()
        invalid_names = (
            ("😀", "usable ASCII slug"),
            ("a" + "界" * 40, "UTF-8 bytes"),
            ("---", "usable ASCII slug"),
        )
        for name, message in invalid_names:
            with self.subTest(name=name):
                screen = FakeScreen([10, *name, 10, 27], width=160)
                with mock.patch(
                    "lib.control.orchestration.projects.resolve_roots",
                    return_value=([str(project.parent)], "test"),
                ), mock.patch(
                    "lib.control.orchestration.projects.list_projects_across",
                    return_value=payload,
                ), mock.patch(
                    "lib.control.rooms.room_harness_available",
                    side_effect=lambda value, _env: value == "codex",
                ), mock.patch("lib.control.rooms.open_room") as open_call:
                    result = tui._open_room_form(
                        screen, FakeCurses(), TuiModel([]), self.config, self.env,
                    )

                self.assertEqual(result, "room open cancelled")
                self.assertIn("Error beside Room name", screen.text)
                self.assertIn(message, screen.text)
                self.assertIn(str(project), screen.text)
                self.assertIn(name, screen.text)
                open_call.assert_not_called()

    def test_task_validation_stays_in_the_active_form(self):
        snapshot = tui.StartCandidateSnapshot(
            repositories=(ModalCandidate("/repo", "current"),),
            bases={"/repo": (ModalCandidate("main", "branch"),)},
            harnesses=(ModalCandidate("codex", "installed"),),
            roles=("implementer",),
        )
        # Repo, Base, erase default harness, enter an invalid harness, then
        # acknowledge the rendered local error by cancelling.
        keys = [10, 10] + [127] * len(self.config.default_harness)
        keys += [*"invalid", 10, 27]
        screen = FakeScreen(keys)
        with mock.patch.object(tui, "freeze_start_candidates", return_value=snapshot), \
                mock.patch.object(tui, "_default_base_candidate", return_value=(
                    ModalCandidate("main", "branch"), "a" * 40,
                )), mock.patch.object(tui, "_supervise_start_process") as start:
            result = tui._start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )
        self.assertEqual(result, "task start cancelled")
        self.assertIn("Harness must be", screen.text)
        self.assertIn("> Harness: invalid", screen.text)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
