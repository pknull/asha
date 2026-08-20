from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.store import (
    StoreCommittedError, StoreError, TaskStore, TransactionCoordinator,
    task_digest,
)
from tests.python.test_control_config_model import task_record


class ControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        self.store = TaskStore(self.config)

    @staticmethod
    def task_lock_id(task_id: str) -> str:
        return TransactionCoordinator.lock_key("task", task_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, **kwargs) -> dict:
        slug = kwargs.get("slug", "control-test")
        kwargs.setdefault("repository_root", str(self.root / "repositories/source"))
        kwargs.setdefault("workspace_path", str(self.config.workspace_root / "repo-key" / slug))
        return task_record(**kwargs)

    def child_line(self, process: subprocess.Popen[str], timeout: float = 5) -> str:
        """Read one signaled child line without TextIO read-ahead hiding readiness."""
        deadline = time.monotonic() + timeout
        data = bytearray()
        fd = process.stdout.fileno()
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "child did not reach the signaled boundary")
            ready, _, _ = select.select([fd], [], [], remaining)
            self.assertTrue(ready, "child did not reach the signaled boundary")
            chunk = os.read(fd, 1)
            self.assertTrue(chunk, "child closed output before signaling")
            data.extend(chunk)
        return data.decode().strip()

    def test_save_is_one_record_per_task_with_exact_modes_and_read_round_trip(self) -> None:
        record = self.record()

        path = self.store.save(record)

        self.assertEqual(path, self.config.tasks_dir / f"{record['task_id']}.json")
        self.assertEqual(self.store.read(record["task_id"]), record)
        for directory in (
            self.config.tasks_dir.parent.parent,
            self.config.tasks_dir.parent,
            self.config.tasks_dir,
            self.config.runtime_dir,
            self.config.runtime_dir / "tasks",
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        lock_id = self.task_lock_id(record["task_id"])
        durable_lock = self.config.tasks_dir / f"{lock_id}.lock"
        self.assertEqual(stat.S_IMODE(durable_lock.stat().st_mode), 0o600)
        lock = self.config.runtime_dir / "tasks" / f"{lock_id}.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_private_state_root_accepts_group_readable_asha_parent_without_read_side_effects(self) -> None:
        state = self.root / "live-state"
        shared = state / "asha"
        state.mkdir(mode=0o700)
        shared.mkdir(mode=0o750)
        shared.chmod(0o750)
        config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(state),
            "XDG_DATA_HOME": str(self.root / "live-data"),
            "XDG_RUNTIME_DIR": str(self.root / "live-runtime"),
        })
        store = TaskStore(config)
        before = sorted(
            (str(path.relative_to(state)), path.stat().st_mode)
            for path in state.rglob("*")
        )

        self.assertEqual(store.list(), [])

        after = sorted(
            (str(path.relative_to(state)), path.stat().st_mode)
            for path in state.rglob("*")
        )
        self.assertEqual(after, before)
        self.assertFalse((shared / "control").exists())

        store.save(task_record(
            slug="shared-state",
            repository_root=str(self.root / "repositories/source"),
            workspace_path=str(config.workspace_root / "repo-key/shared-state"),
        ))
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE((shared / "control").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((shared / "control/tasks").stat().st_mode), 0o700)

    def test_validation_happens_before_any_write(self) -> None:
        record = self.record()
        record["contract"] = "wrong"
        with self.assertRaisesRegex(StoreError, "contract"):
            self.store.save(record)
        self.assertFalse(self.config.tasks_dir.exists())
        self.assertFalse(self.config.runtime_dir.exists())

    def test_existing_record_and_lock_symlinks_are_rejected(self) -> None:
        record = self.record()
        self.store.save(record)
        record_path = self.config.tasks_dir / f"{record['task_id']}.json"
        record_path.unlink()
        record_path.symlink_to(self.root / "outside-record")
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.save(record)

        record_path.unlink()
        lock = self.config.runtime_dir / "tasks" / f"{self.task_lock_id(record['task_id'])}.lock"
        lock.unlink()
        lock.symlink_to(self.root / "outside-lock")
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.save(record)

    def test_symlinked_state_directory_is_rejected(self) -> None:
        target = self.root / "outside"
        target.mkdir()
        (self.root / "state").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.save(self.record())

    def test_dangling_registry_symlink_is_rejected_even_when_listing(self) -> None:
        self.config.tasks_dir.parent.mkdir(parents=True)
        for directory in (
            self.config.tasks_dir.parents[2],
            self.config.tasks_dir.parents[1],
            self.config.tasks_dir.parent,
        ):
            directory.chmod(0o700)
        self.config.tasks_dir.symlink_to(self.root / "absent", target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.list()

    def test_failed_replace_preserves_old_complete_record_and_removes_temporary(self) -> None:
        record = self.record()
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:01:00Z"
        changed["runs"][0]["evidence"] = "updated evidence"
        changed["runs"][0]["evidence_at"] = "2026-08-14T18:01:00Z"

        with mock.patch("lib.control.store.os.replace", side_effect=OSError("injected")):
            with self.assertRaisesRegex(StoreError, "atomic replace"):
                self.store.save(changed, expected_digest=task_digest(record))

        self.assertEqual(self.store.read(record["task_id"]), record)
        self.assertEqual(list(self.config.tasks_dir.glob(".*.tmp.*")), [])

    def test_save_fsyncs_record_and_containing_directory(self) -> None:
        with mock.patch("lib.control.store.os.fsync", wraps=os.fsync) as fsync:
            self.store.save(self.record())
        # Every created directory and its parent are synced, followed by the
        # record and the containing task directory.
        self.assertGreaterEqual(fsync.call_count, 12)

    def test_post_replace_directory_fsync_failure_is_a_committed_error(self) -> None:
        record = self.record()
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:02:00Z"
        changed["runs"][0]["evidence"] = "new visible content"
        changed["runs"][0]["evidence_at"] = changed["updated_at"]
        real_fsync = os.fsync
        real_replace = os.replace
        replaced = False

        def track_replace(*args, **kwargs) -> None:
            nonlocal replaced
            real_replace(*args, **kwargs)
            replaced = True

        def fail_directory(fd: int) -> None:
            if replaced and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory sync failure")
            real_fsync(fd)

        with (
            mock.patch("lib.control.store.os.replace", side_effect=track_replace),
            mock.patch("lib.control.store.os.fsync", side_effect=fail_directory),
        ):
            with self.assertRaisesRegex(
                StoreCommittedError, "visible but durability is indeterminate"
            ):
                self.store.save(changed, expected_digest=task_digest(record))
        self.assertEqual(self.store.read(record["task_id"]), changed)

    def test_pre_replace_io_failure_is_wrapped_and_does_not_commit(self) -> None:
        record = self.record()
        with mock.patch("lib.control.store.os.write", side_effect=OSError("injected write")):
            with self.assertRaises(StoreError) as caught:
                self.store.save(record)
        self.assertNotIsInstance(caught.exception, StoreCommittedError)
        self.assertIn("before atomic replace", str(caught.exception))
        self.assertFalse((self.config.tasks_dir / f"{record['task_id']}.json").exists())

    def test_store_binds_workspace_to_configured_root_and_source_repository_before_writes(self) -> None:
        unsafe = (
            self.record(workspace_path=str(self.root / "elsewhere/task")),
            self.record(
                repository_root=str(self.config.workspace_root / "nested-source"),
                workspace_path=str(self.config.workspace_root / "repo-key/task"),
            ),
            self.record(workspace_path=str(self.config.workspace_root)),
        )
        for record in unsafe:
            with self.subTest(record=record), self.assertRaises(StoreError):
                self.store.save(record)
        self.assertFalse(self.config.tasks_dir.exists())
        self.assertFalse(self.config.runtime_dir.exists())

    def test_store_rejects_existing_workspace_symlink_component_before_writes(self) -> None:
        self.config.workspace_root.mkdir(parents=True)
        current = self.config.workspace_root
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        outside = self.root / "outside-workspaces"
        outside.mkdir()
        outside.chmod(0o700)
        (self.config.workspace_root / "repo-key").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "symlink|resolved canonical"):
            self.store.save(self.record())
        self.assertFalse(self.config.tasks_dir.exists())
        self.assertFalse(self.config.runtime_dir.exists())

    def test_store_rechecks_nonsticky_writable_ancestors_during_each_operation(self) -> None:
        for unsafe_kind in ("state", "runtime", "workspace"):
            with self.subTest(unsafe_kind=unsafe_kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                state = root / "state"
                data = root / "data"
                runtime = root / "runtime"
                for directory in (home, state, data, runtime):
                    directory.mkdir(mode=0o700)
                config = load_config({
                    "HOME": str(home),
                    "ASHA_CONFIG": str(root / "missing.json"),
                    "XDG_STATE_HOME": str(state),
                    "XDG_DATA_HOME": str(data),
                    "XDG_RUNTIME_DIR": str(runtime),
                })
                # Remove the TemporaryDirectory's implicit private boundary;
                # the subsequently widened child must then be rejected.
                root.chmod(0o755)
                unsafe = {"state": state, "runtime": runtime, "workspace": data}[unsafe_kind]
                unsafe.chmod(0o777)
                record = task_record(
                    repository_root=str(root / "repositories/source"),
                    workspace_path=str(config.workspace_root / "repo-key/task"),
                )
                with self.assertRaisesRegex(StoreError, "writable non-sticky ancestor"):
                    TaskStore(config).save(record)

    def test_store_descriptor_traversal_rechecks_namespace_ownership(self) -> None:
        with mock.patch(
            "lib.control.store.namespace_safety_step",
            return_value=("ancestor is not owned by root or the effective user", False),
        ):
            with self.assertRaisesRegex(StoreError, "not owned"):
                self.store.save(self.record())

    def test_hard_linked_existing_lock_is_rejected_without_changing_its_mode(self) -> None:
        record = self.record()
        self.store.save(record)
        lock = self.config.runtime_dir / "tasks" / f"{self.task_lock_id(record['task_id'])}.lock"
        lock.unlink()
        target = self.root / "outside-lock"
        target.write_text("owned elsewhere")
        target.chmod(0o600)
        os.link(target, lock)
        with self.assertRaisesRegex(StoreError, "link count"):
            self.store.save(record)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_pinned_task_directory_resists_parent_symlink_swap_during_replace(self) -> None:
        record = self.record()
        outside = self.root / "outside-state"
        outside.mkdir()
        real_replace = os.replace
        swapped = False

        def swap_parent_then_replace(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                managed = self.config.tasks_dir.parents[1]
                pinned = managed.with_name("asha-pinned")
                managed.rename(pinned)
                managed.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_replace(*args, **kwargs)

        with mock.patch("lib.control.store.os.replace", side_effect=swap_parent_then_replace):
            self.store.save(record)

        self.assertTrue(swapped)
        self.assertFalse(any(outside.rglob("*.json")))
        pinned_record = self.root / "state/asha-pinned/control/tasks" / f"{record['task_id']}.json"
        self.assertTrue(pinned_record.is_file())

    def test_production_lock_reports_deterministic_cross_process_contention(self) -> None:
        record = self.record()
        self.store.save(record)
        lock = self.config.runtime_dir / "tasks" / f"{self.task_lock_id(record['task_id'])}.lock"
        code = """
import fcntl, os, sys
fd = os.open(os.environ['LOCK'], os.O_RDWR | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('contended')
else:
    print('acquired')
finally:
    os.close(fd)
"""
        env = os.environ.copy()
        env.update({
            "LOCK": str(lock),
        })
        with self.store._locked(record["task_id"]):
            held = subprocess.run(
                [sys.executable, "-c", code], env=env, capture_output=True, text=True,
                timeout=5, check=True,
            )
            self.assertEqual(held.stdout.strip(), "contended")
        released = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True,
            timeout=5, check=True,
        )
        self.assertEqual(released.stdout.strip(), "acquired")

    def test_public_save_blocks_on_the_production_lock_until_release(self) -> None:
        record = self.record()
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:02:00Z"
        changed["runs"][0]["evidence"] = "serialized update"
        changed["runs"][0]["evidence_at"] = changed["updated_at"]
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TASK_ID": record["task_id"],
            "TASK_JSON": json.dumps(changed),
            "DIGEST": task_digest(record),
        })
        holder_code = """
import os, sys
from lib.control.config import load_config
from lib.control.store import TaskStore
s = TaskStore(load_config(os.environ))
with s._locked(os.environ['TASK_ID']):
    print('held', flush=True)
    if sys.stdin.readline().strip() != 'release': raise SystemExit(3)
print('released', flush=True)
"""
        save_code = """
import json, os
from lib.control.config import load_config
from lib.control.store import TaskStore
def boundary(): print('at-lock', flush=True)
s = TaskStore(load_config(os.environ), lock_wait_hook=boundary)
s.save(json.loads(os.environ['TASK_JSON']), expected_digest=os.environ['DIGEST'])
print('completed', flush=True)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        saver = None

        try:
            self.assertEqual(self.child_line(holder), "held")
            saver = subprocess.Popen(
                [sys.executable, "-c", save_code], env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(self.child_line(saver), "at-lock")
            readable, _, _ = select.select([saver.stdout], [], [], 0)
            self.assertEqual(readable, [])
            self.assertIsNone(saver.poll())
            holder.stdin.write("release\n")
            holder.stdin.flush()
            self.assertEqual(holder.wait(timeout=5), 0, holder.stderr.read())
            self.assertEqual(self.child_line(saver), "completed")
            self.assertEqual(saver.wait(timeout=5), 0, saver.stderr.read())
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=5)
            if saver is not None and saver.poll() is None:
                saver.kill()
                saver.wait(timeout=5)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                if stream is not None:
                    stream.close()
            if saver is not None:
                for stream in (saver.stdout, saver.stderr):
                    if stream is not None:
                        stream.close()

    def test_state_registry_lock_serializes_updates_across_distinct_runtime_roots(self) -> None:
        record = self.record()
        self.store.save(record)
        first = json.loads(json.dumps(record))
        first["updated_at"] = "2026-08-14T18:02:00Z"
        first["runs"][0]["evidence"] = "first runtime update"
        first["runs"][0]["evidence_at"] = first["updated_at"]
        second = json.loads(json.dumps(first))
        second["runs"][0]["evidence"] = "second runtime update"
        code = """
