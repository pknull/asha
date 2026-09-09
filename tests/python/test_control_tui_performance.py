"""Deterministic refresh-cost and input-isolation regressions for Control."""

from __future__ import annotations

import ast
import contextlib
import copy
import inspect
import json
import subprocess
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control import tui
from lib.control.orchestration import cli
from lib.control.orchestration import store as tui_store
from lib.control.orchestration import tui_model
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.tui_model import (
    InitiativesScreen, attention_items, retained_classification,
)
from lib.control.reconcile import Evidence, StateObservation
from lib.control.tmux import TmuxAdapter, TmuxError
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_control_config_model import task_record


def _terminal_task(slug: str) -> dict:
    task = task_record(slug=slug)
    task["lifecycle"] = "ended"
    task["runs"][0]["state"] = "exited"
    task["runs"][0]["evidence"] = "durable terminal evidence"
    return task


def _snapshot(
    rows=(), *, changed_rows=None, removed=(), order_changed=True,
    initiatives=(), rooms=(), initiatives_changed=True, rooms_changed=True,
    generation=0, delta_base=None,
) -> tui.RefreshSnapshot:
    return tui.RefreshSnapshot(
        rows=tuple(rows), initiative_views=tuple(initiatives),
        room_rows=tuple(rooms), generation=generation,
        changed_rows=changed_rows,
        removed_task_ids=tuple(removed), row_order_changed=order_changed,
        initiative_views_changed=initiatives_changed,
        room_rows_changed=rooms_changed,
        delta_base=delta_base,
    )


class _OneDuePass:
    """Event double: one due deadline, then a deterministic stop."""

    def __init__(self) -> None:
        self.waits = 0
        self.stopped = False

    def wait(self, _interval) -> bool:
        self.waits += 1
        return self.stopped or self.waits > 1

    def set(self) -> None:
        self.stopped = True

    def is_set(self) -> bool:
        return self.stopped


class BackgroundInputIsolationTests(unittest.TestCase):
    @staticmethod
    def _worker_result(runner):
        ready = runner.poll()
        if isinstance(ready, BaseException):
            raise ready
        return ready

    def test_blocked_real_refresh_worker_does_not_block_key_processing(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def load():
            started.set()
            release.wait()
            return _snapshot()

        runner = tui.BackgroundRefresh(load, interval=99)
        runner._stop_event = _OneDuePass()
        thread = threading.Thread(target=runner._run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=2))
        try:
            model = tui.TuiModel([
                tui._terminal_row(_terminal_task(f"key-fast-{index:03d}"))
                for index in range(200)
            ])
            model._ensure_screen()

            model.initiatives.move_selection(1)
            intent = model.dispatch_key("q")

            self.assertEqual(intent.kind, tui.IntentKind.QUIT)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(self._worker_result(runner), tui.RefreshSnapshot)

    def test_worker_assertion_is_raised_by_the_test_thread(self) -> None:
        def load():
            raise AssertionError("worker assertion propagated")

        runner = tui.BackgroundRefresh(load, interval=99)
        runner._stop_event = _OneDuePass()
        runner._run()

        with self.assertRaisesRegex(AssertionError, "worker assertion propagated"):
            self._worker_result(runner)

    def test_real_timer_path_waits_again_only_after_the_pass(self) -> None:
        events = []

        class TwoDeadlines:
            def __init__(self):
                self.results = iter((False, False, True))

            def wait(self, interval):
                events.append(("wait", interval))
                return next(self.results)

            def set(self):
                pass

        def load():
            events.append(("load", None))
            return _snapshot()

        runner = tui.BackgroundRefresh(load, interval=99)
        runner._stop_event = TwoDeadlines()
        runner._run()

        self.assertEqual(events, [
            ("wait", 99.0), ("load", None),
            ("wait", 99.0), ("load", None),
            ("wait", 99.0),
        ])


