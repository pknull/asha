from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from lib.control import view
from lib.control.cli import _publish_tmux_presentation, main as control_main
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.events import (
    EVENT_CONTRACT,
    EVENTS,
    EventError,
    events_dir,
    expire_snapshot,
    expire_terminal_snapshots,
    read_snapshot,
    summarize,
    write_snapshot,
)
from lib.control.jj import WorkspaceIdentity
from lib.control.reconcile import Evidence, LiveAdapters, reconcile_task
from lib.control.store import TaskStore
from lib.control.tmux import PaneFacts, TmuxError
from tests.python.test_control_config_model import task_record


SNAPSHOT_KEYS = {
    "contract", "task_id", "run_id", "event", "state", "harness",
    "harness_session_id", "exit_status", "pane_id", "observed_at",
}


class Increment4Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.task_id = str(uuid.uuid4())
        self.run_id = str(uuid.uuid4())

    def write(self, event: str = "prompt-submitted", **overrides):
        values = {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "event": event,
            "harness": "claude",
            "harness_session_id": "session-123",
            "exit_status": None,
            "pane_id": "%7",
        }
        values.update(overrides)
        return write_snapshot(self.config, **values)

    def invoke(self, args: list[str], env: dict[str, str] | None = None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(args, env=self.env if env is None else env)
        return status, stdout.getvalue(), stderr.getvalue()


class EventSnapshotTests(Increment4Fixture):
    def test_round_trip_exact_keys_bound_mode_and_payload_minimization(self) -> None:
        path = self.write()
        snapshot = read_snapshot(self.config, self.run_id)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(set(snapshot), SNAPSHOT_KEYS)
        self.assertEqual(snapshot["contract"], EVENT_CONTRACT)
        self.assertEqual(snapshot["state"], "working")
        self.assertLessEqual(path.stat().st_size, 4096)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        serialized = path.read_text(encoding="utf-8")
        for forbidden in (
            '"prompt":', "tool_input", "tool_arguments", "tool_output",
            "terminal_capture", '"environment":', '"payload":',
        ):
            self.assertNotIn(forbidden, serialized)

    def test_tmux_presentation_calls_use_five_second_deadlines(self) -> None:
        self.write()
        adapter = mock.Mock()
        adapter.pane_option.return_value = self.run_id
        adapter.pane_facts.return_value = PaneFacts(
            "%7", 123, False, None, None, "asha-task", "work", "title",
        )
        adapter.session_option.return_value = self.task_id
        with mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch(
                    "lib.control.events.summarize", return_value="asha: 1 working",
                ):
            _publish_tmux_presentation(
                self.config, self.run_id, "%7", self.task_id,
            )
        adapter.pane_option.assert_called_once_with(
            "%7", "@asha_run_id", deadline_seconds=5,
        )
        adapter.set_pane_option.assert_called_once_with(
            "%7", "@asha_state", "working", deadline_seconds=5,
        )
        adapter.pane_facts.assert_called_once_with("%7", deadline_seconds=5)
        adapter.session_option.assert_called_once_with(
            "asha-task", "@asha_task_id", deadline_seconds=5,
        )
        adapter.set_session_option.assert_called_once_with(
            "asha-task", "@asha_state", "working", deadline_seconds=5,
        )
        adapter.set_server_summary.assert_called_once_with(
            "asha: 1 working", deadline_seconds=5,
        )

    def test_atomic_replace_failure_preserves_the_old_valid_snapshot(self) -> None:
        self.write("prompt-submitted")
        before = read_snapshot(self.config, self.run_id)

        with mock.patch("lib.control.events.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(EventError):
                self.write("turn-stopped")

        self.assertEqual(read_snapshot(self.config, self.run_id), before)
        self.assertEqual(
            [path.name for path in events_dir(self.config).iterdir()],
            [f"{self.run_id}.json"],
        )

    def test_rejects_hostile_identifiers_events_harnesses_and_strings(self) -> None:
        cases = (
            {"task_id": "not-a-uuid"},
            {"run_id": "NOT-A-UUID"},
            {"event": "unknown-event"},
            {"harness": "unknown"},
            {"harness_session_id": "bad\nvalue"},
            {"harness_session_id": "x" * 201},
            {"pane_id": "%7\u202e"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(EventError):
                self.write(**overrides)

    def test_all_six_events_map_to_the_settled_states(self) -> None:
        expected = {
            "session-start": None,
            "prompt-submitted": "working",
            "tool-completed": "working",
            "permission-requested": "needs-input",
            "turn-stopped": "idle",
            "session-ended": "exited",
        }
        self.assertEqual(set(EVENTS), set(expected))
        for event, state in expected.items():
            with self.subTest(event=event):
                self.write(event)
                self.assertEqual(read_snapshot(self.config, self.run_id)["state"], state)

    def test_session_end_exit_status_selects_exited_or_failed(self) -> None:
        for status, expected in ((None, "exited"), (0, "exited"), (1, "failed"), (127, "failed")):
            with self.subTest(status=status):
                self.write("session-ended", exit_status=status)
                self.assertEqual(
                    read_snapshot(self.config, self.run_id)["state"], expected,
                )

    def test_reader_rejects_duplicate_keys_extra_keys_and_oversized_files(self) -> None:
        path = self.write()
        valid = path.read_text(encoding="utf-8").strip()
        malformed = valid[:-1] + ',"event":"turn-stopped"}'
        path.write_text(malformed, encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(EventError):
            read_snapshot(self.config, self.run_id)

        value = json.loads(valid)
        value["payload"] = "forbidden"
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaises(EventError):
            read_snapshot(self.config, self.run_id)

        path.write_bytes(b"{" + b" " * 4096 + b"}")
        path.chmod(0o600)
        with self.assertRaises(EventError):
            read_snapshot(self.config, self.run_id)

    def test_expire_snapshot_is_safe_and_idempotent(self) -> None:
        path = self.write()

        self.assertTrue(expire_snapshot(self.config, self.run_id))
        self.assertFalse(path.exists())
        self.assertFalse(expire_snapshot(self.config, self.run_id))
        with self.assertRaises(EventError):
            expire_snapshot(self.config, "not-a-run-id")

    def test_terminal_expiry_preserves_nonterminal_and_blocked_snapshots(self) -> None:
        self.write()
        self.assertFalse(expire_terminal_snapshots(self.config, [{
            "run_id": self.run_id, "state": "working", "blocker": None,
        }]))
        self.assertFalse(expire_terminal_snapshots(self.config, [{
            "run_id": self.run_id, "state": "exited", "blocker": "conflict",
        }]))
        self.assertIsNotNone(read_snapshot(self.config, self.run_id))

    def test_summary_labels_event_only_evidence_and_reports_truncation(self) -> None:
        self.assertEqual(summarize(self.config), "asha last-event-only: no snapshots")
        for _ in range(3):
            self.write(run_id=str(uuid.uuid4()))

        with mock.patch("lib.control.events.MAX_SUMMARY_SNAPSHOTS", 2):
            summary = summarize(self.config)

        self.assertEqual(
            summary,
            "asha last-event-only: 2 working, 2 total (truncated: 1 snapshot omitted)",
        )

    def test_summary_scopes_unvalidated_cap_and_reports_observation_failure(self) -> None:
        self.write(run_id="ffffffff-ffff-4fff-8fff-ffffffffffff")
        directory = events_dir(self.config)
        for name in ("-bad-a.json", "-bad-b.json"):
            (directory / name).write_text("not json", encoding="utf-8")

        with mock.patch("lib.control.events.MAX_SUMMARY_SNAPSHOTS", 2):
            self.assertEqual(
                summarize(self.config),
                "asha last-event-only: no valid inspected snapshots "
                "(truncated: 1 snapshot omitted)",
            )
        with mock.patch(
            "lib.control.events.events_dir", side_effect=EventError("unavailable"),
        ):
            self.assertEqual(
                summarize(self.config),
                "asha last-event-only: snapshot status unavailable",
            )

    def test_terminal_reconciliation_persists_before_expiry_and_refreshes_summary(self) -> None:
        source = self.root / "source"
        workspace = self.config.workspace_root / "repo-key" / "terminal"
        source.mkdir()
        source.chmod(0o755)
        workspace.mkdir(parents=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        task = task_record(
            task_id=self.task_id,
            repository_root=str(source),
            workspace_path=str(workspace),
        )
        task["runs"][0]["run_id"] = self.run_id
        store = TaskStore(self.config)
        store.save(task)
        self.write()

        class TerminalAdapters:
            def tmux(inner, task, run):
                return Evidence("tmux", "match", "owned")

            def process(inner, task, run):
                return Evidence("process", "missing", "exited without status")

            def jj(inner, task):
                return Evidence("jj", "match", "owned")

            def event(inner, task, run):
                return Evidence("event", "match", "session ended", state="exited")

        adapters = TerminalAdapters()
        presentation = mock.Mock()
        persisted, terminal = view.locked_reconciliation(
            store, mock.Mock(), task["task_id"], adapters, mock.Mock(),
            presentation=presentation,
        )

        self.assertEqual(terminal["runs"][0]["state"], "exited")
        self.assertEqual(persisted["lifecycle"], "ended")
        self.assertEqual(store.read(task["task_id"])["runs"][0]["state"], "exited")
        self.assertIsNone(read_snapshot(self.config, self.run_id))
        presentation.set_server_summary.assert_called_once_with(
            "asha last-event-only: no snapshots", deadline_seconds=5,
        )

        class MissingEventAdapters(TerminalAdapters):
            def event(inner, task, run):
                return Evidence("event", "missing", "snapshot expired")

        _, repeated = view.locked_reconciliation(
            store, mock.Mock(), task["task_id"], MissingEventAdapters(), mock.Mock(),
        )
        self.assertEqual(repeated["runs"][0]["state"], "exited")

    def test_partial_terminal_reconciliation_keeps_unpersisted_event_evidence(self) -> None:
        source = self.root / "mixed-source"
        workspace = self.config.workspace_root / "repo-key" / "mixed"
        source.mkdir()
        source.chmod(0o755)
        workspace.mkdir(parents=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        task = task_record(
            task_id=self.task_id,
            repository_root=str(source),
            workspace_path=str(workspace),
        )
        latest = task["runs"][0]
        latest["run_id"] = self.run_id
        prior = copy.deepcopy(latest)
        prior["run_id"] = str(uuid.uuid4())
        prior["state"] = "exited"
        prior["evidence"] = "stored exit"
        task["runs"] = [prior, latest]
        store = TaskStore(self.config)
        store.save(task)
        self.write("session-ended")

        class MixedAdapters:
            def tmux(inner, task, run):
                return Evidence("tmux", "match", "owned")

            def process(inner, task, run):
                if run["run_id"] == prior["run_id"]:
                    return Evidence("process", "match", "unexpectedly live")
                return Evidence("process", "missing", "exited without status")

            def jj(inner, task):
                return Evidence("jj", "match", "owned")

            def event(inner, task, run):
                if run["run_id"] == self.run_id:
                    return Evidence("event", "match", "session ended", state="exited")
                return Evidence("event", "missing", "no snapshot")

        persisted, result = view.locked_reconciliation(
            store, mock.Mock(), task["task_id"], MixedAdapters(), mock.Mock(),
            presentation=mock.Mock(),
        )

        self.assertEqual([run["state"] for run in result["runs"]], ["stale", "exited"])
        self.assertEqual(persisted["lifecycle"], "running")
        self.assertEqual(persisted["runs"][-1]["state"], "starting")
        self.assertIsNotNone(read_snapshot(self.config, self.run_id))


class EventCliTests(Increment4Fixture):
    def seed_task(
        self, *, pane_id: str = "%4", task_id: str | None = None,
        run_id: str | None = None, harness: str = "claude",
    ) -> dict:
        source = self.root / "source"
        workspace = self.config.workspace_root / "repo-key" / "control-test"
        source.mkdir(exist_ok=True)
        source.chmod(0o755)
        workspace.mkdir(parents=True, exist_ok=True)
        # The namespace predicate rejects writable ancestors and requires 0700
        # destination components: privatize the fixture chain like production.
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        task = task_record(
            task_id=self.task_id if task_id is None else task_id,
            repository_root=str(source),
            workspace_path=str(workspace),
        )
        task["runs"][0]["run_id"] = self.run_id if run_id is None else run_id
        task["runs"][0]["pane_id"] = pane_id
        task["runs"][0]["harness"] = harness
        TaskStore(self.config).save(task)
        return task

    def managed_env(self) -> dict[str, str]:
        return {
            **self.env,
            "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task_id,
            "ASHA_CONTROL_RUN_ID": self.run_id,
            "ASHA_CONTROL_STATE_DIR": str(self.config.tasks_dir),
        }

    def test_unmanaged_event_is_the_cheapest_silent_noop(self) -> None:
        for marker in (None, "0", "true"):
            env = {} if marker is None else {"ASHA_CONTROL_MANAGED": marker}
            with self.subTest(marker=marker):
                status, stdout, stderr = self.invoke(
                    ["control", "event", "--event", "prompt-submitted"], env,
                )
                self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertFalse(self.config.runtime_dir.exists())

    def test_managed_event_writes_from_environment_identity(self) -> None:
        self.seed_task()

        status, stdout, stderr = self.invoke([
            "control", "event", "--event", "prompt-submitted",
            "--harness", "codex", "--session-id", "opaque-session",
            "--pane-id", "%4", "--json",
        ], self.managed_env())

        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["contract"], EVENT_CONTRACT)
        snapshot = read_snapshot(self.config, self.run_id)
        self.assertEqual(snapshot["task_id"], self.task_id)
        self.assertEqual(snapshot["harness"], "codex")

    def test_event_harness_precedence_is_payload_then_environment_then_stored_run(self) -> None:
        self.seed_task(harness="codex")

        cases = (
            ("stored run", [], {}, "codex"),
            ("environment", [], {"ASHA_HARNESS": "opencode"}, "opencode"),
            (
                "explicit payload",
                ["--harness", "claude"],
                {"ASHA_HARNESS": "opencode"},
                "claude",
            ),
        )
        for label, harness_args, environment, expected in cases:
            with self.subTest(label=label), \
                    mock.patch("lib.control.cli._publish_tmux_presentation"):
                status, stdout, stderr = self.invoke([
                    "control", "event", "--event", "prompt-submitted",
                    "--pane-id", "%4", *harness_args,
                ], {**self.managed_env(), **environment})

            self.assertEqual((status, stdout, stderr), (0, "", ""))
            self.assertEqual(
                read_snapshot(self.config, self.run_id)["harness"], expected,
            )

    def test_managed_event_authorizes_with_lock_free_peek(self) -> None:
        task = self.seed_task()
        store = mock.Mock()
        store.peek.return_value = task
        store.read.side_effect = AssertionError("event authorization must not lock")

        with mock.patch("lib.control.cli.TaskStore", return_value=store), \
                mock.patch("lib.control.cli._publish_tmux_presentation"):
            status, stdout, stderr = self.invoke([
                "control", "event", "--event", "prompt-submitted",
                "--pane-id", "%4",
            ], self.managed_env())

        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertEqual(
            store.peek.call_args_list,
            [mock.call(self.task_id), mock.call(self.task_id)],
        )
        store.read.assert_not_called()

    def test_late_event_write_is_removed_after_terminal_record_wins(self) -> None:
        task = self.seed_task()
        terminal = copy.deepcopy(task)
        terminal["lifecycle"] = "ended"
        terminal["runs"][0]["state"] = "exited"
        store = mock.Mock()
        store.peek.side_effect = [task, terminal]

        with mock.patch("lib.control.cli.TaskStore", return_value=store), \
                mock.patch("lib.control.cli._publish_tmux_presentation") as publish, \
                mock.patch("lib.control.cli.publish_server_summary") as publish_summary:
            status, stdout, stderr = self.invoke([
                "control", "event", "--event", "turn-stopped", "--pane-id", "%4",
            ], self.managed_env())

        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertIsNone(read_snapshot(self.config, self.run_id))
        publish.assert_not_called()
        publish_summary.assert_called_once()

    def test_event_rejects_missing_task_foreign_run_and_unowned_pane(self) -> None:
        self.seed_task()
        cases = (
            (
                "missing task",
                {"ASHA_CONTROL_TASK_ID": str(uuid.uuid4())},
                ["--pane-id", "%4"],
            ),
            (
                "foreign run",
                {"ASHA_CONTROL_RUN_ID": str(uuid.uuid4())},
                ["--pane-id", "%4"],
            ),
            ("mismatched pane", {}, ["--pane-id", "%99"]),
            ("missing pane", {}, []),
        )

        for label, environment, pane_args in cases:
            with self.subTest(label=label), \
                    mock.patch("lib.control.cli.write_snapshot") as write, \
                    mock.patch("lib.control.cli._publish_tmux_presentation") as publish:
                status, stdout, stderr = self.invoke([
                    "control", "event", "--event", "prompt-submitted",
                    *pane_args,
                ], {**self.managed_env(), **environment})

                self.assertEqual((status, stdout), (0, ""))
                self.assertIn("asha control event:", stderr)
                self.assertLessEqual(len(stderr), 600)
                write.assert_not_called()
                publish.assert_not_called()

    def test_rejected_event_preserves_an_existing_snapshot(self) -> None:
        self.seed_task()
        self.write("prompt-submitted", pane_id="%4")
        before = read_snapshot(self.config, self.run_id)

        status, stdout, stderr = self.invoke([
            "control", "event", "--event", "turn-stopped",
            "--pane-id", "%99",
        ], self.managed_env())

        self.assertEqual((status, stdout), (0, ""))
        self.assertIn("asha control event:", stderr)
        self.assertEqual(read_snapshot(self.config, self.run_id), before)

    def test_broken_unwritable_runtime_dir_fails_open(self) -> None:
        self.seed_task()
        runtime = self.config.runtime_dir
        runtime.chmod(0o500)
        self.addCleanup(runtime.chmod, 0o700)

        status, stdout, stderr = self.invoke([
            "control", "event", "--event", "prompt-submitted",
            "--pane-id", "%4",
        ], self.managed_env())

        self.assertEqual((status, stdout), (0, ""))
        self.assertIn("asha control event:", stderr)
        self.assertLessEqual(len(stderr), 600)

    def test_malformed_managed_value_fails_open_but_usage_errors_do_not(self) -> None:
        self.seed_task()
        status, stdout, stderr = self.invoke([
            "control", "event", "--event", "unknown", "--pane-id", "%4",
        ], self.managed_env())
        self.assertEqual((status, stdout), (0, ""))
        self.assertIn("asha control event:", stderr)

        status, stdout, stderr = self.invoke(["control", "event", "--bogus"], self.managed_env())
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("unknown control event argument", stderr)

    def test_bare_control_degrades_without_a_tty_and_tmux_contract_is_unchanged(self) -> None:
        status, stdout, stderr = self.invoke(["control"])
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("asha task list --json", stderr)
        self.assertNotIn("Traceback", stderr)

        status, stdout, stderr = self.invoke(["control", "tmux"])
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("@asha_managed", stdout)


class LiveEventEvidenceTests(Increment4Fixture):
    def setUp(self) -> None:
        super().setUp()
        workspace = self.root / "workspace"
        workspace.mkdir()
        self.task = task_record(
            repository_root=str(self.root / "source"),
            workspace_path=str(workspace),
        )
        self.task_id = self.task["task_id"]
        self.run_id = self.task["runs"][0]["run_id"]

    def test_event_missing_mismatched_malformed_matched_and_session_start(self) -> None:
        adapters = LiveAdapters(config=self.config)
        run = self.task["runs"][0]
        self.assertEqual(adapters.event(self.task, run).outcome, "missing")

        self.write(task_id=str(uuid.uuid4()))
        self.assertEqual(adapters.event(self.task, run).outcome, "mismatch")

        path = events_dir(self.config) / f"{self.run_id}.json"
        path.write_text("not-json", encoding="utf-8")
        path.chmod(0o600)
        self.assertEqual(adapters.event(self.task, run).outcome, "unavailable")

        self.write("tool-completed")
        matched = adapters.event(self.task, run)
        self.assertEqual((matched.outcome, matched.state), ("match", "working"))

        self.write("session-start")
        started = adapters.event(self.task, run)
        self.assertEqual((started.outcome, started.state), ("missing", None))
        self.assertIn("no semantic state", started.detail)

    def test_evidence_state_permission_is_narrowly_widened(self) -> None:
        allowed = (
            Evidence("event", "match", "verified event", state="working"),
            Evidence("process", "missing", "dead pane", state="exited"),
            Evidence("process", "missing", "dead pane", state="failed"),
        )
        self.assertEqual(len(allowed), 3)
        invalid = (
            lambda: Evidence("tmux", "missing", "detail", state="exited"),
            lambda: Evidence("process", "match", "detail", state="exited"),
            lambda: Evidence("process", "missing", "detail", state="working"),
            lambda: Evidence("event", "missing", "detail", state="working"),
        )
        for call in invalid:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_evidence_stale_permission_is_narrowly_widened(self) -> None:
        allowed = Evidence(
            "event", "match", "detail", state="working", stale=True,
        )
        self.assertTrue(allowed.stale)
        invalid = (
            lambda: Evidence(
                "event", "match", "detail", state="idle", stale=True,
            ),
            lambda: Evidence(
                "event", "match", "detail", state="exited", stale=True,
            ),
            lambda: Evidence("event", "unavailable", "detail", stale=True),
            lambda: Evidence("tmux", "match", "detail", stale=True),
        )
        for call in invalid:
            with self.subTest(call=call), self.assertRaisesRegex(
                ValueError, "only a matched in-progress event may be marked stale",
            ):
                call()

    def adapters_for_dead_status(self, status: int | None, signal: int | None = None):
        task = self.task

        class DeadTmux:
            def has_session(inner, name):
                return True

            def session_option(inner, name, option):
                return "1" if option == "@asha_managed" else task["task_id"]

            def pane_facts(inner, pane_id):
                run = task["runs"][0]
                return PaneFacts(
                    pane_id, run["pid"], True, status, signal,
                    task["tmux"]["session"], task["tmux"]["window"], "fixture",
                )

        class MatchingJj:
            def inspect_workspace(inner, path, name, require_empty=False):
                return WorkspaceIdentity(
                    name, task["jj"]["change_id"], task["jj"]["working_commit_id"],
                    (task["jj"]["base_commit_id"],), "fixture",
                )

        return LiveAdapters(config=self.config, tmux=DeadTmux(), jj=MatchingJj())

    def test_dead_status_and_signal_death_reconcile_terminal(self) -> None:
        for status, expected in ((0, "exited"), (7, "failed")):
            with self.subTest(status=status):
                result = reconcile_task(self.task, self.adapters_for_dead_status(status))
                self.assertEqual(result["state"], expected)
        signalled = reconcile_task(self.task, self.adapters_for_dead_status(None, 15))
        self.assertEqual(signalled["state"], "failed")

    def test_live_process_is_never_overridden_by_terminal_event(self) -> None:
        task = self.task
        run = task["runs"][0]
        self.write("session-ended", exit_status=0)

        class LiveTmux:
            def has_session(inner, name):
                return True

            def session_option(inner, name, option):
                return "1" if option == "@asha_managed" else task["task_id"]

            def pane_facts(inner, pane_id):
                return PaneFacts(
                    pane_id, run["pid"], False, None, None,
                    task["tmux"]["session"], task["tmux"]["window"], "fixture",
                )

        class MatchingJj:
            def inspect_workspace(inner, path, name, require_empty=False):
                return WorkspaceIdentity(
                    name, task["jj"]["change_id"], task["jj"]["working_commit_id"],
                    (task["jj"]["base_commit_id"],), "fixture",
                )

        adapters = LiveAdapters(config=self.config, tmux=LiveTmux(), jj=MatchingJj())
        with mock.patch("lib.control.reconcile.harness_api.verify_process", return_value=True):
            result = reconcile_task(task, adapters)
        self.assertEqual(result["state"], "stale")
        self.assertEqual(
            result["blocker"], "event: terminal state contradicts matched live process",
        )

    def _now_after_snapshot(self, seconds: int):
        snapshot = read_snapshot(self.config, self.run_id)
        assert snapshot is not None
        observed = datetime.fromisoformat(snapshot["observed_at"])
        return lambda: observed + timedelta(seconds=seconds)

    def test_in_progress_event_ages_to_match_stale_past_window(self) -> None:
        self.write("prompt-submitted")
        run = self.task["runs"][0]

        fresh = LiveAdapters(config=self.config).event(self.task, run)
        self.assertEqual((fresh.outcome, fresh.state), ("match", "working"))
        self.assertFalse(fresh.stale)

        window = self.config.event_staleness_seconds
        aged = LiveAdapters(
            config=self.config, now=self._now_after_snapshot(window + 60),
        ).event(
            self.task, run,
        )
        self.assertEqual((aged.outcome, aged.state), ("match", "working"))
        self.assertTrue(aged.stale)
        self.assertIn("stale", aged.detail)
        self.assertIn(str(window), aged.detail)

    def test_age_exactly_at_window_does_not_age(self) -> None:
        self.write("prompt-submitted")
        run = self.task["runs"][0]
        window = self.config.event_staleness_seconds

        result = LiveAdapters(
            config=self.config, now=self._now_after_snapshot(window),
        ).event(self.task, run)

        self.assertEqual((result.outcome, result.state), ("match", "working"))
        self.assertFalse(result.stale)

    def test_idle_event_is_not_aged(self) -> None:
        # turn-stopped -> idle is a legitimate resting state; it must survive
        # the window so a finished-and-waiting agent is not flipped to unknown.
        self.write("turn-stopped")
        run = self.task["runs"][0]
        aged = LiveAdapters(
            config=self.config,
            now=self._now_after_snapshot(self.config.event_staleness_seconds + 60),
        ).event(self.task, run)
        self.assertEqual((aged.outcome, aged.state), ("match", "idle"))
        self.assertFalse(aged.stale)

    def test_terminal_event_is_never_aged(self) -> None:
        self.write("session-ended", exit_status=0)
        run = self.task["runs"][0]
        aged = LiveAdapters(
            config=self.config,
            now=self._now_after_snapshot(self.config.event_staleness_seconds + 60),
        ).event(self.task, run)
        self.assertEqual((aged.outcome, aged.state), ("match", "exited"))
        self.assertFalse(aged.stale)

    def test_live_process_with_stale_working_event_reconciles_unknown(self) -> None:
        task = self.task
        run = task["runs"][0]
        self.write("prompt-submitted")

        class LiveTmux:
            def has_session(inner, name):
                return True

            def session_option(inner, name, option):
                return "1" if option == "@asha_managed" else task["task_id"]

            def pane_facts(inner, pane_id):
                return PaneFacts(
                    pane_id, run["pid"], False, None, None,
                    task["tmux"]["session"], task["tmux"]["window"], "fixture",
                )

        class UnreachableTmux:
            def has_session(inner, name):
                raise TmuxError("tmux socket is unreachable")

            def pane_facts(inner, pane_id):
                raise TmuxError("tmux socket is unreachable")

        class NoPidTmux(LiveTmux):
            def pane_facts(inner, pane_id):
                return PaneFacts(
                    pane_id, None, False, None, None,
                    task["tmux"]["session"], task["tmux"]["window"], "fixture",
                )

        class MatchingJj:
            def inspect_workspace(inner, path, name, require_empty=False):
                return WorkspaceIdentity(
                    name, task["jj"]["change_id"], task["jj"]["working_commit_id"],
                    (task["jj"]["base_commit_id"],), "fixture",
                )

        window = self.config.event_staleness_seconds
        stale_now = self._now_after_snapshot(window + 60)

        live = LiveAdapters(
            config=self.config, tmux=LiveTmux(), jj=MatchingJj(), now=stale_now,
        )
        with mock.patch("lib.control.reconcile.harness_api.verify_process", return_value=True):
            result = reconcile_task(task, live)
        # A live process whose only semantic evidence is a stale `working` reads
        # as unknown, not a false positive -- the c5cfa1d2 defect.
        self.assertEqual(result["state"], "unknown")

        for name, tmux in (
            ("tmux-unreachable", UnreachableTmux()),
            ("live-pane-no-pid", NoPidTmux()),
        ):
            with self.subTest(name=name, age="fresh"):
                fresh = LiveAdapters(
                    config=self.config, tmux=tmux, jj=MatchingJj(),
                )
                self.assertEqual(reconcile_task(task, fresh)["state"], "unknown")
            with self.subTest(name=name, age="stale"):
                stale = LiveAdapters(
                    config=self.config, tmux=tmux, jj=MatchingJj(), now=stale_now,
                )
                self.assertEqual(reconcile_task(task, stale)["state"], "unknown")


class Increment4DoctorTests(Increment4Fixture):
    def test_live_delivery_reports_absent_runtime_then_snapshot_age(self) -> None:
        missing = run_doctor(
            self.config, probes={"harness-events": DEFAULT_PROBES["harness-events"]},
        )["probes"][0]
        self.assertEqual(missing["outcome"], "unavailable")
        self.assertIn("does not exist", missing["detail"])

        source = self.root / "source"
        source.mkdir()
        source.chmod(0o755)
        workspace = self.config.workspace_root / "repo-key" / "doctor-event"
        workspace.mkdir(parents=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        task = task_record(
            slug="doctor-event", repository_root=str(source),
            workspace_path=str(workspace),
        )
        self.task_id = task["task_id"]
        self.run_id = task["runs"][0]["run_id"]
        TaskStore(self.config).save(task)
        self.write("tool-completed")

        delivered = run_doctor(
            self.config, probes={"harness-events": DEFAULT_PROBES["harness-events"]},
        )["probes"][0]
        self.assertEqual(delivered["outcome"], "match")
        self.assertIn("readable 1/1", delivered["detail"])
        self.assertIn(self.run_id[:8], delivered["detail"])

    def test_hook_installation_probe_checks_claimed_native_events_read_only(self) -> None:
        handler = (
            Path(__file__).resolve().parents[2]
            / "plugins/session/hooks/handlers/control-event.sh"
        )
        claude = self.home / ".claude"
        codex = self.home / ".codex"
        claude.mkdir()
        codex.mkdir()
        claude_hooks = {}
        for event in (
            "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd",
        ):
            claude_hooks[event] = [{
                "hooks": [{"type": "command", "command": f"{handler} {event}"}],
            }]
        (claude / "settings.json").write_text(
            json.dumps({"hooks": claude_hooks}), encoding="utf-8",
        )
        codex_lines = []
        for event in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
            codex_lines.extend([
                f"[[hooks.{event}]]",
                f"[[hooks.{event}.hooks]]",
                'type = "command"',
                f'command = "env ASHA_HARNESS=codex {handler} {event}"',
            ])
        (codex / "config.toml").write_text(
            "\n".join(codex_lines) + "\n", encoding="utf-8",
        )

        before = {
            path: path.read_bytes()
            for path in (claude / "settings.json", codex / "config.toml")
        }
        result = run_doctor(
            self.config, probes={"hooks": DEFAULT_PROBES["hooks"]},
        )["probes"][0]

        self.assertEqual(result["outcome"], "match")
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in before},
        )

        (claude / "settings.json").write_text('{"hooks":{}}', encoding="utf-8")
        result = run_doctor(
            self.config, probes={"hooks": DEFAULT_PROBES["hooks"]},
        )["probes"][0]
        self.assertEqual(result["outcome"], "missing")
        self.assertIn("claude:SessionEnd", result["detail"])


if __name__ == "__main__":
    unittest.main()
