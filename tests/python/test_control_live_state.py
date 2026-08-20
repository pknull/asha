"""Focused regression coverage for Control's live TUI state projection."""

from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from lib.control import tui as tui_module, view
from lib.control.reconcile import (
    Evidence,
    StateObservation,
    reconcile_task_with_observation,
)
from lib.control.tui import TuiModel, TuiRow, render
from lib.control.transaction import JournalError
from tests.python.test_control_config_model import task_record


OBSERVED = "2026-08-18T12:00:00Z"


class StaticAdapters:
    def __init__(self, event: Evidence, *, process: Evidence | None = None) -> None:
        self._event = event
        self._process = process

    def tmux(self, task, run):
        return Evidence("tmux", "match", "owned pane", observed_at=OBSERVED)

    def process(self, task, run):
        return self._process or Evidence(
            "process", "match", "live process", observed_at=OBSERVED,
        )

    def jj(self, task):
        return Evidence("jj", "match", "owned workspace", observed_at=OBSERVED)

    def event(self, task, run):
        return self._event


class AtomicObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = task_record(slug="live-state")
        self.task["runs"][0]["state"] = "working"

    def reconcile(self, event: Evidence):
        return reconcile_task_with_observation(self.task, StaticAdapters(event))

    def test_winning_state_and_provenance_come_from_one_snapshot(self) -> None:
        result, observation = self.reconcile(Evidence(
            "event", "match", "verified turn-stopped event snapshot",
            state="idle", observed_at=OBSERVED,
        ))

        self.assertEqual(result["state"], "idle")
        self.assertEqual(observation, StateObservation(
            state="idle",
            run_id=self.task["runs"][0]["run_id"],
            source="event",
            observed_at=OBSERVED,
            freshness="durable",
            detail="verified turn-stopped event snapshot",
        ))
        self.assertEqual(
            set(result["evidence"][0]),
            {"source", "outcome", "detail", "state", "stale"},
        )
        self.assertNotIn("observed_at", result["evidence"][0])

    def test_missing_unavailable_and_stale_semantics_project_unknown(self) -> None:
        cases = (
            Evidence("event", "missing", "no event snapshot exists"),
            Evidence("event", "unavailable", "snapshot is malformed or unreadable"),
            Evidence(
                "event", "match", "working snapshot exceeded its trust window",
                state="working", stale=True, observed_at=OBSERVED,
            ),
        )
        for event in cases:
            with self.subTest(outcome=event.outcome, stale=event.stale):
                result, observation = self.reconcile(event)
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(observation.state, "unknown")
                self.assertEqual(observation.source, "event")
                self.assertEqual(
                    observation.freshness,
                    "stale" if event.stale else "unknown",
                )
                self.assertEqual(
                    TuiRow.from_records(
                        self.task, result, observation,
                    ).display_state,
                    "unknown",
                )

    def test_session_start_without_semantic_state_uses_verified_process_starting(self) -> None:
        task = copy.deepcopy(self.task)
        task["runs"][0]["state"] = "starting"
        process_observed = "2026-08-18T12:00:03Z"

        result, observation = reconcile_task_with_observation(
            task,
            StaticAdapters(
                Evidence("event", "missing", "session-start has no semantic state"),
                process=Evidence(
                    "process", "match", "verified live process",
                    observed_at=process_observed,
                ),
            ),
        )

        self.assertEqual(result["state"], "starting")
        self.assertEqual(observation.state, "starting")
        self.assertEqual(observation.source, "process")
        self.assertEqual(observation.observed_at, process_observed)

    def test_tui_row_refuses_state_provenance_divergence(self) -> None:
        result, _ = self.reconcile(Evidence(
            "event", "match", "verified working event",
            state="working", observed_at=OBSERVED,
        ))
        mismatched = StateObservation(
            "unknown", self.task["runs"][0]["run_id"], "event",
            None, "unknown", "missing semantic event",
        )

        with self.assertRaisesRegex(ValueError, "state does not match"):
            TuiRow.from_records(self.task, result, mismatched)

    def test_tui_uses_observation_time_and_names_winning_provenance(self) -> None:
        result, observation = self.reconcile(Evidence(
            "event", "match", "verified tool-completed event snapshot",
            state="working", observed_at=OBSERVED,
        ))
        # Mutation time is deliberately much newer. AGE must remain 10s.
        self.task["updated_at"] = "2026-08-18T12:00:09Z"
        model = TuiModel([
            TuiRow.from_records(self.task, result, observation),
        ], now=datetime(2026, 8, 18, 12, 0, 10, tzinfo=timezone.utc),
           height=30, width=160)

        output = "\n".join(render(model))

        table_row = next(line for line in output.splitlines() if "live-state" in line)
        self.assertTrue(table_row.rstrip().endswith("10s"), table_row)
        self.assertIn("Source:     event", output)
        self.assertIn(f"Observed:   {OBSERVED}", output)
        self.assertIn("Freshness:  fresh", output)

    def test_stored_terminal_state_uses_its_durable_evidence_timestamp(self) -> None:
        run = self.task["runs"][0]
        run["state"] = "exited"
        run["evidence"] = "persisted terminal reconciliation"
        run["evidence_at"] = OBSERVED
        self.task["updated_at"] = OBSERVED
        self.task["lifecycle"] = "ended"

        result, observation = reconcile_task_with_observation(
            self.task,
            StaticAdapters(
                Evidence("event", "missing", "terminal snapshot expired"),
                process=Evidence("process", "missing", "process is gone"),
            ),
        )

        self.assertEqual(result["state"], "exited")
        self.assertEqual(observation.source, "stored")
        self.assertEqual(observation.observed_at, OBSERVED)
        self.assertEqual(observation.freshness, "durable")
        self.assertEqual(observation.detail, "persisted terminal reconciliation")

    def test_prelaunch_state_has_durable_creation_provenance(self) -> None:
        self.task["lifecycle"] = "creating"
        self.task["runs"] = []

        result, observation = reconcile_task_with_observation(
            self.task, creation={"phase": "ready-for-launch"},
        )

        self.assertEqual(result["state"], "creating")
        self.assertEqual(observation.source, "creation")
        self.assertEqual(observation.freshness, "durable")
        self.assertEqual(observation.observed_at, self.task["updated_at"])
        _, repeated = reconcile_task_with_observation(
            self.task, creation={"phase": "ready-for-launch"},
        )
        self.assertEqual(repeated.observed_at, observation.observed_at)