class BoundedProbeTests(unittest.TestCase):
    class Store:
        config = mock.sentinel.config

        def __init__(self, tasks) -> None:
            self.tasks = {task["task_id"]: copy.deepcopy(task) for task in tasks}
            self.skipped = []
            self.lock_count = 0

        def list(self):
            return [copy.deepcopy(task) for task in self.tasks.values()]

        @contextlib.contextmanager
        def transaction_lock(self, _task_id):
            self.lock_count += 1
            yield

        def read(self, task_id):
            return copy.deepcopy(self.tasks[task_id])

    @staticmethod
    def _inventory_line(task, index, *, dead="1", status="0"):
        run = task["runs"][0]
        return "\t".join((
            "4242", task["tmux"]["session"], f"${index}",
            task["tmux"]["window"], f"@{index}", run["pane_id"], str(run["pid"]),
            dead, status, "", "1", "1", task["task_id"], "",
            run["run_id"], "", "",
        ))

    def test_two_hundred_terminal_tasks_use_only_one_bulk_tmux_probe(self) -> None:
        tasks = [_terminal_task(f"terminal-{index:03d}") for index in range(200)]
        for index, task in enumerate(tasks):
            task["tmux"]["session"] = f"session-{index}"
            task["runs"][0]["pane_id"] = f"%{index}"
            task["runs"][0]["pid"] = 1000 + index
        store = self.Store(tasks)
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            output = "\n".join(
                self._inventory_line(task, index)
                for index, task in enumerate(tasks)
            ) + "\n"
            return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

        inventory = TmuxAdapter(runner=run).inventory()
        cache = tui.RefreshCache()

        with mock.patch.object(tui.view, "publish_server_summary"), mock.patch.object(
            tui.view, "expire_terminal_snapshots",
        ), mock.patch.object(
            tui.LiveAdapters, "event",
            return_value=Evidence("event", "missing", "no event snapshot"),
        ):
            rows = tui._load_rows(
                mock.sentinel.config, store, mock.Mock(), mock.Mock(),
                cache=cache, tmux=inventory,
            )

        self.assertEqual(len(rows), 200)
        self.assertTrue(all(row.display_state == "exited" for row in rows))
        self.assertEqual(len(calls), 1)
        self.assertIn("list-panes", calls[0])

    def test_running_task_with_only_stale_runs_is_not_terminal(self) -> None:
        task = _terminal_task("running-stale")
        task["lifecycle"] = "running"
        task["runs"][0]["state"] = "stale"
        task["runs"][0]["evidence"] = "durable ownership conflict"
        self.assertFalse(tui._task_is_terminal(task))

        with mock.patch.object(
            tui, "_read_row", return_value=tui._terminal_row(task),
        ) as read_row, mock.patch.object(tui.view, "publish_server_summary"):
            rows = tui._load_rows(
                mock.sentinel.config, self.Store([task]), mock.Mock(),
                mock.Mock(), tmux=mock.Mock(),
            )

        self.assertEqual(len(rows), 1)
        read_row.assert_called_once()

    def test_terminal_task_detects_live_process_without_tmux_or_jj_subprocess(self) -> None:
        task = _terminal_task("terminal-live-process")
        task["tmux"]["session"] = "terminal-live-process"
        task["runs"][0]["pane_id"] = "%42"
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            output = self._inventory_line(task, 42, dead="0", status="") + "\n"
            return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

        inventory = TmuxAdapter(runner=run).inventory()
        jj = mock.Mock()
        with mock.patch.object(
            tui.LiveAdapters, "event",
            return_value=Evidence("event", "missing", "no event snapshot"),
        ), mock.patch(
            "lib.control.reconcile.harness_api.verify_process", return_value=True,
        ), mock.patch.object(
            tui.view, "expire_terminal_snapshots",
        ), mock.patch.object(tui.view, "publish_server_summary"):
            rows = tui._load_rows(
                mock.sentinel.config, self.Store([task]), mock.Mock(), jj,
                cache=tui.RefreshCache(), tmux=inventory,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[0].display_state, "stale")
        self.assertEqual(
            rows[0].reconciliation["blocker"],
            "process: live process contradicts stored terminal state",
        )
        cache = tui.RefreshCache()
        cache.stabilize_rows(rows)
        self.assertIsNone(
            cache.terminal_row(task),
            "a live-process contradiction must be checked again next pass",
        )
        jj.inspect_workspace.assert_not_called()

    def test_cached_terminal_task_detects_respawned_live_process(self) -> None:
        task = _terminal_task("cached-terminal-live-process")
        task["tmux"]["session"] = "cached-terminal-live-process"
        task["runs"][0]["pane_id"] = "%42"
        calls = []

        def inventory(*, dead, status):
            def run(argv, **_kwargs):
                calls.append(argv)
                output = self._inventory_line(
                    task, 42, dead=dead, status=status,
                ) + "\n"
                return subprocess.CompletedProcess(
                    argv, 0, output.encode(), b"",
                )

            return TmuxAdapter(runner=run).inventory()

        store = self.Store([task])
        cache = tui.RefreshCache()
        with mock.patch.object(
            tui.LiveAdapters, "event",
            return_value=Evidence("event", "missing", "no event snapshot"),
        ), mock.patch(
            "lib.control.reconcile.harness_api.verify_process", return_value=True,
        ), mock.patch.object(
            tui.view, "expire_terminal_snapshots",
        ), mock.patch.object(tui.view, "publish_server_summary"):
            rows = tui._load_rows(
                mock.sentinel.config, store, mock.Mock(), mock.Mock(),
                cache=cache, tmux=inventory(dead="1", status="0"),
            )
            cache.stabilize_rows(rows)
            rows = tui._load_rows(
                mock.sentinel.config, store, mock.Mock(), mock.Mock(),
                cache=cache, tmux=inventory(dead="0", status=""),
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(store.lock_count, 2)
        self.assertEqual(rows[0].display_state, "stale")
        self.assertEqual(
            rows[0].reconciliation["blocker"],
            "process: live process contradicts stored terminal state",
        )

    def test_archived_cache_miss_does_not_lock_and_reread_records(self) -> None:
        tasks = [_terminal_task(f"archived-{index:03d}") for index in range(200)]
        for task in tasks:
            task["lifecycle"] = "archived"
        store = self.Store(tasks)
        cache = tui.RefreshCache()
        cache.begin_generation(1)

        with mock.patch.object(tui.view, "publish_server_summary"):
            rows = tui._load_rows(
                mock.sentinel.config, store, mock.Mock(), mock.Mock(),
                include_archived=True, cache=cache, tmux=mock.Mock(),
            )

        self.assertEqual(len(rows), 200)
        self.assertEqual(store.lock_count, 0)

    def test_hundred_tmux_sessions_use_one_inventory_subprocess(self) -> None:
        calls = []
        lines = []
        for index in range(100):
            fields = (
                "4242", f"session-{index}", f"${index}", "work", f"@{index}",
                f"%{index}", str(1000 + index), "0", "", "", "1",
                "1", f"task-{index}", "",
                f"run-{index}", "", "",
            )
            lines.append("\t".join(fields))

        def run(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, ("\n".join(lines) + "\n").encode(), b"",
            )

        inventory = TmuxAdapter(runner=run).inventory()
        for index in range(100):
            self.assertTrue(inventory.has_session(f"session-{index}"))
            self.assertEqual(
                inventory.session_option(f"session-{index}", "@asha_managed"),
                "1",
            )
            self.assertEqual(
                inventory.pane_option(f"%{index}", "@asha_run_id"),
                f"run-{index}",
            )
            self.assertEqual(inventory.pane_facts(f"%{index}").pane_pid, 1000 + index)

        self.assertEqual(len(calls), 1)
        self.assertIn("list-panes", calls[0])
        with self.assertRaisesRegex(TmuxError, "not present"):
            inventory.session_option("session-0", "@asha_unknown")
        with self.assertRaisesRegex(TmuxError, "not present"):
            inventory.pane_option("%0", "@asha_unknown")

    def test_inventory_accepts_same_named_windows_by_window_identity(self) -> None:
        lines = []
        for window_id, pane_id in (("@1", "%1"), ("@2", "%2")):
            fields = (
                "4242", "shared", "$1", "work", window_id, pane_id,
                "1001", "0", "", "", "1", "1", "task-shared", "",
                f"run-{pane_id[1:]}", "", "",
            )
            lines.append("\t".join(fields))

        def run(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv, 0, ("\n".join(lines) + "\n").encode(), b"",
            )

        inventory = TmuxAdapter(runner=run).inventory()

        self.assertEqual(inventory.pane_facts("%1").window, "work")
        self.assertEqual(inventory.pane_facts("%2").window, "work")
        self.assertIn(
            inventory.window_pane_facts("shared", "work").pane_id,
            {"%1", "%2"},
        )

    def test_inventory_and_session_list_skip_foreign_names(self) -> None:
        def line(session, window, window_id, pane_id):
            return "\t".join((
                "4242", session, "$1", window, window_id, pane_id,
                "1001", "0", "", "", "1", "1", "task-valid", "",
                "run-valid", "", "",
            ))

        inventory_output = "\n".join((
            line("my project", "work", "@1", "%1"),
            line("foreign", "my window", "@2", "%2"),
            line("valid", "work", "@3", "%3"),
        )) + "\n"

        def run(argv, **_kwargs):
            if "list-sessions" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, b"my project\nvalid\n", b"",
                )
            return subprocess.CompletedProcess(
                argv, 0, inventory_output.encode(), b"",
            )

        adapter = TmuxAdapter(runner=run)
        inventory = adapter.inventory()

        self.assertEqual(inventory.list_sessions(), ["valid"])
        self.assertEqual(adapter.list_sessions(), ["valid"])
        self.assertEqual(inventory.pane_facts("%3").session, "valid")
        for pane_id in ("%1", "%2"):
            with self.assertRaisesRegex(TmuxError, "can't find pane"):
                inventory.pane_facts(pane_id)

    def test_terminal_rows_run_durable_maintenance_without_live_probes(self) -> None:
        task = _terminal_task("terminal-maintenance")
        task["tmux"]["session"] = "terminal-maintenance"
        task["runs"][0]["pane_id"] = "%42"
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            output = self._inventory_line(task, 42) + "\n"
            return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

        inventory = TmuxAdapter(runner=run).inventory()
        store = self.Store([task])
        jj = mock.Mock()
        with mock.patch.object(
            tui.LiveAdapters, "event",
            return_value=Evidence("event", "missing", "no event snapshot"),
        ), mock.patch.object(
            tui.view, "persist_terminal_reconciliation", return_value=task,
        ) as persist, mock.patch.object(
            tui.view, "expire_terminal_snapshots",
        ) as expire, mock.patch.object(tui.view, "publish_server_summary"):
            rows = tui._load_rows(
                mock.sentinel.config, store, mock.Mock(), jj,
                cache=tui.RefreshCache(), tmux=inventory,
            )

        self.assertEqual(len(rows), 1)
        persist.assert_called_once()
        expire.assert_called_once()
        jj.inspect_workspace.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_refresh_uses_each_tasks_recorded_tmux_socket_inventory(self) -> None:
        default_task = task_record(slug="default-socket")
        named_task = task_record(slug="named-socket")
        named_task["tmux"]["socket"] = "alternate"
        default_inventory = mock.sentinel.default_inventory
        named_inventory = mock.sentinel.named_inventory
        source = mock.Mock()
        source.inventory.return_value = default_inventory
        named_source = mock.Mock()
        named_source.inventory.return_value = named_inventory
        private_store = SimpleNamespace(skipped=[])

        def load_rows(*_args, **kwargs):
            select = kwargs["tmux_for_task"]
            self.assertIs(select(default_task), default_inventory)
            self.assertIs(select(named_task), named_inventory)
            return []

        with mock.patch.object(tui, "TaskStore", return_value=private_store), \
                mock.patch.object(tui, "CreationJournalStore"), \
                mock.patch.object(tui, "JjAdapter"), \
                mock.patch.object(tui, "_load_rows", side_effect=load_rows), \
                mock.patch.object(tui, "_load_initiative_views", return_value=[]), \
                mock.patch.object(tui, "_load_room_rows", return_value=[]), \
                mock.patch.object(tui, "TmuxAdapter", return_value=named_source) as adapter:
            tui._load_refresh_snapshot(
                mock.sentinel.config, {}, cache=tui.RefreshCache(), tmux=source,
            )

        adapter.assert_called_once_with(socket="alternate")

    def test_refresh_degrades_all_branches_when_inventory_fails(self) -> None:
        row = tui._terminal_row(_terminal_task("inventory-fallback"))
        source = mock.Mock()
        source.inventory.side_effect = TmuxError("malformed inventory")
        private_store = SimpleNamespace(skipped=[])

        with mock.patch.object(tui, "TaskStore", return_value=private_store), \
                mock.patch.object(tui, "CreationJournalStore"), \
                mock.patch.object(tui, "JjAdapter"), \
                mock.patch.object(tui, "_load_rows", return_value=[row]) as load_rows, \
                mock.patch.object(
                    tui, "_load_initiative_views", return_value=[{"state": "running"}],
                ) as load_initiatives, mock.patch.object(
                    tui, "_load_room_rows", return_value=[{"state": "open"}],
                ) as load_rooms:
            snapshot = tui._load_refresh_snapshot(
                mock.sentinel.config, {}, cache=tui.RefreshCache(), tmux=source,
            )

        self.assertEqual(snapshot.rows, (row,))
        self.assertEqual(snapshot.initiative_views, ({"state": "running"},))
        self.assertEqual(snapshot.room_rows, ({"state": "open"},))
        self.assertIs(load_rows.call_args.kwargs["tmux"], source)
        self.assertIs(
            load_rows.call_args.kwargs["tmux_for_task"](row.task), source,
        )
        self.assertIs(load_initiatives.call_args.kwargs["tmux"], source)
        self.assertIs(load_rooms.call_args.kwargs["tmux"], source)


