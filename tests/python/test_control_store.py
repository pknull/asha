"""Shared private-storage helpers: directory traversal, file opens and locks.

The task registry that once exercised these helpers retired with the task
substrate (L-b); Rooms, sessions and the database still use them, so their
guards are pinned here directly.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.store import (
    StoreError, _directory_fd, _managed_start, _open_existing_file, _registry_lock,
)


ROOT = Path(__file__).resolve().parents[2]


class StorageHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = self.root / "asha/state/control"

    def open_state(self, path: Path | None = None, *, create: bool = True):
        path = path or self.state
        return _directory_fd(path, create=create, managed_start=_managed_start(path, ("state", "control")))

    def test_missing_directories_yield_none_until_created_private(self) -> None:
        with self.open_state(create=False) as fd:
            self.assertIsNone(fd)
        self.assertFalse(self.state.exists())
        with self.open_state() as fd:
            self.assertIsNotNone(fd)
        for directory in (self.state, self.state.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_same_user_metadata_changes_never_hide_or_refuse_state(self) -> None:
        # Threat model (2026-10-05): the local user is trusted, so modes, link
        # counts and symlinks the Keeper or his tools leave on Asha's private
        # trees never refuse an operation or hide a record (#115, #116).
        with self.open_state():
            pass
        record = self.state / "record.json"
        record.write_text("{}")
        record.chmod(0o644)
        outside = self.root / "outside"
        outside.write_text("{}")
        os.link(outside, self.state / "linked.json")
        for directory in (self.state, self.state.parent):
            directory.chmod(0o775)
        moved = self.root / "elsewhere-state"
        self.state.parent.rename(moved)
        self.state.parent.symlink_to(moved, target_is_directory=True)

        with self.open_state(create=False) as fd:
            self.assertIsNotNone(fd)
            for name in ("record.json", "linked.json"):
                opened = _open_existing_file(fd, name, "record")
                try:
                    self.assertEqual(os.read(opened, 16), b"{}")
                finally:
                    os.close(opened)
            with _registry_lock(fd):
                pass
        self.assertEqual(stat.S_IMODE((moved / "control").stat().st_mode), 0o775)

    def test_fifo_files_fail_promptly_as_not_regular(self) -> None:
        with self.open_state():
            pass
        os.mkfifo(self.state / "record.json", 0o600)
        program = (
            "import sys\n"
            "from pathlib import Path\n"
            "from lib.control.store import StoreError, _directory_fd, _managed_start, _open_existing_file\n"
            "path = Path(sys.argv[1])\n"
            "with _directory_fd(path, create=False, managed_start=_managed_start(path, ('state', 'control'))) as fd:\n"
            "    try:\n"
            "        _open_existing_file(fd, 'record.json', 'record')\n"
            "    except StoreError as exc:\n"
            "        print(exc)\n"
        )
        result = subprocess.run([sys.executable, "-c", program, str(self.state)], cwd=ROOT,
                                capture_output=True, text=True, timeout=5, check=True)
        self.assertIn("record is not a regular file", result.stdout)

    def test_first_use_mkdir_eexist_race_reopens_without_chmod(self) -> None:
        real_mkdir = os.mkdir
        real_fsync = os.fsync
        raced: list[str] = []
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
                raced.append(path)
                raise FileExistsError("simulated concurrent mkdir winner")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def tracking_fsync(fd: int) -> None:
            synced_inodes.append(os.fstat(fd).st_ino)
            real_fsync(fd)

        with mock.patch("lib.control.store.os.mkdir", side_effect=losing_mkdir), \
                mock.patch("lib.control.store.os.fsync", side_effect=tracking_fsync):
            with self.open_state() as fd:
                self.assertIsNotNone(fd)
        self.assertTrue(raced)
        self.assertTrue(set(race_inodes).issubset(synced_inodes))

        other = self.root / "other/state/control"
        other.parent.parent.mkdir(mode=0o700)
        raced.clear()

        def unsafe_winner(path, mode=0o777, *, dir_fd=None):
            if not raced:
                real_mkdir(path, 0o777, dir_fd=dir_fd)
                os.chmod(path, 0o777, dir_fd=dir_fd, follow_symlinks=False)
                raced.append(path)
                raise FileExistsError("simulated unsafe winner")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        # A directory another creator made is used as found: never refused on
        # its mode, never chmodded.
        with mock.patch("lib.control.store.os.mkdir", side_effect=unsafe_winner):
            with self.open_state(other):
                pass
        self.assertTrue(raced)
        self.assertEqual(stat.S_IMODE(other.parent.stat().st_mode), 0o777)

    def test_directory_durability_failure_is_controlled_and_retry_resyncs_visible_pair(self) -> None:
        real_fsync = os.fsync
        for failure_side in ("child", "parent"):
            with self.subTest(failure_side=failure_side), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                state = root / "asha/state/control"
                child_path = root / "asha"
                parent_inode = root.stat().st_ino
                injected: list[int] = []

                def fail_first_pair_sync(fd: int) -> None:
                    inode = os.fstat(fd).st_ino
                    if child_path.exists():
                        target = child_path.stat().st_ino if failure_side == "child" else parent_inode
                        if inode == target and not injected:
                            injected.append(inode)
                            raise OSError("injected directory durability failure")
                    real_fsync(fd)

                with mock.patch("lib.control.store.os.fsync", side_effect=fail_first_pair_sync):
                    with self.assertRaisesRegex(StoreError, "cannot establish Control directory durability"):
                        with self.open_state(state):
                            pass
                self.assertTrue(injected)
                self.assertTrue(child_path.is_dir())
                self.assertFalse(state.exists())

                child_inode = child_path.stat().st_ino
                retry_syncs: list[int] = []

                def track_retry(fd: int) -> None:
                    retry_syncs.append(os.fstat(fd).st_ino)
                    real_fsync(fd)

                with mock.patch("lib.control.store.os.fsync", side_effect=track_retry):
                    with self.open_state(state):
                        pass
                self.assertIn((child_inode, parent_inode), list(zip(retry_syncs, retry_syncs[1:])))


if __name__ == "__main__":
    unittest.main()