class AutomaticRefreshTests(unittest.TestCase):
    class Screen:
        def __init__(self) -> None:
            self.keys = [-1, -1, -1, -1, ord("q")]
            self.timeout_ms = None

        def timeout(self, value) -> None:
            self.timeout_ms = value

        def getch(self):
            return self.keys.pop(0)

    class Curses:
        KEY_RESIZE = -10
        KEY_UP = -11
        KEY_DOWN = -12
        KEY_ENTER = -13

    def test_loop_reconciles_on_bounded_monotonic_multi_second_cadence(self) -> None:
        initial = task_record(slug="initial")
        result = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": initial["task_id"],
            "state": "starting",
            "blocker": None,
            "evidence": [],
            "runs": [],
        }
        model = TuiModel([TuiRow.from_records(initial, result)])
        refreshed = []
        for state, source, detail in (
            ("working", "event", "prompt submitted"),
            ("needs-input", "tmux", "visible permission prompt"),
            ("working", "event", "tool completed"),
            ("idle", "event", "turn stopped"),
        ):
            refreshed_result = dict(result, state=state)
            refreshed.append(TuiRow.from_records(
                initial, refreshed_result,
                StateObservation(
                    state, initial["runs"][0]["run_id"], source,
                    OBSERVED, "fresh", detail,
                ),
            ))
        screen = self.Screen()

        with mock.patch.object(tui_module, "_paint"), \
                mock.patch.object(
                    tui_module, "_load_rows",
                    side_effect=[[item] for item in refreshed],
                ) as load, \
                mock.patch.object(
                    tui_module.time, "monotonic",
                    side_effect=[
                        0.0,
                        5.1, 5.2,
                        10.2, 10.3,
                        15.3, 15.4,
                        20.4, 20.5,
                        20.6,
                    ],
                ):
            status = tui_module._curses_loop(
                screen, self.Curses(), model, SimpleNamespace(), {},
                SimpleNamespace(skipped=[]),
                mock.Mock(), mock.Mock(),
            )

        self.assertEqual(status, 0)
        self.assertEqual(screen.timeout_ms, 200)
        self.assertGreaterEqual(tui_module._AUTO_REFRESH_SECONDS, 3.0)
        self.assertEqual(load.call_count, 4)
        self.assertEqual(model.rows[0].display_state, "idle")
        self.assertEqual(model.rows[0].observation.source, "event")

    def test_slow_refresh_schedules_the_next_pass_from_completion(self) -> None:
        task = task_record(slug="slow-refresh")
        reconciliation = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": task["task_id"],
            "state": "working",
            "blocker": None,
            "evidence": [],
            "runs": [],
        }
        row = TuiRow.from_records(task, reconciliation)
        model = TuiModel([row])
        screen = self.Screen()
        screen.keys = [-1, ord("q")]

        # The refresh begins at 5.1s and finishes at 25s. The next 25.1s poll
        # must not refresh again; its deadline is 30s from completion.
        with mock.patch.object(tui_module, "_paint"), \
                mock.patch.object(tui_module, "_load_rows", return_value=[row]) as load, \
                mock.patch.object(
                    tui_module.time, "monotonic",
                    side_effect=[0.0, 5.1, 25.0, 25.1],
                ):
            status = tui_module._curses_loop(
                screen, self.Curses(), model, SimpleNamespace(), {},
                SimpleNamespace(skipped=[]), mock.Mock(), mock.Mock(),
            )

        self.assertEqual(status, 0)
        load.assert_called_once()

    def test_automatic_refresh_failure_is_visible_and_does_not_end_loop(self) -> None:
        task = task_record(slug="refresh-failure")
        reconciliation = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": task["task_id"],
            "state": "starting",
            "blocker": None,
            "evidence": [],
            "runs": [],
        }
        model = TuiModel([TuiRow.from_records(task, reconciliation)])
        screen = self.Screen()
        screen.keys = [-1, ord("q")]

        with mock.patch.object(tui_module, "_paint"), \
                mock.patch.object(
                    tui_module, "_load_rows", side_effect=ValueError("adapter failed"),
                ), mock.patch.object(
                    tui_module.time, "monotonic",
                    side_effect=[0.0, 5.1, 5.2, 5.3],
                ):
            status = tui_module._curses_loop(
                screen, self.Curses(), model, SimpleNamespace(), {},
                SimpleNamespace(skipped=[]), mock.Mock(), mock.Mock(),
            )

        self.assertEqual(status, 0)
        self.assertIsNone(model.message)
        self.assertEqual(
            model.automatic_refresh_error,
            "automatic reconciliation failed: adapter failed",
        )
        self.assertIn(
            "automatic reconciliation failed: adapter failed",
            "\n".join(render(model)),
        )

    def test_success_clears_only_the_previous_automatic_refresh_failure(self) -> None:
        task = task_record(slug="refresh-recovery")
        reconciliation = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": task["task_id"],
            "state": "starting",
            "blocker": None,
            "evidence": [],
            "runs": [],
        }
        row = TuiRow.from_records(task, reconciliation)
        model = TuiModel([row])
        model.message = "operator selected refresh-recovery"
        screen = self.Screen()
        screen.keys = [-1, -1, ord("q")]

        with mock.patch.object(tui_module, "_paint"), \
                mock.patch.object(
                    tui_module, "_load_rows",
                    side_effect=[ValueError("adapter failed"), [row]],
                ), mock.patch.object(
                    tui_module.time, "monotonic",
                    side_effect=[0.0, 5.1, 5.2, 10.3, 10.4],
                ):
            status = tui_module._curses_loop(
                screen, self.Curses(), model, SimpleNamespace(), {},
                SimpleNamespace(skipped=[]), mock.Mock(), mock.Mock(),
            )

        self.assertEqual(status, 0)
        self.assertEqual(model.message, "operator selected refresh-recovery")
        self.assertIsNone(model.automatic_refresh_error)

    def test_queued_navigation_and_quit_dispatch_before_due_slow_refresh(self) -> None:
        first = task_record(slug="first")
        second = task_record(slug="second")
        result = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": first["task_id"],
            "state": "starting",
            "blocker": None,
            "evidence": [],
            "runs": [],
        }
        model = TuiModel([
            TuiRow.from_records(first, result),
            TuiRow.from_records(second, dict(result, task_id=second["task_id"])),
        ])
        screen = self.Screen()
        screen.keys = [self.Curses.KEY_DOWN, ord("q")]

        with mock.patch.object(tui_module, "_paint"), \
                mock.patch.object(
                    tui_module, "_load_rows",
                    side_effect=AssertionError("queued input must win"),
                ) as load, mock.patch.object(
                    tui_module.time, "monotonic", side_effect=[0.0, 5.1, 5.2],
                ):
            status = tui_module._curses_loop(
                screen, self.Curses(), model, SimpleNamespace(), {},
                SimpleNamespace(skipped=[]), mock.Mock(), mock.Mock(),
            )

        self.assertEqual(status, 0)
        self.assertEqual(model.selection, 1)
        load.assert_not_called()

    def test_default_height_reserves_visible_space_for_automatic_refresh_error(self) -> None:
        rows = []
        for index in range(20):
            task = task_record(slug=f"many-{index:02d}")
            reconciliation = {
                "contract": "asha.control-reconciliation.v1",
                "task_id": task["task_id"],
                "state": "starting",
                "blocker": None,
                "evidence": [],
                "runs": [],
            }
            rows.append(TuiRow.from_records(task, reconciliation))
        model = TuiModel(rows)
        operator_message = "operator status " * 100
        model.message = operator_message
        model.automatic_refresh_error = (
            "automatic reconciliation failed: adapter failed"
        )

        output = render(model)

        self.assertTrue(
            any(
                "automatic reconciliation failed: adapter failed" in line
                for line in output
            ),
        )
        self.assertEqual(
            output[-1],
            "Enter inspect  x actions  A scope  n start  r reconcile  d diff  a archive  / filter  ? help  q quit",
        )
        self.assertEqual(model.message, operator_message)

    def test_batch_row_load_publishes_server_summary_once(self) -> None:
        tasks = [task_record(slug="one"), task_record(slug="two")]
        store = SimpleNamespace(list=lambda: tasks, skipped=[])
        rows = [
            TuiRow.from_records(task, {
                "contract": "asha.control-reconciliation.v1",
                "task_id": task["task_id"], "state": "starting",
                "blocker": None, "evidence": [], "runs": [],
            })
            for task in tasks
        ]
        adapter = mock.Mock()

        with mock.patch.object(tui_module, "_adapter_for_task", return_value=adapter), \
                mock.patch.object(tui_module, "_read_row", side_effect=rows), \
                mock.patch.object(tui_module.view, "publish_server_summary") as publish:
            loaded = tui_module._load_rows(
                mock.sentinel.config, store, mock.Mock(), mock.Mock(),
            )

        self.assertEqual(loaded, rows)
        self.assertEqual(publish.call_count, 1)
        self.assertIs(publish.call_args.args[1], adapter)
        self.assertIn("now", publish.call_args.kwargs)