class IncrementalSnapshotTests(unittest.TestCase):
    @staticmethod
    def _initiative(state):
        return {
            "initiative": {
                "initiative_id": "11111111-1111-4111-8111-111111111111",
                "slug": "refresh-delta", "label": "Refresh delta",
                "state": state,
                "limits": {"max_parallel": 1, "max_total_tasks": 1},
            },
            "plan": None, "nodes": [], "attempts": [], "links": [],
            "events": [], "coordinator": None, "coordinator_live": False,
            "seals": [], "reviews": [], "verifications": [],
            "approvals": [], "storage": None,
        }

    @staticmethod
    def _cached(rows):
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        stable, changed, removed, order_changed = cache.stabilize_rows(rows)
        cache.stabilize_branch("initiatives", (), None)
        cache.stabilize_branch("rooms", (), None)
        cache.mark_applied(_snapshot(
            stable, changed_rows=changed, removed=removed,
            order_changed=order_changed,
        ))
        return cache, stable, changed, removed, order_changed

    def test_dropped_pass_changes_are_redelivered_from_applied_baseline(self) -> None:
        original = tui._terminal_row(_terminal_task("dropped-delta"))
        initial_view = self._initiative("running")
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        initial_rows, changed, removed, order_changed = cache.stabilize_rows([
            original,
        ])
        initial_views, initiatives_changed = cache.stabilize_branch(
            "initiatives", (initial_view,), None,
        )
        rooms, rooms_changed = cache.stabilize_branch("rooms", (), None)
        initial = _snapshot(
            initial_rows, changed_rows=changed, removed=removed,
            order_changed=order_changed, initiatives=initial_views, rooms=rooms,
            initiatives_changed=initiatives_changed,
            rooms_changed=rooms_changed,
        )
        model = tui.TuiModel(())
        tui._apply_refresh_snapshot(model, {}, initial)
        cache.mark_applied(initial)

        changed_task = copy.deepcopy(original.task)
        changed_task["updated_at"] = "2026-08-14T18:00:03Z"
        changed_view = self._initiative("paused")
        # This completed pass is deliberately never applied or acknowledged.
        cache.stabilize_rows([tui._terminal_row(changed_task)])
        cache.stabilize_branch("initiatives", (changed_view,), None)
        cache.stabilize_branch("rooms", (), None)

        rows, changed, removed, order_changed = cache.stabilize_rows([
            tui._terminal_row(copy.deepcopy(changed_task)),
        ])
        views, initiatives_changed = cache.stabilize_branch(
            "initiatives", (copy.deepcopy(changed_view),), None,
        )
        rooms, rooms_changed = cache.stabilize_branch("rooms", (), None)
        replacement = _snapshot(
            rows, changed_rows=changed, removed=removed,
            order_changed=order_changed, initiatives=views, rooms=rooms,
            initiatives_changed=initiatives_changed,
            rooms_changed=rooms_changed,
        )

        self.assertEqual([row.task["task_id"] for row in changed], [
            original.task["task_id"],
        ])
        self.assertTrue(initiatives_changed)
        tui._apply_refresh_snapshot(model, {}, replacement)
        cache.mark_applied(replacement)
        self.assertEqual(model.rows[0].task["updated_at"], "2026-08-14T18:00:03Z")
        self.assertEqual(
            model.initiatives.views[0]["initiative"]["state"], "paused",
        )

    def test_late_acknowledgement_forces_safe_full_handoff(self) -> None:
        original = tui._terminal_row(_terminal_task("late-ack"))
        changed_task = copy.deepcopy(original.task)
        changed_task["updated_at"] = "2026-08-14T18:00:03Z"
        changed = tui._terminal_row(changed_task)
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        rows, delta, removed, ordered = cache.stabilize_rows([original])
        initial = _snapshot(rows, changed_rows=delta, removed=removed,
                            order_changed=ordered)
        cache.mark_applied(initial)
        model = tui.TuiModel((original,))
        model._ensure_screen()

        base = cache.delta_base()
        rows, delta, removed, ordered = cache.stabilize_rows([changed])
        later_applied = _snapshot(
            rows, changed_rows=delta, removed=removed,
            order_changed=ordered, delta_base=base,
        )
        # A following load reverts to the original while the UI is still on
        # the initial baseline, so its original delta is legitimately empty.
        rows, delta, removed, ordered = cache.stabilize_rows([original])
        following = _snapshot(
            rows, changed_rows=delta, removed=removed,
            order_changed=ordered, initiatives_changed=False,
            rooms_changed=False, delta_base=base,
        )
        self.assertEqual(delta, ())

        tui._apply_refresh_snapshot(model, {}, later_applied)
        cache.mark_applied(later_applied)
        rebased = cache.prepare_to_apply(following)
        self.assertEqual(rebased.changed_rows, rebased.rows)
        self.assertTrue(rebased.initiative_views_changed)
        tui._apply_refresh_snapshot(model, {}, rebased)
        cache.mark_applied(rebased)
        self.assertEqual(model.rows[0].task["updated_at"], original.task["updated_at"])

    def test_unchanged_snapshot_does_not_repaint_or_rebuild_rows(self) -> None:
        original = tui._terminal_row(_terminal_task("unchanged"))
        cache, stable, _changed, _removed, _order = self._cached([original])
        model = tui.TuiModel(())
        model._ensure_screen()
        tui._apply_refresh_snapshot(
            model, {}, _snapshot(stable, changed_rows=stable),
        )
        model.dirty = False
        same_rows, changed, removed, order_changed = cache.stabilize_rows([
            tui._terminal_row(copy.deepcopy(original.task)),
        ])
        _, initiatives_changed = cache.stabilize_branch("initiatives", (), None)
        _, rooms_changed = cache.stabilize_branch("rooms", (), None)

        with mock.patch.object(tui, "_refresh_initiatives") as refresh:
            applied = tui._apply_refresh_snapshot(
                model, {}, _snapshot(
                    same_rows, changed_rows=changed, removed=removed,
                    order_changed=order_changed,
                    initiatives_changed=initiatives_changed,
                    rooms_changed=rooms_changed,
                ),
            )

        self.assertFalse(applied)
        self.assertFalse(model.dirty)
        self.assertIs(model.rows[0], stable[0])
        refresh.assert_not_called()

    def test_one_row_change_reuses_every_other_visible_row(self) -> None:
        first = tui._terminal_row(_terminal_task("first"))
        second = tui._terminal_row(_terminal_task("second"))
        cache, stable, _changed, _removed, _order = self._cached([first, second])
        model = tui.TuiModel(())
        model._ensure_screen()
        tui._apply_refresh_snapshot(
            model, {}, _snapshot(stable, changed_rows=stable),
        )
        before = {row.task["task_id"]: row for row in model.rows}
        visible_before = {
            row.task_id: row for row in model.initiatives.rows()
            if row.task_id is not None
        }
        changed_task = copy.deepcopy(second.task)
        changed_task["updated_at"] = "2026-08-14T18:00:02Z"
        unchanged = cache.terminal_row(first.task)
        self.assertIsNotNone(unchanged)
        rows, changed, removed, order_changed = cache.stabilize_rows([
            unchanged, tui._terminal_row(changed_task),
        ])
        _, initiatives_changed = cache.stabilize_branch("initiatives", (), None)
        _, rooms_changed = cache.stabilize_branch("rooms", (), None)

        applied = tui._apply_refresh_snapshot(
            model, {}, _snapshot(
                rows, changed_rows=changed, removed=removed,
                order_changed=order_changed,
                initiatives_changed=initiatives_changed,
                rooms_changed=rooms_changed,
            ),
        )
        after = {row.task["task_id"]: row for row in model.rows}
        visible_after = {
            row.task_id: row for row in model.initiatives.rows()
            if row.task_id is not None
        }

        self.assertTrue(applied)
        self.assertEqual([row.task["task_id"] for row in changed], [second.task["task_id"]])
        self.assertIs(after[first.task["task_id"]], before[first.task["task_id"]])
        self.assertIsNot(after[second.task["task_id"]], before[second.task["task_id"]])
        self.assertIs(
            visible_after[first.task["task_id"]],
            visible_before[first.task["task_id"]],
        )
        self.assertIsNot(
            visible_after[second.task["task_id"]],
            visible_before[second.task["task_id"]],
        )

    def test_unchanged_data_repaints_only_when_displayed_age_changes(self) -> None:
        task = _terminal_task("age-advances")
        task["runs"][0]["evidence_at"] = "2026-08-14T18:00:00Z"
        row = tui._terminal_row(task)
        model = tui.TuiModel(
            [row], now=datetime(2026, 8, 14, 18, 0, 0, tzinfo=timezone.utc),
        )
        model._clock_pinned = False
        model._ensure_screen()
        model.dirty = False
        unchanged = _snapshot(
            (row,), changed_rows=(), removed=(), order_changed=False,
            initiatives_changed=False, rooms_changed=False,
        )

        five_seconds_later = model.now + timedelta(seconds=5)
        with mock.patch.object(tui, "_utc_now", return_value=five_seconds_later):
            applied = tui._apply_refresh_snapshot(model, {}, unchanged)

        self.assertFalse(applied)
        self.assertTrue(model.dirty)
        self.assertEqual(tui._age(row.observation.observed_at, model.now), "5s")

        model.dirty = False
        with mock.patch.object(tui, "_utc_now", return_value=five_seconds_later):
            tui._apply_refresh_snapshot(model, {}, unchanged)
        self.assertFalse(model.dirty)



