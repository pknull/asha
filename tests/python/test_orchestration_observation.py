"""Bounded inventory via public CLI, real stores, and fake tmux wire replies."""
from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import time
import unittest
import uuid
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

from lib.control.harness import process_identity
from lib.control.rooms import RoomError, RoomStore, _project_marker
from lib.control.store import SnapshotBudget, StoreError, TaskStore, TransactionCoordinator
from lib.control.tmux import TmuxAdapter
from lib.control.orchestration import cli, coordinator
from lib.control.orchestration.model import (MESSAGE_CONTRACT, message_content_digest,
    chair_sender_identity, record_digest)
from lib.control.orchestration.observation import current_activity, encode_activity
from lib.control.orchestration.store import InitiativeStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.test_control_config_model import task_record
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class ObservationTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self):
        super().setUp()
        self.repo.chmod(0o700)
        self.lines = []
        self.calls = []
        def runner(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, ("\n".join(self.lines) + "\n").encode() if self.lines else b"", b"")
        self.tmux = TmuxAdapter(runner=runner)

    def inventory_line(self, *, pane="%23", session="test", task="", run="",
                       room="", project="", dead=False, session_id="$1"):
        return "\t".join([str(os.getppid()), session, session_id, "room" if room else "work",
                         "@1", pane, str(os.getpid()), "1" if dead else "0", "", "", "1",
                         "1", task, room, run, room, project])

    def task(self, *, dead=False):
        task = task_record(repository_root=str(self.repo),
            workspace_path=str(self.config.control.workspace_root / "repo-key/control-test"))
        task["runs"][0].update(pid=os.getpid(), process_start_identity=process_identity(os.getpid()))
        TaskStore(self.config.control).save(task)
        self.lines.append(self.inventory_line(task=task["task_id"], run=task["runs"][0]["run_id"],
            session=task["tmux"]["session"], dead=dead))
        return task

    def room(self):
        record = {"contract": "asha.room.v1", "room_id": str(uuid.uuid4()), "name": "Example",
                  "slug": "example", "project_id": "example", "project_root": str(self.repo),
                  "project_name": "Example project", "harness": "codex",
                  "tmux": {"session": "example-room", "session_id": "$2", "window": "room", "pane_id": "%24"},
                  "created_at": now_text(), "updated_at": now_text(), "lifecycle": "open", "prompt_digest": "c" * 64}
        RoomStore(self.config.control).create(record)
        self.lines.append(self.inventory_line(pane="%24", session="example-room", room=record["room_id"],
                                              project=_project_marker("example"), session_id="$2"))
        return record

    def test_current_activity_uses_live_tasks_rooms_and_heads_without_history(self):
        task = self.task()
        room = self.room()
        with mock.patch.object(InitiativeStore, "list_initiatives", side_effect=AssertionError("full list forbidden")), \
                mock.patch.object(InitiativeStore, "list_events_snapshot", side_effect=AssertionError("history forbidden")), \
                mock.patch.object(InitiativeStore, "list_plans_snapshot", side_effect=AssertionError("plans forbidden")), \
                mock.patch.object(TaskStore, "list", side_effect=AssertionError("full task list forbidden")), \
                mock.patch.object(RoomStore, "list", side_effect=AssertionError("full room list forbidden")):
            value = current_activity(self.config, tmux=self.tmux)
        found = {r["source"]: r for r in value["rows"]}
        self.assertEqual(found["tasks"]["task_id"], task["task_id"])
        self.assertEqual(found["rooms"]["room_id"], room["room_id"])
        self.assertEqual(found["rooms"]["status"], "open")
        self.assertEqual(found["initiatives"]["initiative_id"], self.initiative_id)
        encoded = encode_activity(value)
        self.assertNotIn(b"acceptance_criteria", encoded)
        self.assertNotIn(b"Execute one node", encoded)
        self.assertNotIn(b"active_plan", encoded)
        self.assertLessEqual(len(encoded), 65536)
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(self.calls[0][1]["timeout"], 2)
        self.assertEqual(value["sources"]["tasks"]["unavailable_records"], 0,
                         "normal transaction lock files are not malformed task records")

    def test_dead_and_mismatched_processes_are_not_actual_live_tasks(self):
        self.task(dead=True)
        value = current_activity(self.config, tmux=self.tmux)
        self.assertFalse(any(r["source"] == "tasks" for r in value["rows"]))
        self.lines[0] = self.lines[0].replace("\t1\t\t\t1\t", "\t0\t\t\t1\t")
        with mock.patch("lib.control.orchestration.observation.verify_process", return_value=False):
            value = current_activity(self.config, tmux=self.tmux)
        self.assertFalse(any(r["source"] == "tasks" for r in value["rows"]))
        self.assertGreater(value["sources"]["tasks"]["unavailable_records"], 0)

    def test_missing_tmux_is_unavailable_not_an_exact_zero(self):
        self.task()
        self.tmux.runner = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, b"", b"error connecting: permission denied")
        value = current_activity(self.config, tmux=self.tmux)
        self.assertFalse(value["complete"])
        self.assertGreater(value["sources"]["tasks"]["unavailable_records"], 0)
        self.assertEqual(value["sources"]["tasks"]["count_kind"], "lower-bound")

    def test_scan_and_row_caps_are_honest_and_do_not_materialize_full_registry(self):
        base = self.initiative()
        for index in range(8):
            value = copy.deepcopy(base)
            value.update(initiative_id=str(uuid.uuid4()), slug=f"draft-{index}", state="draft",
                         active_plan=None, state_revision=0, last_event_sequence=0)
            self.store.save_initiative(value)
        result = current_activity(self.config, tmux=self.tmux, scanned=3, rows=2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        for source in result["sources"].values():
            self.assertLessEqual(source["scanned"], 3)
        self.assertTrue(result["sources"]["initiatives"]["truncated"])
        self.assertTrue(result["sources"]["messages"]["truncated"])
        self.assertEqual(result["sources"]["initiatives"]["count_kind"], "lower-bound")

    def test_deadline_and_external_output_are_bounded(self):
        self.task()
        def slow(argv, **kwargs):
            self.assertLessEqual(kwargs["timeout"], .02)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        self.tmux.runner = slow
        result = current_activity(self.config, tmux=self.tmux, seconds=.02)
        self.assertFalse(result["complete"])
        self.tmux.runner = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"x" * 65537, b"")
        result = current_activity(self.config, tmux=self.tmux)
        self.assertFalse(result["complete"])
        with mock.patch("lib.control.orchestration.observation.time.monotonic", side_effect=lambda: 100):
            budget = SnapshotBudget(deadline=99)
            self.assertFalse(budget.ready())
            self.assertTrue(budget.summary()["truncated"])

    def test_readonly_inventory_does_not_create_locks_or_write_on_negative_reads(self):
        self.task(); self.room()
        root = Path(self.config.control.asha_home)
        before = {str(p): p.stat().st_mtime_ns for p in root.rglob("*")}
        real_open = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT), path)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", side_effect=readonly):
            result = current_activity(self.config, tmux=self.tmux)
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in root.rglob("*")}, before)
        self.assertTrue(any(r["source"] == "tasks" for r in result["rows"]))

    def test_pending_message_ids_without_body_and_pending_decisions(self):
        current = coordinator.claim(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%7"}, tmux=FakeTmux())
        anchor = current["anchor"]
        seat = {"pid": anchor["pane_pid"], "process_start_identity": anchor["process_start_identity"]}
        message = {"contract": MESSAGE_CONTRACT, "initiative_id": self.initiative_id,
                   "message_id": str(uuid.uuid4()), "body": "secret technical context",
                   "content_digest": message_content_digest("secret technical context"),
                   "persisted_at": now_text(),
                   "sender": {"role": "operator-chair", "identity": chair_sender_identity(anchor, seat), "anchor": anchor, "process": seat},
                   "recipient": {key: current[key] for key in ("coordinator_id", "generation", "anchor")}}
        self.store.save_message(self.initiative_id, message)
        self.set_running(self.initiative())
        head = self.initiative()
        updated = copy.deepcopy(head)
        updated.update(state="needs-input", state_revision=head["state_revision"] + 1, updated_at=now_text())
        self.store.save_initiative(updated, expected_digest=record_digest(head))
        result = current_activity(self.config, tmux=self.tmux)
        rows = {row["source"]: row for row in result["rows"]}
        self.assertEqual(rows["messages"]["message_id"], message["message_id"])
        self.assertEqual(rows["messages"]["address_status"], "current")
        self.assertEqual(rows["decisions"]["state"], "needs-input")
        self.assertNotIn(b"secret technical context", encode_activity(result))

    def test_foreign_symlink_and_invalid_names_report_incomplete(self):
        bad = self.config.initiatives_dir / str(uuid.uuid4())
        bad.symlink_to(self.config.initiatives_dir / self.initiative_id)
        result = current_activity(self.config, tmux=self.tmux)
        self.assertGreater(result["sources"]["initiatives"]["unavailable_records"], 0)
        self.assertFalse(result["complete"])

    def inventory_cli(self):
        out = io.StringIO()
        with mock.patch.object(cli, "TmuxAdapter", return_value=self.tmux), redirect_stdout(out):
            code = cli.main(["initiative", "inventory", "--json"], env=self.env)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.getvalue().encode("utf-8")), 65536)
        return json.loads(out.getvalue())

    def assert_unavailable_source(self, result, source):
        self.assertFalse(result["complete"])
        evidence = result["sources"][source]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["unavailable_records"], 1)
        self.assertEqual(evidence["count_kind"], "lower-bound")

    def test_cli_real_transaction_locks_held_and_released_are_readonly_metadata(self):
        task = self.task()
        transactions = TransactionCoordinator(self.config.control)
        locks = (("task", task["task_id"]), ("source", str(self.repo)),
                 ("repository", "repo:" + "a" * 64))
        paths = [self.config.control.tasks_dir / (transactions.lock_key(*lock) + ".lock")
                 for lock in locks]
        with ExitStack() as held:
            for domain, identity in locks:
                held.enter_context(transactions.lock(domain, identity))
            before = [(path.stat(), path.read_bytes()) for path in paths]
            result = self.inventory_cli()
            self.assertTrue(result["sources"]["tasks"]["complete"])
            self.assertEqual(result["sources"]["tasks"]["unavailable_records"], 0)
            self.assertEqual([row["task_id"] for row in result["rows"]
                              if row["source"] == "tasks"], [task["task_id"]])
        real_open = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT), path)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", side_effect=readonly), \
                mock.patch("os.fchmod", side_effect=AssertionError("lock repair forbidden")), \
                mock.patch("os.unlink", side_effect=AssertionError("lock pruning forbidden")):
            result = self.inventory_cli()
        self.assertTrue(result["sources"]["tasks"]["complete"])
        self.assertEqual(result["sources"]["tasks"]["unavailable_records"], 0)
        for path, (metadata, content) in zip(paths, before):
            current = path.stat()
            self.assertEqual((current.st_ino, current.st_mode, current.st_mtime_ns, current.st_ctime_ns),
                             (metadata.st_ino, metadata.st_mode, metadata.st_mtime_ns, metadata.st_ctime_ns))
            self.assertEqual(path.read_bytes(), content)
        bounded = current_activity(self.config, tmux=self.tmux, scanned=1)
        self.assertFalse(bounded["sources"]["tasks"]["complete"])
        self.assertTrue(bounded["sources"]["tasks"]["truncated"])
        self.assertLessEqual(bounded["sources"]["tasks"]["scanned"], 1)

    def test_cli_invalid_transaction_lock_names_remain_unavailable(self):
        task = self.task()
        for name in ("source-" + "a" * 63 + ".lock", "repository-" + "a" * 65 + ".lock",
                     "task-" + "A" * 64 + ".lock", "source-" + "g" * 64 + ".lock",
                     "other-" + "a" * 64 + ".lock", "repository-" + "a" * 64 + ".lock.extra"):
            with self.subTest(name=name):
                path = self.config.control.tasks_dir / name
                path.write_bytes(b""); path.chmod(0o600)
                try:
                    result = self.inventory_cli()
                    self.assert_unavailable_source(result, "tasks")
                    self.assertEqual([row["task_id"] for row in result["rows"]
                                      if row["source"] == "tasks"], [task["task_id"]])
                finally:
                    path.unlink()

    def test_cli_transaction_lock_lookalikes_require_safe_owned_regular_files(self):
        task = self.task()
        target = self.root / "lock-target"
        target.write_bytes(b"untouched"); target.chmod(0o600)
        for domain in ("task", "source", "repository"):
            for kind in ("symlink", "directory", "fifo", "hardlink", "mode", "foreign"):
                with self.subTest(domain=domain, kind=kind):
                    path = self.config.control.tasks_dir / (domain + "-" + "f" * 64 + ".lock")
                    if kind == "symlink":
                        path.symlink_to(target)
                    elif kind == "directory":
                        path.mkdir(mode=0o700)
                    elif kind == "fifo":
                        os.mkfifo(path, 0o600)
                    elif kind == "hardlink":
                        os.link(target, path)
                    else:
                        path.write_bytes(b""); path.chmod(0o644 if kind == "mode" else 0o600)
                    before = path.lstat()
                    real_fstat = os.fstat
                    def metadata(fd):
                        value = real_fstat(fd)
                        if kind == "foreign" and (value.st_dev, value.st_ino) == (before.st_dev, before.st_ino):
                            parts = list(value); parts[4] = os.geteuid() + 1
                            return os.stat_result(parts)
                        return value
                    try:
                        with mock.patch("os.fstat", side_effect=metadata):
                            result = self.inventory_cli()
                        self.assert_unavailable_source(result, "tasks")
                        self.assertEqual([row["task_id"] for row in result["rows"]
                                          if row["source"] == "tasks"], [task["task_id"]])
                        self.assertEqual(path.lstat().st_mode, before.st_mode)
                        self.assertEqual(path.lstat().st_ino, before.st_ino)
                        self.assertEqual(target.read_bytes(), b"untouched")
                    finally:
                        path.rmdir() if kind == "directory" else path.unlink()

    def test_cli_malformed_task_enums_preserve_valid_rows(self):
        task = self.task()
        damaged = copy.deepcopy(task)
        damaged["task_id"] = str(uuid.uuid4())
        path = self.config.control.tasks_dir / (damaged["task_id"] + ".json")
        for field in ("lifecycle", "source.kind", "runs.0.state"):
            for value in ([], {}, None, True, 42, "unsupported"):
                with self.subTest(field=field, value=value):
                    record = copy.deepcopy(damaged)
                    if field == "lifecycle":
                        record["lifecycle"] = value
                    elif field == "source.kind":
                        record["source"]["kind"] = value
                    else:
                        record["runs"][0]["state"] = value
                    path.write_text(json.dumps(record)); path.chmod(0o600)
                    with self.assertRaises(StoreError):
                        TaskStore(self.config.control).peek(damaged["task_id"])
                    result = self.inventory_cli()
                    self.assert_unavailable_source(result, "tasks")
                    self.assertEqual([row["task_id"] for row in result["rows"]
                                      if row["source"] == "tasks"], [task["task_id"]])

    def test_task_snapshot_does_not_suppress_validator_programming_errors(self):
        self.task()
        with mock.patch("lib.control.store.validate_task", side_effect=TypeError("validator bug")):
            with self.assertRaisesRegex(TypeError, "validator bug"):
                TaskStore(self.config.control).bounded_snapshots(
                    SnapshotBudget(deadline=time.monotonic() + 10))

    def test_cli_malformed_room_values_preserve_valid_rows(self):
        room = self.room()
        damaged = copy.deepcopy(room)
        damaged["room_id"] = str(uuid.uuid4())
        path = RoomStore(self.config.control).root / (damaged["room_id"] + ".json")
        for field, value in (("lifecycle", []), ("lifecycle", {}), ("lifecycle", None),
                             ("harness", []), ("tmux", [])):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(damaged)
                record[field] = value
                path.write_text(json.dumps(record))
                path.chmod(0o600)
                with self.assertRaises(RoomError):
                    RoomStore(self.config.control).read(damaged["room_id"])
                result = self.inventory_cli()
                self.assert_unavailable_source(result, "rooms")
                rooms = [row for row in result["rows"] if row["source"] == "rooms"]
                self.assertEqual([row["room_id"] for row in rooms], [room["room_id"]])
                self.assertEqual(rooms[0]["status"], "open")

    def test_cli_malformed_initiative_values_preserve_valid_rows(self):
        head = self.initiative()
        damaged = copy.deepcopy(head)
        damaged["initiative_id"] = str(uuid.uuid4())
        directory = self.config.initiatives_dir / damaged["initiative_id"]
        directory.mkdir(mode=0o700)
        path = directory / "initiative.json"
        for field, value in (("state", []), ("state", {}), ("state", None),
                             ("contract", []), ("scope", {"kind": []}),
                             ("active_plan", [])):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(damaged)
                record[field] = value
                path.write_text(json.dumps(record))
                path.chmod(0o600)
                with self.assertRaises(StoreError):
                    self.store.peek(damaged["initiative_id"])
                result = self.inventory_cli()
                self.assert_unavailable_source(result, "initiatives")
                self.assertEqual([row["initiative_id"] for row in result["rows"]
                                  if row["source"] == "initiatives"], [self.initiative_id])
                for source in ("decisions", "messages", "coordinators"):
                    self.assertFalse(result["sources"][source]["complete"])

    def test_cli_malformed_nested_approval_values_preserve_valid_rows(self):
        approval = self.store.list_approvals_snapshot(self.initiative_id)[0]
        pending = copy.deepcopy(approval)
        pending.update(request_id=str(uuid.uuid4()), state="requested")
        pending.pop("decided_by", None)
        self.store.save_approval(self.initiative_id, pending)
        damaged = copy.deepcopy(pending)
        damaged["request_id"] = str(uuid.uuid4())
        path = (self.config.initiatives_dir / self.initiative_id / "approvals"
                / (damaged["request_id"] + ".json"))
        for field, value in (("state", []), ("state", {}),
                             ("requested_by", {"actor_kind": [], "actor_id": pending["actor_id"]}),
                             ("requested_by", [])):
            with self.subTest(field=field, value=value):
                record = copy.deepcopy(damaged)
                record[field] = value
                path.write_text(json.dumps(record))
                path.chmod(0o600)
                result = self.inventory_cli()
                self.assert_unavailable_source(result, "decisions")
                self.assertEqual([row["request_id"] for row in result["rows"]
                                  if row["source"] == "decisions"], [pending["request_id"]])
                self.assertTrue(any(row["source"] == "initiatives" for row in result["rows"]))

    def test_cli_serialized_byte_and_row_caps_include_escaping(self):
        base = self.initiative()
        for index in range(12):
            value = copy.deepcopy(base)
            value.update(initiative_id=str(uuid.uuid4()), slug=f"unicode-{index}", state="draft",
                         label="界" * 60, active_plan=None, state_revision=0, last_event_sequence=0)
            self.store.save_initiative(value)
        result = current_activity(self.config, tmux=self.tmux, byte_limit=4096)
        self.assertLessEqual(len(encode_activity(result)), 4096)
        self.assertTrue(result["truncated"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli._initiative_command(["inventory", "--rows", "1", "--json"], self.env, tmux=self.tmux)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.getvalue().encode()), 65536)
        self.assertEqual(len(json.loads(out.getvalue())["rows"]), 1)
        with self.assertRaises(ValueError):
            current_activity(self.config, tmux=self.tmux, rows=51)


if __name__ == "__main__":
    unittest.main()