import json, os, sys
from lib.control.config import load_config
from lib.control.store import StoreError, TaskStore
def waiting(): print('registry-waiting', flush=True)
def acquired():
    print('registry-acquired', flush=True)
    if os.environ['HOLD'] == '1' and sys.stdin.readline().strip() != 'release':
        raise SystemExit(4)
s = TaskStore(load_config(os.environ), registry_lock_wait_hook=waiting,
              registry_lock_acquired_hook=acquired)
try:
    s.save(json.loads(os.environ['TASK_JSON']), expected_digest=os.environ['DIGEST'])
except StoreError as exc:
    print('error:' + str(exc), flush=True)
else:
    print('success', flush=True)
"""
        base_env = os.environ.copy()
        base_env.update({
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "DIGEST": task_digest(record),
        })

        def start(payload: dict, runtime: str, hold: bool) -> subprocess.Popen[str]:
            env = dict(base_env)
            env.update({
                "TASK_JSON": json.dumps(payload),
                "XDG_RUNTIME_DIR": str(self.root / runtime),
                "HOLD": "1" if hold else "0",
            })
            return subprocess.Popen(
                [sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )

        one = start(first, "runtime-one", True)
        two = None
        try:
            self.assertEqual(self.child_line(one), "registry-waiting")
            self.assertEqual(self.child_line(one), "registry-acquired")
            two = start(second, "runtime-two", False)
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import fcntl,os,sys; "
                        "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
                        "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                        "\nexcept BlockingIOError: print('contended')"
                        "\nelse: print('acquired')"
                    ),
                    str(self.config.tasks_dir),
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            self.assertEqual(probe.stdout.strip(), "contended")
            readable, _, _ = select.select([two.stdout], [], [], 0)
            self.assertEqual(readable, [])
            self.assertIsNone(two.poll())

            one.stdin.write("release\n")
            one.stdin.flush()
            self.assertEqual(self.child_line(one), "success")
            self.assertEqual(one.wait(timeout=5), 0, one.stderr.read())
            self.assertEqual(self.child_line(two), "registry-waiting")
            self.assertEqual(self.child_line(two), "registry-acquired")
            self.assertIn("digest mismatch", self.child_line(two))
            self.assertEqual(two.wait(timeout=5), 0, two.stderr.read())
            self.assertEqual(self.store.read(record["task_id"]), first)
        finally:
            for process in (one, two):
                if process is None:
                    continue
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def test_peek_ignores_registry_lock_without_allocating_runtime_state_and_rejects_malformed(self) -> None:
        record = self.record()
        path = self.store.save(record)
        peek_runtime = self.root / "peek-runtime"
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(peek_runtime),
            "TASKS_DIR": str(self.config.tasks_dir),
            "TASK_ID": record["task_id"],
        })
        holder_code = """