class ParkedWorkIncrementalPatchTests(unittest.TestCase):
    """The task-only cache patch and a full rebuild agree about parked demand.

    A paused initiative parks its node decision. A worker beneath it that
    reaches a real prompt must still surface through the incremental patch, and
    when the prompt is answered the patch must not restore the parked decision
    as a stale `needs input`. Resume changes the initiative branch, so the tree
    rebuilds and the decision is re-exposed from its own record.
    """

    INITIATIVE = "11111111-1111-4111-8111-111111111111"
    TASK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    ATTEMPT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self) -> None:
        self.task = task_record(task_id=self.TASK, slug="parked-worker")
        self.run_id = self.task["runs"][0]["run_id"]

    def view(self, state: str) -> dict:
        view = IncrementalSnapshotTests._initiative(state)
        view["nodes"] = [{
            "node_id": "implementation-a", "state": "needs-input", "type": "work",
            "goal": "Implement A",
        }]
        view["attempts"] = [{
            "attempt_id": self.ATTEMPT, "node_id": "implementation-a",
            "ordinal": 1, "state": "running",
        }]
        view["links"] = [{"attempt_id": self.ATTEMPT, "control_task_id": self.TASK}]
        return view

    def worker(self, state: str, detail: str, observed_at: str) -> tui.TuiRow:
        reconciliation = {
            "contract": "asha.control-reconciliation.v1", "task_id": self.TASK,
            "state": state, "blocker": None,
            "evidence": [{"state": state, "detail": detail}],
            "runs": [{
                "contract": "asha.control-run-reconciliation.v1",
                "run_id": self.run_id, "state": "active", "blocker": None, "evidence": [],
            }],
        }
        observation = StateObservation(state, self.run_id, "events", observed_at, "fresh", detail)
        return tui.TuiRow.from_records(copy.deepcopy(self.task), reconciliation, observation)

    def snapshot(self, cache: tui.RefreshCache, worker: tui.TuiRow, state: str) -> tui.RefreshSnapshot:
        rows, changed, removed, order_changed = cache.stabilize_rows([worker])
        views, views_changed = cache.stabilize_branch("initiatives", (self.view(state),), None)
        rooms, rooms_changed = cache.stabilize_branch("rooms", (), None)
        return _snapshot(
            rows, changed_rows=changed, removed=removed, order_changed=order_changed,
            initiatives=views, rooms=rooms, initiatives_changed=views_changed,
            rooms_changed=rooms_changed,
        )

    @staticmethod
    def facts(screen: InitiativesScreen) -> list:
        return [(row.key, row.state, row.attention, row.worker) for row in screen.rows()]

    @staticmethod
    def row(model: tui.TuiModel, kind: str):
        return next(row for row in model.initiatives.rows() if row.kind == kind)

    @staticmethod
    def full_rebuild(model: tui.TuiModel) -> InitiativesScreen:
        screen = model.initiatives
        return InitiativesScreen(
            screen.views, height=screen.height, width=screen.width,
            expanded=set(screen.expanded), task_rows=model.rows,
        )

    def test_a_live_prompt_reaches_a_parked_node_and_a_stale_one_never_returns(self) -> None:
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        first = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:05Z"), "paused")
        tui._apply_refresh_snapshot(model, {}, first)
        cache.mark_applied(first)
        model.initiatives.expanded.add(("initiative", self.INITIATIVE))
        self.assertEqual(
            (self.row(model, "initiative").attention, self.row(model, "node").attention), ("-", "-"),
        )
        self.assertEqual(self.row(model, "node").state, "needs-input")
        head_before = self.row(model, "initiative")

        # The worker reaches a prompt: only the task row changed.
        prompt = self.snapshot(
            cache, self.worker("needs-input", "pane shows the input prompt", "2026-08-14T18:00:06Z"), "paused",
        )
        self.assertFalse(prompt.initiative_views_changed)
        self.assertFalse(prompt.row_order_changed)
        self.assertEqual([row.task["task_id"] for row in prompt.changed_rows], [self.TASK])
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, prompt))
        cache.mark_applied(prompt)
        self.assertIs(self.row(model, "initiative"), head_before, "the patch reused the untouched head row")
        node = self.row(model, "node")
        self.assertEqual((node.attention, node.worker), ("at prompt: pane shows the input prompt", "needs-input"))
        self.assertTrue(node.needs_human)
        self.assertEqual(self.facts(model.initiatives), self.facts(self.full_rebuild(model)))
        self.assertEqual(
            [item["kind"] for item in attention_items(model.initiatives.views, model.rows)], ["worker"],
        )

        # The prompt is answered: the parked decision must not come back as a stale needs-input.
        answered = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:07Z"), "paused")
        self.assertFalse(answered.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, answered))
        cache.mark_applied(answered)
        self.assertIs(self.row(model, "initiative"), head_before)
        node = self.row(model, "node")
        self.assertEqual((node.attention, node.worker, node.needs_human), ("-", "idle", False))
        self.assertEqual(self.facts(model.initiatives), self.facts(self.full_rebuild(model)))
        self.assertEqual(attention_items(model.initiatives.views, model.rows), [])

        # Resume is an initiative change: the branch rebuilds and the decision is re-exposed.
        resumed = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:07Z"), "running")
        self.assertTrue(resumed.initiative_views_changed)
        self.assertEqual(resumed.changed_rows, ())
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, resumed))
        cache.mark_applied(resumed)
        node = self.row(model, "node")
        self.assertEqual((node.attention, node.worker, node.needs_human), ("needs input", "idle", True))
        self.assertEqual(self.facts(model.initiatives), self.facts(self.full_rebuild(model)))
        self.assertEqual(
            [item["kind"] for item in attention_items(model.initiatives.views, model.rows)], ["needs-input"],
        )

    def test_a_live_prompt_beneath_a_collapsed_parked_head_is_listed_under_the_filter(self) -> None:
        """Collapse is not parking: `!` lists the prompt row, full and patched refreshes agree."""
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        first = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:05Z"), "paused")
        tui._apply_refresh_snapshot(model, {}, first)
        cache.mark_applied(first)
        screen = model.initiatives
        self.assertNotIn(("initiative", self.INITIATIVE), screen.expanded)
        self.assertEqual([row.kind for row in screen.rows()], ["initiative"])
        head_before = self.row(model, "initiative")

        def filtered_rebuild() -> InitiativesScreen:
            return InitiativesScreen(
                screen.views, height=screen.height, width=screen.width,
                expanded=set(screen.expanded), task_rows=model.rows, attention_only=True,
            )

        # The worker reaches a prompt through the task-only patch while the
        # head stays collapsed: the normal tree is unchanged and quiet.
        prompt = self.snapshot(
            cache, self.worker("needs-input", "pane shows the input prompt", "2026-08-14T18:00:06Z"), "paused",
        )
        self.assertFalse(prompt.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, prompt))
        cache.mark_applied(prompt)
        self.assertIs(self.row(model, "initiative"), head_before)
        self.assertEqual([row.kind for row in screen.rows()], ["initiative"])
        self.assertEqual(self.facts(screen), self.facts(self.full_rebuild(model)))
        self.assertEqual(
            [item["kind"] for item in attention_items(screen.views, model.rows)], ["worker"],
        )

        # `!` reveals the prompt row with its still-collapsed head, exactly as
        # a full rebuild under the filter would.
        screen.attention_only = True
        self.assertNotIn(("initiative", self.INITIATIVE), screen.expanded)
        self.assertEqual(
            [(row.kind, row.depth, row.attention) for row in screen.rows()],
            [("initiative", 0, "-"), ("node", 1, "at prompt: pane shows the input prompt")],
        )
        self.assertEqual(self.facts(screen), self.facts(filtered_rebuild()))
        self.assertEqual(tui.summary_counts(screen.rows()), tui.summary_counts(filtered_rebuild().rows()))
        self.assertEqual(tui.summary_counts(screen.rows())["paused"], 1)
        self.assertEqual(tui.summary_counts(screen.rows())["waiting"], 0)

        # Answered while filtered: the parked head has nothing live beneath it
        # and leaves; the refresh under `!` is a rebuild and agrees with one.
        answered = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:07Z"), "paused")
        self.assertFalse(answered.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, answered))
        cache.mark_applied(answered)
        self.assertEqual(screen.rows(), [])
        self.assertEqual(self.facts(screen), self.facts(filtered_rebuild()))
        self.assertEqual(attention_items(screen.views, model.rows), [])

        # Back in the normal tree the head is still collapsed and readable.
        screen.attention_only = False
        self.assertEqual([(row.kind, row.attention) for row in screen.rows()], [("initiative", "-")])
        self.assertEqual(self.facts(screen), self.facts(self.full_rebuild(model)))

        # Resumed and collapsed, the re-exposed decision is listed under `!`
        # with its head; the normal tree still shows only the head.
        resumed = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:07Z"), "running")
        self.assertTrue(resumed.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, resumed))
        cache.mark_applied(resumed)
        self.assertEqual([(row.kind, row.attention) for row in screen.rows()], [("initiative", "-")])
        screen.attention_only = True
        self.assertEqual(
            [(row.kind, row.depth, row.attention) for row in screen.rows()],
            [("initiative", 0, "-"), ("node", 1, "needs input")],
        )
        self.assertEqual(self.facts(screen), self.facts(filtered_rebuild()))
        self.assertEqual(
            [item["kind"] for item in attention_items(screen.views, model.rows)], ["needs-input"],
        )




