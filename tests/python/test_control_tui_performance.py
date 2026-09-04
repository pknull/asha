"""Deterministic refresh-cost and input-isolation regressions for Control."""

from __future__ import annotations

import contextlib
import copy
import subprocess
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from lib.control import tui
from lib.control.reconcile import Evidence
from lib.control.tmux import TmuxAdapter, TmuxError
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


if __name__ == "__main__":
    unittest.main()
