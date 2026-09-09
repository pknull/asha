import copy
import os
from pathlib import Path
import unittest
import uuid

from lib.control.database import ControlDatabase
from lib.control.sqlite_journals import SQLiteCreationJournalStore
from lib.control.sqlite_ownership import SQLiteOwnershipStore
from lib.control.transaction import (MaterializationOwnershipStore, JournalError,
    JOURNAL_CONTRACT, _JJ_PRIVATE_PATHS, validate_journal)
from tests.python import test_control_increment2 as journal_tests


class SQLiteOwnershipTests(unittest.TestCase):
    def setUp(self):
        journal_tests.JournalStoreTests.setUp(self)
        self.store = SQLiteOwnershipStore(self.config)
        self.legacy = MaterializationOwnershipStore(self.config)
        self.facts = [[1, 2, 0o100600, os.geteuid()]]
        self.plan = "a" * 64

    def test_new_sidecar_is_separate_and_replay_preserves_inode(self):
        binding = self.store.write(self.task_id, self.plan, self.facts)
        self.assertEqual(Path(binding["path"]).parent, self.config.tasks_dir.parent / "materialization-ownership")
        self.assertFalse(self.legacy.directory.exists())
        self.assertEqual(self.store.read(binding), self.facts)
        self.assertEqual(self.store.write(self.task_id, self.plan, self.facts), binding)
        with self.assertRaises(JournalError):
            self.store.write(self.task_id, self.plan, [[2, 3, 4, 5]])

    def test_retained_sidecar_read_and_replay_leave_exact_binding_under_frozen_parent(self):
        binding = self.legacy.write(self.task_id, self.plan, self.facts)
        path = Path(binding["path"])
        before = path.stat()
        self.legacy.directory.chmod(0o500)
        self.addCleanup(self.legacy.directory.chmod, 0o700)
        self.assertEqual(self.store.path(self.task_id), path)
        self.assertEqual(self.store.read(binding), self.facts)
        self.assertEqual(self.store.write(self.task_id, self.plan, self.facts), binding)
        self.assertEqual(path.stat().st_ino, before.st_ino)
        self.assertEqual(path.stat().st_mode, before.st_mode)
        with self.assertRaises(JournalError):
            self.store.write(self.task_id, "b" * 64, self.facts)
        with self.assertRaises(JournalError):
            self.legacy.write(str(uuid.uuid4()), self.plan, self.facts)
        self.store.write(str(uuid.uuid4()), self.plan, self.facts)

    def test_replaced_or_chmodded_retained_sidecar_refuses_original_binding(self):
        binding = self.legacy.write(self.task_id, self.plan, self.facts)
        path = Path(binding["path"])
        path.chmod(0o400)
        with self.assertRaises(JournalError):
            self.store.read(binding)
        path.chmod(0o600)
        old = path.with_suffix(".old")
        path.rename(old)
        path.write_bytes(old.read_bytes())
        path.chmod(0o600)
        with self.assertRaisesRegex(JournalError, "identity changed"):
            self.store.read(binding)

    def test_duplicate_roots_symlink_and_foreign_binding_are_refused(self):
        binding = self.legacy.write(self.task_id, self.plan, self.facts)
        self.store.directory.mkdir(mode=0o700)
        duplicate = self.store.directory / Path(binding["path"]).name
        duplicate.write_bytes(Path(binding["path"]).read_bytes())
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(JournalError, "both"):
            self.store.read(binding)
        duplicate.unlink()
        duplicate.symlink_to(binding["path"])
        with self.assertRaises(JournalError):
            self.store.read(binding)
        duplicate.unlink()
        with self.assertRaises(JournalError):
            self.store.read({**binding, "path": str(self.config.asha_home / "foreign" / duplicate.name)})

    def test_interrupted_new_sidecar_publication_replays_without_changing_bound_inode(self):
        def fail(point):
            if point == "sidecar:renamed":
                raise RuntimeError("power loss")
        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.store.write(self.task_id, self.plan, self.facts, failure_injector=fail)
        inode = self.store.path(self.task_id).stat().st_ino
        binding = self.store.write(self.task_id, self.plan, self.facts)
        self.assertEqual(Path(binding["path"]).stat().st_ino, inode)
        self.assertEqual(self.store.read(binding), self.facts)

    def test_sql_journal_accepts_only_exact_new_or_retained_sidecar_paths(self):
        binding = self.store.write(self.task_id, self.plan, self.facts)
        journal = journal_tests.JournalStoreTests.journal(self)
        journal["contract"] = JOURNAL_CONTRACT
        journal.pop("expected_materialization")
        journal.pop("materialized_owned")
        journal["materialization_plan"] = {"contract": "asha.control-materialization-plan.v1",
            "base_commit_id": journal["jj"]["base_commit_id"], "digest": self.plan,
            "blob_count": 1, "directory_count": 0, "entry_count": 1, "total_blob_bytes": 0}
        journal["materialization_ownership"] = {"sidecar": binding, "private": {key: {} for key in _JJ_PRIVATE_PATHS}}
        with self.assertRaises(JournalError):
            validate_journal(journal, config=self.config)
        with ControlDatabase(self.config, create=True):
            pass
        journals = SQLiteCreationJournalStore(self.config)
        journals.save(journal)
        self.assertEqual(journals.read(self.task_id), journal)
        changed = copy.deepcopy(journal)
        changed["materialization_ownership"]["sidecar"]["path"] = str(
            self.config.tasks_dir.parent / "materialization-ownership" / (str(uuid.uuid4()) + ".ownership"))
        with self.assertRaises(JournalError):
            journals.save(changed)
        changed["materialization_ownership"]["sidecar"]["path"] = str(self.config.asha_home / Path(binding["path"]).name)
        with self.assertRaises(JournalError):
            journals.save(changed)