class BoundedRetainedReadTests(ExecutionFixture, unittest.TestCase):
    """The one observation is bounded in every dimension, and says what it missed.

    The caps are the store's presentation contract, shared by the native tree
    and by `asha initiative attention`: there is no second enumeration and no
    unbounded fallback. Every deadline here is cooperative -- it is checked
    between directory entries, so one blocking filesystem syscall inside a
    single record read is not preempted by it, and nothing below claims a
    syscall-level guarantee. What is asserted is the reported completeness.
    """

    def archive_head(self) -> str:
        current = self.initiative()
        for state in ("running", "ready-for-integration", "integrated", "archived"):
            if current["state"] == state:
                continue
            changed = copy.deepcopy(current)
            changed.update({
                "state": state, "state_revision": current["state_revision"] + 1,
                "updated_at": tui._utc_now().isoformat(
                    timespec="microseconds",
                ).replace("+00:00", "Z"),
            })
            self.store.save_initiative(changed, expected_digest=record_digest(current))
            current = changed
        return current["initiative_id"]

    def test_heads_already_read_survive_a_deadline_before_graph_loading(self) -> None:
        budget = tui_store.PresentationBudget(deadline_seconds=30.0)
        def heads(store, shared):
            records = [self.initiative()]
            shared.deadline = -1.0
            return records
        with mock.patch.object(tui_store.InitiativeStore, "bounded_head_snapshots", heads), \
                mock.patch.object(tui_store.InitiativeStore, "bounded_presentation_records",
                                  side_effect=AssertionError("graph read after deadline")):
            views = tui._load_initiative_views(self.env, budget=budget)
        self.assertEqual([v["initiative"]["initiative_id"] for v in views], [self.initiative_id])
        self.assertTrue(views[0]["_graph_unavailable"])
        self.assertFalse(views[0]["_completeness"]["complete"])

    def test_missing_active_plan_is_incomplete_even_when_all_reads_succeeded(self) -> None:
        budget = tui_store.PresentationBudget(deadline_seconds=30.0)
        real_read = tui_store.InitiativeStore.bounded_presentation_records
        def omit_plan(store, iid, directory, shared, **kwargs):
            return [] if directory == "plans" else real_read(store, iid, directory, shared, **kwargs)
        with mock.patch.object(tui_store.InitiativeStore, "bounded_presentation_records", omit_plan):
            views = tui._load_initiative_views(self.env, budget=budget)
        self.assertFalse(views[0]["_graph_complete"])
        self.assertEqual(budget.unavailable, 0)
        self.assertFalse(tui.observation_completeness(views, budget)["complete"])

    def test_task_cap_is_reported_after_the_head_summary_was_captured(self) -> None:
        budget = tui_store.PresentationBudget(task_limit=1, deadline_seconds=30.0)
        views = tui._load_initiative_views(self.env, budget=budget)
        self.assertTrue(views[0]["_completeness"]["complete"])
        self.assertEqual(budget.admit_tasks(["first", "second"]), ["first"])
        report = tui.observation_completeness(views, budget)
        self.assertFalse(report["complete"])
        self.assertIn("tasks", report["caps_reached"])
        self.assertEqual(report["scanned"]["tasks"], 2)

    def test_the_caps_are_the_store_presentation_contract(self) -> None:
        # The loader keeps literals so this module can load orchestration
        # lazily; the numbers themselves have exactly one source of truth.
        self.assertEqual(tui._RETAINED_HEAD_LIMIT, tui_store.PRESENTATION_HEAD_LIMIT)
        self.assertEqual(tui._RETAINED_PER_HEAD_LIMIT, tui_store.PRESENTATION_PER_HEAD_LIMIT)
        self.assertEqual(tui._RETAINED_NESTED_LIMIT, tui_store.PRESENTATION_NESTED_LIMIT)
        self.assertEqual(tui._RETAINED_TASK_LIMIT, tui_store.PRESENTATION_TASK_LIMIT)
        self.assertEqual(tui._RETAINED_EVENT_TAIL, tui_store.PRESENTATION_EVENT_TAIL)
        self.assertEqual(
            tui._RETAINED_DEADLINE_SECONDS, tui_store.PRESENTATION_DEADLINE_SECONDS,
        )
        self.assertEqual(
            (tui._RETAINED_HEAD_LIMIT, tui._RETAINED_TASK_LIMIT,
             tui._RETAINED_NESTED_LIMIT, tui._RETAINED_PER_HEAD_LIMIT,
             tui._RETAINED_EVENT_TAIL, tui._RETAINED_DEADLINE_SECONDS),
            (256, 512, 8192, 512, 50, 2.0),
        )

    def test_the_read_reports_its_own_completeness(self) -> None:
        views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        completeness = views[0]["_completeness"]
        self.assertEqual(completeness["head_limit"], tui._RETAINED_HEAD_LIMIT)
        self.assertEqual(completeness["deadline_seconds"], tui._RETAINED_DEADLINE_SECONDS)
        self.assertTrue(completeness["complete"])
        self.assertFalse(completeness["truncated"])
        self.assertFalse(completeness["deadline_exceeded"])
        self.assertEqual(completeness["caps_reached"], [])
        self.assertEqual(completeness["unavailable_records"], 0)
        self.assertEqual(completeness["failures"], [])
        self.assertEqual(completeness["graphs_loaded"], 1)
        self.assertEqual(completeness["graphs_unavailable"], 0)
        self.assertEqual(completeness["archived_head_metadata"], 0)
        self.assertEqual(completeness["limits"], {
            "heads": 256, "per_head": 512, "nested": 8192, "tasks": 512,
            "event_tail": 50, "deadline_seconds": 2.0,
        })

    def test_one_shared_loader_serves_the_tree_and_the_public_verb(self) -> None:
        """No second enumeration exists: both callers read the same bounded pass."""
        self.assertNotIn(
            "list_initiatives",
            inspect.getsource(tui._load_initiative_views),
            "the loader must not keep an unbounded registry fallback",
        )
        with mock.patch.object(
            tui_store.InitiativeStore, "list_initiatives",
            side_effect=AssertionError("the bounded loader must not lock the registry"),
        ), mock.patch.object(
            tui_store.InitiativeStore, "list_nodes_snapshot",
            side_effect=AssertionError("the bounded loader must not use strict listings"),
        ), mock.patch.object(
            tui_store.InitiativeStore, "list_events_snapshot",
            side_effect=AssertionError("the bounded loader must not use strict listings"),
        ):
            views = tui._load_initiative_views(self.env, tmux=mock.Mock())
            with mock.patch(
                "lib.control.cli._load_rows_for_attention", return_value=(),
            ):
                payload = cli._attention_payload(self.env)
        self.assertEqual(len(views), 1)
        self.assertEqual(payload["contract"], cli.ATTENTION_CONTRACT)
        self.assertEqual(payload["observation"]["heads_loaded"], 1)

    def test_an_archived_head_is_metadata_and_never_an_expanded_graph(self) -> None:
        initiative_id = self.archive_head()
        with mock.patch.object(
            tui_store.InitiativeStore, "bounded_presentation_records",
            side_effect=AssertionError("archived graph must not be read"),
        ), mock.patch.object(
            tui_store.InitiativeStore, "bounded_event_sample",
            side_effect=AssertionError("archived graph must not be read"),
        ):
            views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual([view["initiative"]["initiative_id"] for view in views],
                         [initiative_id])
        archived = views[0]
        self.assertTrue(archived["_metadata_only"])
        self.assertEqual(archived["initiative"]["state"], "archived")
        for empty in ("nodes", "attempts", "links", "events", "actions", "seals",
                      "approvals", "reviews", "verifications"):
            self.assertEqual(archived[empty], [], empty)
        self.assertEqual(archived["_completeness"]["archived_head_metadata"], 1)
        self.assertEqual(archived["_completeness"]["graphs_loaded"], 0)
        # An unread graph is unknown, not zero, and reading it is still
        # complete: nothing was skipped, it was deliberately never opened.
        screen = InitiativesScreen(views, height=28, width=122, view_scope="all")
        head = next(row for row in screen.rows() if row.kind == "initiative")
        self.assertEqual(head.nodes, "?")
        self.assertTrue(archived["_completeness"]["complete"])

    def test_a_short_read_marks_the_counts_partial_instead_of_implying_a_total(self) -> None:
        with mock.patch.object(tui, "_RETAINED_HEAD_LIMIT", 0):
            views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual(views, [], "nothing loads inside a zero-head budget")
        # One head loaded under a budget that stops before the rest is partial,
        # and the title says so rather than presenting a short list as a total.
        loaded = tui._load_initiative_views(self.env, tmux=mock.Mock())
        loaded[0]["_completeness"] = dict(
            loaded[0]["_completeness"], truncated=True, complete=False,
        )
        screen = InitiativesScreen(loaded, height=28, width=122)
        self.assertTrue(screen.retained_counts["partial"])
        model = tui.TuiModel(height=28, width=122)
        model.initiatives = screen
        title = str(tui.render(model)[0])
        self.assertIn("Heads 1/1", title)
        self.assertIn("partial", title)

    def test_the_head_cap_stops_the_pass_without_discarding_what_it_read(self) -> None:
        """Reaching the cap is truncation to report, never work to throw away."""
        budget = tui_store.PresentationBudget(head_limit=1, deadline_seconds=30.0)
        views = tui._load_initiative_views(self.env, tmux=mock.Mock(), budget=budget)
        self.assertEqual(len(views), 1)
        summary = budget.summary()
        self.assertEqual(summary["scanned"]["heads"], 1)
        self.assertTrue(summary["truncated"])
        self.assertIn("heads", summary["caps_reached"])
        self.assertFalse(summary["deadline_exceeded"])
        self.assertFalse(summary["complete"])
        self.assertEqual(views[0]["_completeness"]["graphs_loaded"], 1)
        # The report names the budget that was spent, not the module default.
        self.assertEqual(views[0]["_completeness"]["head_limit"], 1)
        self.assertEqual(views[0]["_completeness"]["deadline_seconds"], 30.0)

    def test_the_cooperative_deadline_truncates_and_reports_instead_of_lying(self) -> None:
        # A deadline already spent stops the read between records. Nothing here
        # claims a blocking syscall inside one record read is preempted.
        with mock.patch.object(tui, "_RETAINED_DEADLINE_SECONDS", -1.0):
            views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual(views, [])
        # The same spent deadline is reported, not hidden, to a caller that
        # owns the budget and gets no view back to read it from.
        budget = tui_store.PresentationBudget(deadline_seconds=-1.0)
        self.assertEqual(
            tui._load_initiative_views(self.env, tmux=mock.Mock(), budget=budget), [],
        )
        summary = tui.observation_completeness([], budget)
        self.assertTrue(summary["truncated"])
        self.assertTrue(summary["deadline_exceeded"])
        self.assertFalse(summary["complete"])

    def test_an_unreadable_record_is_counted_not_silently_dropped(self) -> None:
        (self.config.initiatives_dir / "not-a-uuid").mkdir()
        views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        completeness = views[0]["_completeness"]
        self.assertEqual(completeness["unavailable_records"], 1)
        self.assertFalse(completeness["complete"])
        screen = InitiativesScreen(views, height=28, width=122)
        self.assertTrue(screen.retained_counts["partial"])
        self.assertEqual(
            [row.label for row in screen.rows() if row.kind == "initiative"],
            ["execution-test"],
            "a record that could not be read never removes one that could",
        )
        self.assertFalse(
            screen.binding_complete,
            "an incomplete read cannot prove which tasks are unbound",
        )

    def test_one_malformed_subrecord_keeps_the_head_and_its_readable_rows(self) -> None:
        """The accepted finding: a bad nested record must degrade, not blank the tree."""
        clean = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual(len(clean), 1)
        self.assertTrue(clean[0]["_completeness"]["complete"])
        readable_nodes = sorted(node["node_id"] for node in clean[0]["nodes"])
        self.assertTrue(readable_nodes)
        attempts_dir = self.config.initiatives_dir / self.initiative_id / "attempts"
        corrupt = attempts_dir / "11111111-2222-4333-8444-555555555555.json"
        corrupt.write_text("{not json")
        views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual(len(views), 1, "the head survives its damaged subrecord")
        completeness = views[0]["_completeness"]
        self.assertGreaterEqual(completeness["unavailable_records"], 1)
        self.assertFalse(completeness["complete"])
        self.assertTrue(any(
            "attempts" in failure["scope"] for failure in completeness["failures"]
        ), completeness["failures"])
        self.assertEqual(
            sorted(node["node_id"] for node in views[0]["nodes"]), readable_nodes,
            "the records that read back as themselves are kept",
        )
        screen = InitiativesScreen(views, height=28, width=122)
        self.assertEqual(
            [row.label for row in screen.rows() if row.kind == "initiative"],
            ["execution-test"],
        )
        self.assertTrue(screen.retained_counts["partial"])
        model = tui.TuiModel(height=28, width=122)
        model.initiatives = screen
        self.assertIn("partial", str(tui.render(model)[0]))

    def test_a_head_whose_graph_cannot_be_read_stays_visible_as_unknown(self) -> None:
        """Not even an unexpected failure may blank the tree behind one head."""
        with mock.patch.object(
            tui_store.InitiativeStore, "bounded_event_sample",
            side_effect=RuntimeError("event directory exploded"),
        ):
            views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertEqual(len(views), 1)
        self.assertTrue(views[0]["_graph_unavailable"])
        self.assertEqual(views[0]["_completeness"]["graphs_unavailable"], 1)
        self.assertEqual(views[0]["_completeness"]["graphs_loaded"], 0)
        self.assertIn(
            "RuntimeError", views[0]["_completeness"]["failures"][0]["reason"],
        )
        verdict = retained_classification(views[0])
        self.assertEqual(
            (verdict["current"], verdict["certain"]), (True, False),
            "missing evidence keeps a head in Current and marks it uncertain",
        )
        screen = InitiativesScreen(views, height=28, width=122)
        head = next(row for row in screen.rows() if row.kind == "initiative")
        self.assertEqual(head.nodes, "?")
        self.assertTrue(screen.retained_counts["unknown"])
        # And nothing is asserted or denied about a graph nobody could read.
        self.assertEqual(attention_items(views), [])
        screen.selection = next(
            index for index, row in enumerate(screen.rows()) if row.key == head.key
        )
        detail = "\n".join(screen.detail_lines())
        self.assertIn("could not be read in this refresh", detail)
        self.assertIn("event directory exploded", detail)
        for claimed in ("no terminal seal", "Review:", "Verify:", "Limits:"):
            self.assertNotIn(
                claimed, detail,
                "the pane may not assert facts over records nobody opened",
            )

    def test_a_foreign_or_symlinked_record_is_refused_and_counted(self) -> None:
        clean = tui._load_initiative_views(self.env, tmux=mock.Mock())
        expected = sorted(node["node_id"] for node in clean[0]["nodes"])
        self.assertTrue(expected)
        nodes_dir = self.config.initiatives_dir / self.initiative_id / "nodes"
        readable = sorted(path.name for path in nodes_dir.glob("*.json"))
        # A record whose own identity contradicts the filename carrying it.
        foreign = json.loads((nodes_dir / readable[0]).read_text())
        foreign["node_id"] = "somewhere-else"
        (nodes_dir / "not-the-same-node.json").write_text(json.dumps(foreign))
        # A symlink is refused outright rather than followed to its target,
        # even though that target is a record this reader would accept.
        (nodes_dir / "link-a.json").symlink_to(nodes_dir / readable[0])
        views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        completeness = views[0]["_completeness"]
        self.assertEqual(completeness["unavailable_records"], 2)
        self.assertFalse(completeness["complete"])
        self.assertEqual(
            sorted(node["node_id"] for node in views[0]["nodes"]), expected,
            "the valid records beside a refused one are unchanged",
        )
        reasons = " ".join(
            failure["reason"] for failure in completeness["failures"]
        )
        self.assertIn("symlink", reasons.lower())

    def test_nested_entries_cost_the_budget_one_by_one_not_one_per_graph(self) -> None:
        budget = tui_store.PresentationBudget(deadline_seconds=30.0)
        tui._load_initiative_views(self.env, tmux=mock.Mock(), budget=budget)
        summary = budget.summary()
        entries = 0
        root = self.config.initiatives_dir / self.initiative_id
        for directory in ("plans", "nodes", "attempts", "links", "actions",
                          "approvals", "seals", "reviews", "verifications",
                          "coordinators", "events"):
            path = root / directory
            if path.is_dir():
                entries += len([item for item in path.iterdir()
                                if not item.name.startswith(".")])
        self.assertEqual(summary["scanned"]["nested"], entries)
        self.assertGreater(entries, 1, "one graph must cost more than one unit")
        self.assertTrue(summary["complete"])

    def test_the_nested_cap_truncates_the_observation_and_names_itself(self) -> None:
        budget = tui_store.PresentationBudget(nested_limit=1, deadline_seconds=30.0)
        views = tui._load_initiative_views(self.env, tmux=mock.Mock(), budget=budget)
        summary = budget.summary()
        self.assertEqual(len(views), 1, "the head is kept, its graph is short")
        self.assertTrue(summary["truncated"])
        self.assertIn("nested", summary["caps_reached"])
        self.assertFalse(summary["complete"])
        self.assertFalse(tui_model._view_complete(views[0]))

    def test_the_per_head_cap_truncates_one_head_without_ending_the_pass(self) -> None:
        budget = tui_store.PresentationBudget(per_head_limit=1, deadline_seconds=30.0)
        views = tui._load_initiative_views(self.env, tmux=mock.Mock(), budget=budget)
        summary = budget.summary()
        self.assertEqual(len(views), 1)
        self.assertIn("per-head", summary["caps_reached"])
        self.assertLessEqual(summary["scanned"]["nested"], 2)
        self.assertFalse(summary["complete"])

    def test_the_event_sample_is_capped_and_never_claimed_as_exact_history(self) -> None:
        budget = tui_store.PresentationBudget(event_tail=1, deadline_seconds=30.0)
        events, complete = self.store.bounded_event_sample(self.initiative_id, budget)
        whole = self.store.list_events_snapshot(self.initiative_id)
        self.assertGreater(len(whole), 1, "the fixture must have a history to cap")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["sequence"], whole[-1]["sequence"])
        self.assertFalse(
            complete, "a capped sample is a sample, never an exact tail",
        )
        # Uncapped, the same read is the journal and says so.
        whole_budget = tui_store.PresentationBudget(deadline_seconds=30.0)
        sample, exact = self.store.bounded_event_sample(
            self.initiative_id, whole_budget,
        )
        self.assertTrue(exact)
        self.assertEqual(
            [item["sequence"] for item in sample],
            [item["sequence"] for item in whole],
        )

    def test_a_noncontiguous_journal_is_not_offered_as_complete_evidence(self) -> None:
        events_dir = self.config.initiatives_dir / self.initiative_id / "events"
        names = sorted(path.name for path in events_dir.glob("*.json"))
        self.assertGreater(len(names), 1)
        (events_dir / names[0]).unlink()
        budget = tui_store.PresentationBudget(deadline_seconds=30.0)
        _events, complete = self.store.bounded_event_sample(self.initiative_id, budget)
        self.assertFalse(complete)
        views = tui._load_initiative_views(self.env, tmux=mock.Mock())
        self.assertFalse(views[0]["_events_complete"])