import fcntl, os, sys
fd = os.open(os.environ['TASKS_DIR'], os.O_RDONLY | os.O_DIRECTORY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    print('held', flush=True)
    if sys.stdin.readline().strip() != 'release': raise SystemExit(3)
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
"""
        peek_code = """
import json, os
from lib.control.config import load_config
from lib.control.store import TaskStore
task = TaskStore(load_config(os.environ)).peek(os.environ['TASK_ID'])
print(json.dumps(task, sort_keys=True), flush=True)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        peeker = None
        try:
            self.assertEqual(self.child_line(holder), "held")
            peeker = subprocess.Popen(
                [sys.executable, "-c", peek_code], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(json.loads(self.child_line(peeker, timeout=2)), record)
            self.assertEqual(peeker.wait(timeout=2), 0, peeker.stderr.read())
            self.assertIsNone(holder.poll())
            self.assertFalse(peek_runtime.exists())

            holder.stdin.write("release\n")
            holder.stdin.flush()
            self.assertEqual(holder.wait(timeout=5), 0, holder.stderr.read())

            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(StoreError, "invalid JSON"):
                TaskStore(load_config(env)).peek(record["task_id"])
            self.assertFalse(peek_runtime.exists())
        finally:
            for process in (holder, peeker):
                if process is None:
                    continue
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def test_peek_missing_registry_is_read_only_and_invalid_ids_are_store_errors(self) -> None:
        config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "peek-state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "peek-runtime"),
        })
        store = TaskStore(config)

        with self.assertRaisesRegex(StoreError, "not found"):
            store.peek(str(uuid.uuid4()))
        with self.assertRaises(StoreError):
            store.peek("not-a-canonical-task-id")

        self.assertFalse(config.tasks_dir.exists())
        self.assertFalse(config.runtime_dir.exists())

    def test_first_use_mkdir_eexist_race_reopens_and_validates_without_chmod(self) -> None:
        self.config.tasks_dir.parents[2].mkdir(parents=True, mode=0o700)
        self.config.tasks_dir.parents[2].chmod(0o700)
        real_mkdir = os.mkdir
        real_fsync = os.fsync
        raced: list[Path] = []
        race_inodes: list[int] = []
        synced_inodes: list[int] = []

        def losing_mkdir(path, mode=0o777, *, dir_fd=None):
            if not raced:
                race_inodes.append(os.fstat(dir_fd).st_ino)
                real_mkdir(path, 0o700, dir_fd=dir_fd)
                child = os.open(path, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
                try:
                    race_inodes.append(os.fstat(child).st_ino)
                finally:
                    os.close(child)
                raced.append(Path(path))
                raise FileExistsError("simulated concurrent mkdir winner")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def tracking_fsync(fd: int) -> None:
            synced_inodes.append(os.fstat(fd).st_ino)
            real_fsync(fd)

        with (
            mock.patch("lib.control.store.os.mkdir", side_effect=losing_mkdir),
            mock.patch("lib.control.store.os.fsync", side_effect=tracking_fsync),
        ):
            self.store.save(self.record())
        self.assertTrue(raced)
        self.assertTrue(set(race_inodes).issubset(synced_inodes))

        other = self.record(slug="bad-race-mode")
        runtime = self.root / "runtime-bad"
        config = load_config({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state-bad"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(runtime),
        })
        config.tasks_dir.parents[1].mkdir(parents=True, mode=0o700)
        config.tasks_dir.parents[2].chmod(0o700)
        config.tasks_dir.parents[1].chmod(0o700)
        raced.clear()

        def unsafe_winner(path, mode=0o777, *, dir_fd=None):
            if not raced:
                real_mkdir(path, 0o777, dir_fd=dir_fd)
                os.chmod(path, 0o777, dir_fd=dir_fd, follow_symlinks=False)
                raced.append(Path(path))
                raise FileExistsError("simulated unsafe winner")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch("lib.control.store.os.mkdir", side_effect=unsafe_winner):
            with self.assertRaisesRegex(StoreError, "writable non-sticky|mode 0700"):
                TaskStore(config).save(other)
        self.assertEqual(stat.S_IMODE(config.tasks_dir.parent.stat().st_mode), 0o777)

    def test_directory_durability_failure_is_controlled_and_retry_resyncs_visible_pair(self) -> None:
        real_fsync = os.fsync
        for failure_side in ("child", "parent"):
            with self.subTest(failure_side=failure_side), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                home = root / "home"
                home.mkdir(mode=0o700)
                config = load_config({
                    "HOME": str(home),
                    "ASHA_CONFIG": str(root / "missing.json"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                })
                store = TaskStore(config)
                record = task_record(
                    repository_root=str(root / "repositories/source"),
                    workspace_path=str(config.workspace_root / "repo-key/control-test"),
                )
                child_path = root / "state"
                parent_inode = root.stat().st_ino
                injected: list[int] = []

                def fail_first_pair_sync(fd: int) -> None:
                    inode = os.fstat(fd).st_ino
                    if child_path.exists():
                        child_inode = child_path.stat().st_ino
                        target = child_inode if failure_side == "child" else parent_inode
                        if inode == target and not injected:
                            injected.append(inode)
                            raise OSError("injected directory durability failure")
                    real_fsync(fd)

                with mock.patch("lib.control.store.os.fsync", side_effect=fail_first_pair_sync):
                    with self.assertRaisesRegex(
                        StoreError, "cannot establish Control directory durability"
                    ):
                        store.save(record)
                self.assertTrue(injected)
                self.assertTrue(child_path.is_dir())
                self.assertFalse((config.tasks_dir / f"{record['task_id']}.json").exists())

                child_inode = child_path.stat().st_ino
                retry_syncs: list[int] = []

                def track_retry(fd: int) -> None:
                    retry_syncs.append(os.fstat(fd).st_ino)
                    real_fsync(fd)

                with mock.patch("lib.control.store.os.fsync", side_effect=track_retry):
                    store.save(record)
                self.assertIn(
                    (child_inode, parent_inode),
                    list(zip(retry_syncs, retry_syncs[1:])),
                )

    def test_existing_update_requires_fresh_digest_and_legal_identity_preserving_transition(self) -> None:
        record = self.record()
        self.store.save(record)
        digest = task_digest(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:02:00Z"
        changed["runs"][0]["evidence"] = "new bounded evidence"
        changed["runs"][0]["evidence_at"] = "2026-08-14T18:02:00Z"

        with self.assertRaisesRegex(StoreError, "expected digest is required"):
            self.store.save(changed)
        with self.assertRaisesRegex(StoreError, "digest mismatch"):
            self.store.save(changed, expected_digest="0" * 64)
        self.assertEqual(self.store.read(record["task_id"]), record)

        self.store.save(changed, expected_digest=digest)
        self.assertEqual(self.store.read(record["task_id"]), changed)

    def test_legal_creation_and_run_transitions_bind_deferred_identities_once(self) -> None:
        creating = self.record()
        initial_run = json.loads(json.dumps(creating["runs"][0]))
        creating["lifecycle"] = "creating"
        creating["jj"]["change_id"] = None
        creating["jj"]["working_commit_id"] = None
        creating["runs"] = []
        self.store.save(creating)

        running = json.loads(json.dumps(creating))
        running["lifecycle"] = "running"
        running["updated_at"] = "2026-08-14T18:01:00Z"
        running["jj"]["change_id"] = "k" * 32
        running["jj"]["working_commit_id"] = "c" * 40
        running["runs"] = [initial_run]
        self.store.save(running, expected_digest=task_digest(creating))

        working = json.loads(json.dumps(running))
        working["updated_at"] = "2026-08-14T18:02:00Z"
        working["runs"][0]["state"] = "working"
        working["runs"][0]["harness_session_id"] = "session-123"
        working["runs"][0]["evidence"] = "verified prompt submission"
        working["runs"][0]["evidence_at"] = "2026-08-14T18:02:00Z"
        self.store.save(working, expected_digest=task_digest(running))
        self.assertEqual(self.store.read(working["task_id"]), working)
        drifted = json.loads(json.dumps(working))
        drifted["runs"][0]["harness_session_id"] = "different-session"
        with self.assertRaisesRegex(StoreError, "harness_session_id"):
            self.store.save(drifted, expected_digest=task_digest(working))

    def test_running_can_finish_atomically_as_ended_or_failed(self) -> None:
        for terminal, lifecycle in (("exited", "ended"), ("failed", "failed")):
            with self.subTest(terminal=terminal):
                record = self.record(slug=f"finish-{terminal}")
                self.store.save(record)
                changed = json.loads(json.dumps(record))
                changed["lifecycle"] = lifecycle
                changed["updated_at"] = "2026-08-14T18:03:00Z"
                changed["runs"][0]["state"] = terminal
                changed["runs"][0]["evidence_at"] = changed["updated_at"]
                self.store.save(changed, expected_digest=task_digest(record))
                self.assertEqual(self.store.read(record["task_id"]), changed)

    def test_running_can_fail_while_preserving_a_live_run(self) -> None:
        record = self.record()
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["lifecycle"] = "failed"
        changed["updated_at"] = "2026-08-14T18:03:00Z"
        changed["runs"][0]["state"] = "working"
        changed["runs"][0]["evidence"] = "controller failed after launch; run preserved"
        changed["runs"][0]["evidence_at"] = changed["updated_at"]
        self.store.save(changed, expected_digest=task_digest(record))
        self.assertEqual(self.store.read(record["task_id"]), changed)

        exited = json.loads(json.dumps(changed))
        exited["updated_at"] = "2026-08-14T18:04:00Z"
        exited["runs"][0]["state"] = "exited"
        exited["runs"][0]["evidence"] = "preserved run later exited normally"
        exited["runs"][0]["evidence_at"] = exited["updated_at"]
        self.store.save(exited, expected_digest=task_digest(changed))
        self.assertEqual(self.store.read(record["task_id"]), exited)

    def test_creating_and_running_may_append_a_starting_run_while_atomically_failing(self) -> None:
        creating = self.record(slug="creating-launch-failure")
        launched = json.loads(json.dumps(creating["runs"][0]))
        creating["lifecycle"] = "creating"
        creating["jj"]["change_id"] = None
        creating["jj"]["working_commit_id"] = None
        creating["runs"] = []
        self.store.save(creating)

        failed_after_launch = json.loads(json.dumps(creating))
        failed_after_launch["lifecycle"] = "failed"
        failed_after_launch["updated_at"] = "2026-08-14T18:02:00Z"
        failed_after_launch["jj"]["change_id"] = "k" * 32
        failed_after_launch["jj"]["working_commit_id"] = "c" * 40
        launched["evidence"] = "process started before controller failure"
        launched["evidence_at"] = failed_after_launch["updated_at"]
        failed_after_launch["runs"] = [launched]
        self.store.save(failed_after_launch, expected_digest=task_digest(creating))
        self.assertEqual(self.store.read(creating["task_id"]), failed_after_launch)

        running = self.record(slug="sequential-launch-failure")
        self.store.save(running)
        failed_sequential = json.loads(json.dumps(running))
        failed_sequential["lifecycle"] = "failed"
        failed_sequential["updated_at"] = "2026-08-14T18:03:00Z"
        failed_sequential["runs"][0]["state"] = "exited"
        failed_sequential["runs"][0]["evidence_at"] = failed_sequential["updated_at"]
        next_run = json.loads(json.dumps(failed_sequential["runs"][0]))
        next_run["run_id"] = str(uuid.uuid4())
        next_run["pane_id"] = "%101"
        next_run["state"] = "starting"
        next_run["evidence"] = "sequential process started before controller failure"
        failed_sequential["runs"].append(next_run)
        self.store.save(failed_sequential, expected_digest=task_digest(running))
        self.assertEqual(self.store.read(running["task_id"]), failed_sequential)

    def test_terminal_tasks_cannot_append_runs_but_running_tasks_can_append_sequentially(self) -> None:
        for lifecycle in ("failed", "ended", "archived"):
            record = self.record(slug=f"terminal-{lifecycle}")
            record["lifecycle"] = lifecycle
            record["runs"][0]["state"] = "exited"
            self.store.save(record)
            changed = json.loads(json.dumps(record))
            changed["updated_at"] = "2026-08-14T18:04:00Z"
            added = json.loads(json.dumps(changed["runs"][0]))
            added["run_id"] = str(uuid.uuid4())
            added["pane_id"] = "%99"
            added["state"] = "starting"
            added["evidence_at"] = changed["updated_at"]
            changed["runs"].append(added)
            with self.subTest(lifecycle=lifecycle), self.assertRaises(StoreError):
                self.store.save(changed, expected_digest=task_digest(record))

        running = self.record(slug="sequential-run")
        self.store.save(running)
        sequential = json.loads(json.dumps(running))
        sequential["updated_at"] = "2026-08-14T18:04:00Z"
        sequential["runs"][0]["state"] = "exited"
        sequential["runs"][0]["evidence_at"] = sequential["updated_at"]
        added = json.loads(json.dumps(sequential["runs"][0]))
        added["run_id"] = str(uuid.uuid4())
        added["pane_id"] = "%100"
        added["state"] = "starting"
        sequential["runs"].append(added)
        self.store.save(sequential, expected_digest=task_digest(running))
        self.assertEqual(self.store.read(running["task_id"]), sequential)

    def test_update_rejects_malformed_current_record_identity_drift_and_illegal_transitions(self) -> None:
        record = self.record()
        path = self.store.save(record)
        digest = task_digest(record)
        variants = []
        for mutate in (
            lambda item: item.__setitem__("slug", "changed-slug"),
            lambda item: item.__setitem__("lifecycle", "creating"),
            lambda item: item["runs"][0].__setitem__("pid", 99999),
            lambda item: item.__setitem__("runs", []),
        ):
            changed = json.loads(json.dumps(record))
            changed["updated_at"] = "2026-08-14T18:03:00Z"
            mutate(changed)
            variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed), self.assertRaises(StoreError):
                self.store.save(changed, expected_digest=digest)

        path.write_text("not json")
        path.chmod(0o600)
        with self.assertRaisesRegex(StoreError, "invalid JSON"):
            self.store.save(record, expected_digest=digest)

    def test_update_rejects_duplicate_keys_in_current_record(self) -> None:
        record = self.record()
        path = self.store.save(record)
        raw = path.read_text()
        path.write_text('{"task_id":' + json.dumps(record["task_id"]) + ',' + raw[1:])
        path.chmod(0o600)
        with self.assertRaisesRegex(StoreError, "duplicate JSON key"):
            self.store.save(record, expected_digest=task_digest(record))

    def test_update_rejects_illegal_terminal_run_transition(self) -> None:
        record = self.record()
        record["lifecycle"] = "ended"
        record["runs"][0]["state"] = "exited"
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:04:00Z"
        changed["runs"][0]["state"] = "working"
        with self.assertRaises(StoreError):
            self.store.save(changed, expected_digest=task_digest(record))

    def test_missing_reads_do_not_create_persistent_lock_files(self) -> None:
        self.store.save(self.record())
        locks = self.config.runtime_dir / "tasks"
        before = {path.name for path in locks.iterdir()}
        for _ in range(5):
            with self.assertRaisesRegex(StoreError, "not found"):
                self.store.read(str(uuid.uuid4()))
        self.assertEqual({path.name for path in locks.iterdir()}, before)

    def test_save_snapshots_mutable_input_before_validation(self) -> None:
        record = self.record()
        expected_label = record["label"]
        from lib.control import store as store_module
        real_validate = store_module.validate_task

        def validate_then_mutate(value):
            result = real_validate(value)
            record["label"] = "caller mutation"
            return result

        with mock.patch("lib.control.store.validate_task", side_effect=validate_then_mutate):
            self.store.save(record)
        stored = self.store.read(record["task_id"])
        self.assertEqual(stored["label"], expected_label)

    def test_fifo_records_are_skipped_and_direct_operations_fail_promptly(self) -> None:
        record = self.record()
        self.store.save(record)
        record_path = self.config.tasks_dir / f"{record['task_id']}.json"
        record_path.unlink()
        os.mkfifo(record_path, 0o600)
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "TASK_ID": record["task_id"],
            "TASK_JSON": json.dumps(record),
            "DIGEST": task_digest(record),
        })
        template = """
import json, os
from lib.control.config import load_config
from lib.control.store import StoreError, TaskStore
s = TaskStore(load_config(os.environ))
try:
    OPERATION
except StoreError:
    print('rejected')
else:
    raise SystemExit('unsafe operation succeeded')
"""
        list_result = subprocess.run(
            [sys.executable, "-c", """
import os
from lib.control.config import load_config
from lib.control.store import TaskStore
s = TaskStore(load_config(os.environ))
print(len(s.list()), len(s.skipped))
"""],
            env=env, capture_output=True, text=True, timeout=2, check=True,
        )
        self.assertEqual(list_result.stdout.strip(), "0 1")
        for operation in (
            "s.read(os.environ['TASK_ID'])",
            "s.save(json.loads(os.environ['TASK_JSON']), expected_digest=os.environ['DIGEST'])",
        ):
            result = subprocess.run(
                [sys.executable, "-c", template.replace("OPERATION", operation)],
                env=env, capture_output=True, text=True, timeout=2, check=True,
            )
            self.assertEqual(result.stdout.strip(), "rejected")

        second = self.record(slug="fifo-lock")
        self.store.save(second)
        lock = self.config.runtime_dir / "tasks" / f"{self.task_lock_id(second['task_id'])}.lock"
        lock.unlink()
        os.mkfifo(lock, 0o600)
        with self.assertRaisesRegex(StoreError, "regular file"):
            self.store.save(second, expected_digest=task_digest(second))

    def test_list_is_sorted_and_show_uses_authoritative_id_or_unique_exact_slug(self) -> None:
        duplicate_a = self.record(slug="same")
        duplicate_b = self.record(slug="same")
        unique = self.record(slug="unique")
        for record in (duplicate_b, unique, duplicate_a):
            self.store.save(record)

        listed = self.store.list()
        self.assertEqual([item["task_id"] for item in listed], sorted(
            [duplicate_a["task_id"], duplicate_b["task_id"], unique["task_id"]]
        ))
        self.assertEqual(self.store.resolve(unique["task_id"]), unique)
        self.assertEqual(self.store.resolve("unique"), unique)
        with self.assertRaisesRegex(StoreError, "ambiguous"):
            self.store.resolve("same")
        with self.assertRaisesRegex(StoreError, "not found"):
            self.store.resolve("missing")
        with self.assertRaisesRegex(StoreError, "task selector") as caught:
            self.store.resolve("unsafe\u202eselector")
        self.assertNotIn("\u202e", str(caught.exception))

    def test_list_skips_foreign_and_unreadable_records_without_hiding_good_tasks(self) -> None:
        good = self.record(slug="good")
        self.store.save(good)
        (self.config.tasks_dir / "notes.json").write_text(
            "operator notes\n", encoding="utf-8",
        )
        unreadable_name = f"{uuid.uuid4()}.json"
        unreadable = self.config.tasks_dir / unreadable_name
        unreadable.write_text("{}\n", encoding="utf-8")
        unreadable.chmod(0o644)

        self.assertEqual(self.store.list(), [good])
        self.assertEqual(
            {entry["name"] for entry in self.store.skipped},
            {"notes.json", unreadable_name},
        )
        self.assertTrue(all(entry["reason"] for entry in self.store.skipped))
        self.assertEqual(self.store.resolve("good"), good)
        self.assertEqual(len(self.store.skipped), 2)

    def test_update_preserves_existing_run_order(self) -> None:
        record = self.record()
        record["lifecycle"] = "ended"
        record["runs"][0]["state"] = "exited"
        second = json.loads(json.dumps(record["runs"][0]))
        second["run_id"] = str(uuid.uuid4())
        second["pane_id"] = "%24"
        record["runs"].append(second)
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["updated_at"] = "2026-08-14T18:05:00Z"
        changed["runs"].reverse()
        with self.assertRaisesRegex(StoreError, "original order"):
            self.store.save(changed, expected_digest=task_digest(record))

    def test_new_run_must_enter_in_starting_state(self) -> None:
        record = self.record()
        self.store.save(record)
        changed = json.loads(json.dumps(record))
        changed["runs"][0]["state"] = "exited"
        second = json.loads(json.dumps(changed["runs"][0]))
        second["run_id"] = str(uuid.uuid4())
        second["pane_id"] = "%99"
        second["state"] = "working"
        changed["runs"].append(second)
        changed["updated_at"] = "2026-08-14T18:06:00Z"
        changed["runs"][0]["evidence_at"] = changed["updated_at"]
        changed["runs"][1]["evidence_at"] = changed["updated_at"]
        with self.assertRaisesRegex(StoreError, "new runs.*starting"):
            self.store.save(changed, expected_digest=task_digest(record))

    def test_list_skips_malformed_record_while_direct_read_rejects_it(self) -> None:
        record = self.record()
        self.store.save(record)
        path = self.config.tasks_dir / f"{record['task_id']}.json"
        path.write_text("not json")
        self.assertEqual(self.store.list(), [])
        self.assertIn("invalid JSON", self.store.skipped[0]["reason"])
        with self.assertRaisesRegex(StoreError, "invalid JSON"):
            self.store.read(record["task_id"])

        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.read(record["task_id"])

    def test_store_rejects_existing_non_directory_repository_and_workspace_components(self) -> None:
        repository_file = self.root / "repository-file"
        repository_file.write_text("not a directory")
        with self.assertRaisesRegex(StoreError, "directory"):
            self.store.save(self.record(repository_root=str(repository_file)))

        workspace_parent = self.config.workspace_root / "repo-key"
        workspace_parent.mkdir(parents=True)
        current = workspace_parent
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        workspace_file = workspace_parent / "workspace-file"
        workspace_file.write_text("not a directory")
        with self.assertRaisesRegex(StoreError, "directory"):
            self.store.save(self.record(
                slug="workspace-file",
                workspace_path=str(workspace_file),
            ))

        intermediate = self.root / "repository-intermediate"
        intermediate.write_text("not a directory")
        with self.assertRaisesRegex(StoreError, "directory"):
            self.store.save(self.record(repository_root=str(intermediate / "child")))

    def test_read_rejects_record_with_permissions_broader_than_0600(self) -> None:
        record = self.record()
        path = self.store.save(record)
        path.chmod(0o644)
        with self.assertRaisesRegex(StoreError, "mode 0600"):
            self.store.read(record["task_id"])

    def test_invalid_registry_filename_does_not_echo_unicode_format_controls(self) -> None:
        self.store.save(self.record())
        hostile = self.config.tasks_dir / "unsafe\u202ename.json"
        hostile.write_text("{}")
        hostile.chmod(0o600)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.skipped[0]["name"], hostile.name)
        self.assertNotIn("\u202e", self.store.skipped[0]["reason"])


if __name__ == "__main__":
    unittest.main()