class ReconciliationPresentationTests(unittest.TestCase):
    class Store:
        config = mock.sentinel.config

        def __init__(self, task: dict) -> None:
            self.task = task

        @contextlib.contextmanager
        def transaction_lock(self, task_id):
            yield

        def read(self, task_id):
            return copy.deepcopy(self.task)

    class Presentation:
        def __init__(
            self, task: dict, *, managed: str = "1",
            task_owner: str | None = None, run_owner: str | None = None,
            facts_session: str | None = None,
        ) -> None:
            self.task = task
            self.managed = managed
            self.task_owner = task["task_id"] if task_owner is None else task_owner
            run = task["runs"][0] if task["runs"] else None
            self.run_owner = (
                run["run_id"] if run_owner is None and run is not None else run_owner
            )
            self.facts_session = facts_session or task["tmux"]["session"]
            self.writes: list[tuple[str, str, str, str]] = []

        def session_option(self, session, option, **kwargs):
            if option == "@asha_managed":
                return self.managed
            if option == "@asha_task_id":
                return self.task_owner
            return None

        def pane_option(self, pane_id, option, **kwargs):
            return self.run_owner if option == "@asha_run_id" else None

        def pane_facts(self, pane_id, **kwargs):
            return SimpleNamespace(
                session=self.facts_session,
                window=self.task["tmux"]["window"],
            )

        def set_pane_option(self, pane_id, option, value, **kwargs):
            self.writes.append(("pane", pane_id, option, value))

        def set_session_option(self, session, option, value, **kwargs):
            self.writes.append(("session", session, option, value))

    def setUp(self) -> None:
        self.task = task_record(slug="presentation")
        self.task["runs"][0]["state"] = "working"
        self.adapters = StaticAdapters(Evidence(
            "event", "match", "stale working snapshot",
            state="working", stale=True, observed_at=OBSERVED,
        ))

    def test_both_locked_paths_mirror_the_derived_primary_run_state(self) -> None:
        run = self.task["runs"][0]
        for locked in (
            view.locked_reconciliation,
            view.locked_reconciliation_observation,
        ):
            with self.subTest(locked=locked.__name__):
                presentation = self.Presentation(self.task)
                with mock.patch.object(view, "publish_server_summary") as publish:
                    returned = locked(
                        self.Store(self.task), mock.Mock(), self.task["task_id"],
                        self.adapters, mock.Mock(), presentation=presentation,
                    )

                reconciliation = returned[1]
                self.assertEqual(reconciliation["state"], "unknown")
                self.assertEqual(presentation.writes, [
                    ("pane", run["pane_id"], "@asha_state", "unknown"),
                    (
                        "session", self.task["tmux"]["session"],
                        "@asha_state", "unknown",
                    ),
                ])
                publish.assert_called_once_with(mock.sentinel.config, presentation)

    def test_foreign_and_no_run_targets_are_never_written(self) -> None:
        foreign_cases = (
            {"managed": "0"},
            {"task_owner": "11111111-1111-4111-8111-111111111111"},
            {"run_owner": "22222222-2222-4222-8222-222222222222"},
            {"facts_session": "asha-foreign-deadbeef"},
        )
        for options in foreign_cases:
            with self.subTest(options=options):
                presentation = self.Presentation(self.task, **options)
                with mock.patch.object(view, "publish_server_summary"):
                    view.locked_reconciliation(
                        self.Store(self.task), mock.Mock(), self.task["task_id"],
                        self.adapters, mock.Mock(), presentation=presentation,
                    )
                self.assertEqual(presentation.writes, [])

        no_run = copy.deepcopy(self.task)
        no_run["runs"] = []
        no_run["lifecycle"] = "failed"
        presentation = self.Presentation(no_run)
        journals = mock.Mock()
        journals.read.side_effect = JournalError("creation journal not found")
        with mock.patch.object(view, "publish_server_summary"):
            view.locked_reconciliation(
                self.Store(no_run), journals, no_run["task_id"],
                self.adapters, mock.Mock(), presentation=presentation,
            )
        self.assertEqual(presentation.writes, [])

    def test_mirror_uses_atomic_task_observation_with_primary_run_target(self) -> None:
        failed = copy.deepcopy(self.task)
        failed["lifecycle"] = "failed"
        run = failed["runs"][0]
        presentation = self.Presentation(failed)

        with mock.patch.object(view, "publish_server_summary"):
            _, reconciliation = view.locked_reconciliation(
                self.Store(failed), mock.Mock(), failed["task_id"],
                self.adapters, mock.Mock(), presentation=presentation,
            )

        self.assertEqual(reconciliation["state"], "failed")
        self.assertEqual(reconciliation["runs"][0]["state"], "unknown")
        self.assertEqual(presentation.writes, [
            ("pane", run["pane_id"], "@asha_state", "failed"),
            (
                "session", failed["tmux"]["session"],
                "@asha_state", "failed",
            ),
        ])

    def test_mirror_and_summary_receive_the_same_sampled_time(self) -> None:
        presentation = self.Presentation(self.task)
        sampled = lambda: datetime(2026, 8, 18, 12, 0, 5, tzinfo=timezone.utc)

        with mock.patch.object(view, "publish_server_summary") as publish:
            view.locked_reconciliation(
                self.Store(self.task), mock.Mock(), self.task["task_id"],
                self.adapters, mock.Mock(), presentation=presentation,
                presentation_now=sampled,
            )

        publish.assert_called_once_with(
            mock.sentinel.config, presentation, now=sampled,
        )


if __name__ == "__main__":
    unittest.main()