class RetainedViewRefreshTests(unittest.TestCase):
    """A refresh reveals hidden work that starts asking, and re-hides it after.

    Both refresh paths are covered: the full rebuild an initiative change
    forces, and the task-only cache patch, which must not be able to leave a
    newly asking head off screen just because the head row was not cached.
    """

    INITIATIVE = "11111111-1111-4111-8111-111111111111"
    TASK = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    ATTEMPT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def setUp(self) -> None:
        self.task = task_record(task_id=self.TASK, slug="quiet-worker")
        self.run_id = self.task["runs"][0]["run_id"]

    def view(self) -> dict:
        view = IncrementalSnapshotTests._initiative("paused")
        view["nodes"] = [{
            "node_id": "implementation-a", "state": "succeeded", "type": "work",
            "goal": "Implement A",
        }]
        view["attempts"] = [{
            "attempt_id": self.ATTEMPT, "node_id": "implementation-a",
            "ordinal": 1, "state": "sealed-success",
        }]
        view["links"] = [{"attempt_id": self.ATTEMPT, "control_task_id": self.TASK}]
        return view

    def test_a_completeness_change_alone_invalidates_the_cached_rows(self) -> None:
        """Cache identity carries every completeness the classification reads.

        A refresh that changes nothing but what the read could see still
        changes which heads belong on screen and what the counts may claim, so
        a row set built under one completeness must never be reused under
        another.
        """
        screen = InitiativesScreen([self.view()], height=28, width=122)
        screen.rows()
        for field, value in (
            ("_completeness", {"complete": False, "truncated": True,
                               "unavailable_records": 0}),
            ("_graph_complete", False),
            ("_graph_unavailable", True),
            ("_events_complete", False),
            ("_metadata_only", True),
        ):
            with self.subTest(field=field):
                fresh = InitiativesScreen([self.view()], height=28, width=122)
                before = fresh._current_rows_key()
                fresh.rows()
                fresh.views[0][field] = value
                self.assertNotEqual(
                    fresh._current_rows_key(), before,
                    "the cache key must depend on this completeness",
                )
        # And the view scope is part of the same identity.
        scoped = InitiativesScreen([self.view()], height=28, width=122)
        key = scoped._current_rows_key()
        scoped.view_scope = "all"
        self.assertNotEqual(scoped._current_rows_key(), key)

    def worker(self, state: str, detail: str, observed_at: str) -> tui.TuiRow:
        reconciliation = {
            "contract": "asha.control-reconciliation.v1", "task_id": self.TASK,
            "state": state, "blocker": None,
            "evidence": [{"state": state, "detail": detail}],
            "runs": [{
                "contract": "asha.control-run-reconciliation.v1",
                "run_id": self.run_id, "state": "active", "blocker": None, "evidence": [],
            }],
        }
        observation = StateObservation(state, self.run_id, "events", observed_at, "fresh", detail)
        return tui.TuiRow.from_records(copy.deepcopy(self.task), reconciliation, observation)

    def snapshot(self, cache: tui.RefreshCache, worker: tui.TuiRow) -> tui.RefreshSnapshot:
        rows, changed, removed, order_changed = cache.stabilize_rows([worker])
        views, views_changed = cache.stabilize_branch("initiatives", (self.view(),), None)
        rooms, rooms_changed = cache.stabilize_branch("rooms", (), None)
        return _snapshot(
            rows, changed_rows=changed, removed=removed, order_changed=order_changed,
            initiatives=views, rooms=rooms, initiatives_changed=views_changed,
            rooms_changed=rooms_changed,
        )

    @staticmethod
    def facts(screen: InitiativesScreen) -> list:
        return [(row.key, row.state, row.attention, row.worker) for row in screen.rows()]

    def rebuild(self, model: tui.TuiModel) -> InitiativesScreen:
        screen = model.initiatives
        return InitiativesScreen(
            screen.views, height=screen.height, width=screen.width,
            expanded=set(screen.expanded), task_rows=model.rows,
            view_scope=screen.view_scope,
        )

    def heads(self, screen: InitiativesScreen) -> list:
        return [row.id for row in screen.rows() if row.kind == "initiative"]

    def test_a_task_only_patch_reveals_a_hidden_head_that_starts_asking(self) -> None:
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        quiet = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:05Z"))
        tui._apply_refresh_snapshot(model, {}, quiet)
        cache.mark_applied(quiet)
        screen = model.initiatives
        self.assertEqual(self.heads(screen), [], "a sealed, parked, idle head is quiet")
        self.assertEqual(screen.retained_counts, {
            "shown": 0, "loaded": 1, "hidden": 1, "partial": False, "unknown": False,
        })
        # The worker beneath it reaches a prompt on a task-only pass.
        prompt = self.snapshot(
            cache, self.worker("needs-input", "pane shows the input prompt", "2026-08-14T18:00:06Z"),
        )
        self.assertFalse(prompt.initiative_views_changed)
        self.assertEqual([row.task["task_id"] for row in prompt.changed_rows], [self.TASK])
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, prompt))
        cache.mark_applied(prompt)
        self.assertEqual(self.heads(screen), [self.INITIATIVE], "the ask brings its head back")
        self.assertEqual(screen.retained_counts["shown"], 1)
        self.assertEqual(screen.retained_counts["hidden"], 0)
        self.assertEqual(self.facts(screen), self.facts(self.rebuild(model)))
        self.assertEqual(
            [item["kind"] for item in attention_items(screen.views, model.rows)], ["worker"],
        )
        # Answered, the evidence permits the quiet classification again.
        answered = self.snapshot(
            cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:07Z"),
        )
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, answered))
        cache.mark_applied(answered)
        self.assertEqual(self.heads(screen), [])
        self.assertEqual(screen.retained_counts["hidden"], 1)
        self.assertEqual(self.facts(screen), self.facts(self.rebuild(model)))
        # All retained draws it throughout, with the same facts a rebuild has.
        screen.set_view_scope("all")
        self.assertEqual(self.heads(screen), [self.INITIATIVE])
        self.assertEqual(self.facts(screen), self.facts(self.rebuild(model)))

    def test_a_full_refresh_reveals_a_hidden_head_that_starts_asking(self) -> None:
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        quiet = self.snapshot(cache, self.worker("idle", "turn stopped", "2026-08-14T18:00:05Z"))
        tui._apply_refresh_snapshot(model, {}, quiet)
        cache.mark_applied(quiet)
        screen = model.initiatives
        self.assertEqual(self.heads(screen), [])

        # The initiative branch changes: the node itself now needs a decision,
        # which is durable demand and forces a full rebuild, not a patch.
        asking = self.view()
        asking["initiative"]["state"] = "running"
        asking["nodes"][0]["state"] = "needs-input"
        rows, changed, removed, order = cache.stabilize_rows([
            self.worker("idle", "turn stopped", "2026-08-14T18:00:05Z"),
        ])
        views, views_changed = cache.stabilize_branch("initiatives", (asking,), None)
        rooms, rooms_changed = cache.stabilize_branch("rooms", (), None)
        snapshot = _snapshot(
            rows, changed_rows=changed, removed=removed, order_changed=order,
            initiatives=views, rooms=rooms, initiatives_changed=views_changed,
            rooms_changed=rooms_changed,
        )
        self.assertTrue(snapshot.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, snapshot))
        cache.mark_applied(snapshot)
        self.assertEqual(self.heads(screen), [self.INITIATIVE])
        self.assertEqual(screen.retained_counts["hidden"], 0)
        self.assertEqual(self.facts(screen), self.facts(self.rebuild(model)))
        self.assertEqual(
            [item["kind"] for item in attention_items(screen.views, model.rows)],
            ["needs-input"],
        )

    def test_a_patch_that_keeps_the_head_keeps_the_counts_truthful(self) -> None:
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        prompt = self.snapshot(
            cache, self.worker("needs-input", "pane shows the input prompt", "2026-08-14T18:00:05Z"),
        )
        tui._apply_refresh_snapshot(model, {}, prompt)
        cache.mark_applied(prompt)
        screen = model.initiatives
        self.assertEqual(self.heads(screen), [self.INITIATIVE])
        head_before = next(row for row in screen.rows() if row.kind == "initiative")

        still_asking = self.snapshot(
            cache, self.worker("needs-input", "pane shows the trust prompt", "2026-08-14T18:00:06Z"),
        )
        self.assertFalse(still_asking.initiative_views_changed)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, still_asking))
        cache.mark_applied(still_asking)
        self.assertIs(
            next(row for row in screen.rows() if row.kind == "initiative"), head_before,
            "the head row is reused: this really is the patched path",
        )
        self.assertEqual(screen.retained_counts, {
            "shown": 1, "loaded": 1, "hidden": 0, "partial": False, "unknown": False,
        })
        self.assertEqual(
            screen.retained_counts, self.rebuild(model).retained_counts,
            "the patched counts are the counts a full rebuild would report",
        )

    def test_an_unbound_task_change_still_patches_without_a_rebuild(self) -> None:
        cache = tui.RefreshCache()
        cache.begin_generation(0)
        model = tui.TuiModel(())
        model._ensure_screen()
        first = tui._terminal_row(_terminal_task("loose"))
        rows, changed, removed, order = cache.stabilize_rows([first])
        cache.stabilize_branch("initiatives", (), None)
        cache.stabilize_branch("rooms", (), None)
        applied = _snapshot(rows, changed_rows=changed, removed=removed, order_changed=order)
        tui._apply_refresh_snapshot(model, {}, applied)
        cache.mark_applied(applied)
        before = next(row for row in model.initiatives.rows() if row.task_id is not None)

        changed_task = copy.deepcopy(first.task)
        changed_task["updated_at"] = "2026-08-14T18:00:02Z"
        rows, changed, removed, order = cache.stabilize_rows([tui._terminal_row(changed_task)])
        _, initiatives_changed = cache.stabilize_branch("initiatives", (), None)
        _, rooms_changed = cache.stabilize_branch("rooms", (), None)
        self.assertTrue(tui._apply_refresh_snapshot(model, {}, _snapshot(
            rows, changed_rows=changed, removed=removed, order_changed=order,
            initiatives_changed=initiatives_changed, rooms_changed=rooms_changed,
        )))
        after = next(row for row in model.initiatives.rows() if row.task_id is not None)
        self.assertIsNot(after, before)
        self.assertEqual(after.task_id, before.task_id)


