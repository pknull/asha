import copy
import fcntl
import os
from pathlib import Path
import unittest

from lib.control.registry_tree import (ROOTS, capture_tree, prepare_missing_roots,
    verify_tree, apply_modes, fence_locks, validate_entries)
from lib.control.store import StoreError, TaskStore
from lib.control.transaction import MaterializationOwnershipStore
from tests.python import test_control_registry_stage as fixtures


class RegistryTreeTests(unittest.TestCase):
    def setUp(self):
        fixtures.RegistryStageTests.setUp(self)
        self.sidecar = MaterializationOwnershipStore(self.config).write(self.task["task_id"], "a" * 64, [[1, 2, 3, 4]])
        self.snapshot = capture_tree(self.config)
        self.prepared = prepare_missing_roots(self.config, self.snapshot)
        def thaw_for_cleanup():
            for root in ROOTS:
                path = self.config.tasks_dir.parent / root
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o700)
                    for directory, dirs, files in os.walk(path):
                        for name in dirs:
                            child = Path(directory) / name
                            if not child.is_symlink():
                                child.chmod(0o700)
                        for name in files:
                            child = Path(directory) / name
                            if not child.is_symlink():
                                child.chmod(0o600)
        self.addCleanup(thaw_for_cleanup)

    def test_freeze_and_restore_preserve_bytes_inodes_and_ownership_file_mode(self):
        original = copy.deepcopy(self.prepared)
        with fence_locks(self.config, self.prepared):
            apply_modes(self.config, self.prepared, frozen=True)
            verify_tree(self.config, self.prepared, frozen=True)
            self.assertEqual(Path(self.sidecar["path"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.config.tasks_dir.stat().st_mode & 0o777, 0o500)
            with self.assertRaises(StoreError):
                TaskStore(self.config).save(self.task)
            apply_modes(self.config, self.prepared, frozen=False)
        self.assertEqual(capture_tree(self.config), original)

    def test_partial_freeze_resumes_and_partial_thaw_resumes(self):
        def crash(_path):
            raise RuntimeError("interrupted")
        with fence_locks(self.config, self.prepared):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                apply_modes(self.config, self.prepared, frozen=True, after_change=crash)
        with fence_locks(self.config, self.prepared):
            apply_modes(self.config, self.prepared, frozen=True)
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                apply_modes(self.config, self.prepared, frozen=False, after_change=crash)
        with fence_locks(self.config, self.prepared):
            apply_modes(self.config, self.prepared, frozen=False)
        verify_tree(self.config, self.prepared)

    def test_replaced_inode_cannot_be_thawed_by_replayed_snapshot(self):
        path = self.config.tasks_dir / (self.task["task_id"] + ".json")
        raw = path.read_bytes()
        path.rename(path.with_suffix(".old"))
        path.write_bytes(raw)
        path.chmod(0o600)
        path.with_suffix(".old").unlink()
        with self.assertRaisesRegex(StoreError, "snapshot changed"):
            apply_modes(self.config, self.prepared, frozen=True)
        self.assertEqual(self.config.tasks_dir.stat().st_mode & 0o777, 0o700)

    def test_busy_writer_refuses_without_waiting_and_releases_partial_locks(self):
        fd = os.open(self.config.tasks_dir, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(StoreError, "writer is still active"):
            with fence_locks(self.config, self.prepared):
                self.fail("busy writer admitted")
        fcntl.flock(fd, fcntl.LOCK_UN)
        with fence_locks(self.config, self.prepared):
            pass

    def test_snapshot_rejects_links_and_hardlinks(self):
        original = self.config.tasks_dir / (self.task["task_id"] + ".json")
        twin = original.with_suffix(".link")
        os.link(original, twin)
        with self.assertRaisesRegex(StoreError, "link count"):
            capture_tree(self.config)
        twin.unlink()
        twin.symlink_to(original)
        with self.assertRaisesRegex(StoreError, "symlink"):
            capture_tree(self.config)

    def test_fence_journal_rejects_escape_missing_parent_and_unknown_fields(self):
        for change in (lambda row: row.update(path="../foreign"),
                       lambda row: row.update(path="tasks/unknown/file.json"),
                       lambda row: row.update(extra="untrusted")):
            broken = copy.deepcopy(self.prepared)
            change(next(row for row in broken if row["kind"] == "file"))
            with self.assertRaises(StoreError):
                validate_entries(broken)

    def test_missing_root_is_bound_to_new_empty_inode_before_freezing(self):
        missing = [row["path"] for row in self.snapshot if row["kind"] == "missing"]
        self.assertTrue(missing)
        self.assertFalse(any(row["kind"] == "missing" for row in self.prepared))
        apply_modes(self.config, self.prepared, frozen=True)
        for name in missing:
            self.assertEqual((self.config.tasks_dir.parent / name).stat().st_mode & 0o777, 0o500)