class OwnedTestFileGuardTests(unittest.TestCase):
    """Running an owned test file directly must collect exactly what `-m` does.

    The accepted finding: a ``if __name__ == "__main__": unittest.main()``
    guard placed anywhere but the end of a file calls `unittest.main()` before
    the classes below it exist, so direct execution silently omits them while
    every gate passes, because every gate invokes ``python3 -m unittest
    <module>`` where the guard is false.

    The equality below is measured through the real mechanism rather than a
    proxy for it: the file is executed with ``__name__`` set to ``"__main__"``
    and `unittest.main` replaced by a collector, so the number counted is
    exactly what the guard would have handed the runner at the exact point it
    fires.
    """

    OWNED = (
        "test_control_tui_initiatives_mode",
        "test_control_tui_style",
        "test_control_tui_performance",
        "test_orchestration_tui_model",
    )

    def owned_path(self, name: str) -> Path:
        return Path(__file__).resolve().parent / f"{name}.py"

    def direct_collection(self, path: Path) -> int:
        """What `unittest.main()` would collect when this file is run directly."""
        module = types.ModuleType("__main__")
        module.__file__ = str(path)
        collected: list[int] = []

        def collector(*args, **kwargs):
            del args, kwargs
            collected.append(
                unittest.defaultTestLoader.loadTestsFromModule(module).countTestCases()
            )

        compiled = compile(path.read_text(), str(path), "exec")
        with mock.patch.object(unittest, "main", collector):
            exec(compiled, module.__dict__)  # noqa: S102 - the guard is the subject
        self.assertEqual(
            len(collected), 1, f"{path.name} never reached its own guard",
        )
        return collected[0]

    def test_direct_and_module_collection_are_equal_for_every_owned_file(self) -> None:
        for name in self.OWNED:
            with self.subTest(module=name):
                by_module = unittest.defaultTestLoader.loadTestsFromName(
                    f"tests.python.{name}",
                ).countTestCases()
                self.assertGreater(by_module, 0)
                self.assertEqual(
                    self.direct_collection(self.owned_path(name)), by_module,
                    "running this file directly must not omit a single test",
                )

    def test_the_guard_is_the_last_statement_in_every_owned_file(self) -> None:
        for name in self.OWNED:
            with self.subTest(module=name):
                body = ast.parse(self.owned_path(name).read_text()).body
                guards = [
                    index for index, node in enumerate(body)
                    if isinstance(node, ast.If) and any(
                        isinstance(child, ast.Name) and child.id == "__name__"
                        for child in ast.walk(node.test)
                    )
                ]
                self.assertEqual(len(guards), 1, "exactly one entry-point guard")
                self.assertEqual(
                    guards[0], len(body) - 1,
                    "no test class may be defined after the guard",
                )



if __name__ == "__main__":
    unittest.main()
