import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from lib.control.database import ControlDatabase, DATABASE_NAME
from lib.control.jj import ColocationIntentStore
from lib.control.orchestration.authority import add_authority, list_authorities, revoke_authority
from lib.control.orchestration.config import from_control
from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
from lib.control.orchestration.store import InitiativeStore
from lib.control.prune import PruneRecordStore
from lib.control.record_registry import RecordRegistry
from lib.control.registry_backend import BACKEND_CONTRACT, selected_backend
from lib.control.rooms import RoomStore
from lib.control.sqlite_auxiliary import SQLitePruneRecordStore
from lib.control.sqlite_colocation import SQLiteColocationIntentStore
from lib.control.sqlite_journals import SQLiteCreationJournalStore
from lib.control.sqlite_ownership import SQLiteOwnershipStore
from lib.control.sqlite_rooms import SQLiteRoomStore
from lib.control.sqlite_tasks import SQLiteTaskStore
from lib.control.store import TaskStore, TransactionCoordinator, StoreError
from lib.control.transaction import CreationJournalStore, MaterializationOwnershipStore
from tests.python import test_control_increment2 as journal_tests
from tests.python.test_orchestration_model import initiative


def install_marker_fixture(config, **overrides):
    """Selection fixture only; does not test or perform production activation."""
    value = {"contract": BACKEND_CONTRACT, "backend": "sqlite", "state": "active",
        "activation_id": str(uuid.uuid4()), "source_root": str(config.asha_home),
        "stage_digest": "a" * 64, "activated_at": "2026-09-08T12:00:00Z", **overrides}
    with ControlDatabase(config, create=True) as db, db.transaction(write=True) as c:
        RecordRegistry("registry-backend", scope="control").put(c, "active", json.dumps(value).encode())
    return value


class RegistryBackendTests(unittest.TestCase):
    def setUp(self):
        journal_tests.JournalStoreTests.setUp(self)
        self.orchestration = from_control(self.config)

    def constructors(self):
        return [(TaskStore, SQLiteTaskStore, self.config),
            (RoomStore, SQLiteRoomStore, self.config),
            (InitiativeStore, SQLiteInitiativeStore, self.orchestration),
            (CreationJournalStore, SQLiteCreationJournalStore, self.config),
            (PruneRecordStore, SQLitePruneRecordStore, self.config),
            (ColocationIntentStore, SQLiteColocationIntentStore, self.config),
            (MaterializationOwnershipStore, SQLiteOwnershipStore, self.config)]

    def test_missing_database_and_unselected_database_keep_file_stores(self):
        for create in (False, True):
            if create:
                with ControlDatabase(self.config, create=True):
                    pass
            for base, _, config in self.constructors():
                self.assertIs(type(base(config)), base)
            self.assertEqual((self.config.tasks_dir.parent / DATABASE_NAME).exists(), create)

    def test_all_public_constructors_select_sqlite_and_run_subclass_initializers(self):
        install_marker_fixture(self.config)
        for base, sql, config in self.constructors():
            instance = base(config)
            self.assertIs(type(instance), sql)
            self.assertIs(instance.__class__, sql)
        hook = object()
        self.assertIs(TaskStore(self.config, lock_wait_hook=hook)._lock_wait_hook, hook)
        journal = journal_tests.JournalStoreTests.journal(self)
        CreationJournalStore(self.config).save(journal)
        self.assertEqual(CreationJournalStore(self.config).read(self.task_id), journal)
        self.assertFalse((self.config.tasks_dir.parent / "transactions").exists())
        record = initiative()
        InitiativeStore(self.orchestration).save_initiative(record)
        self.assertEqual(InitiativeStore(self.orchestration).read_initiative(record["initiative_id"]), record)

    def test_small_room_configuration_uses_the_same_backend_root(self):
        install_marker_fixture(self.config)
        small = SimpleNamespace(asha_home=self.config.asha_home)
        room = RoomStore(small)
        self.assertIs(type(room), SQLiteRoomStore)
        self.assertEqual(room.config.tasks_dir, self.config.tasks_dir)

    def test_incomplete_transition_and_offline_stage_refuse_construction(self):
        with ControlDatabase(self.config, create=True) as db, db.transaction(write=True) as c:
            RecordRegistry("registry-backend", scope="control").put(c, "transition", b'{"state":"preparing"}')
        for base, _, config in self.constructors():
            with self.assertRaisesRegex(StoreError, "incomplete"):
                base(config)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
            RecordRegistry("registry-migration", scope="control").put(c, "attempt", b'{"state":"incomplete"}')
        for base, _, config in self.constructors():
            with self.assertRaisesRegex(StoreError, "offline"):
                base(config)

    def test_foreign_or_malformed_marker_never_falls_back_to_files(self):
        install_marker_fixture(self.config, source_root="/tmp/foreign-control-root")
        with self.assertRaisesRegex(StoreError, "another root"):
            TaskStore(self.config)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
            RecordRegistry("registry-backend", scope="control").put(c, "active", b'{"state":"active"}')
        with self.assertRaises(StoreError):
            selected_backend(self.config)

    def test_source_lifecycle_lock_does_not_write_frozen_task_registry(self):
        install_marker_fixture(self.config)
        self.config.tasks_dir.mkdir(mode=0o700)
        self.config.tasks_dir.chmod(0o500)
        self.addCleanup(self.config.tasks_dir.chmod, 0o700)
        source = Path(self.temp.name) / "lock-source"
        source.mkdir()
        with TransactionCoordinator(self.config).source_lock(source):
            self.assertEqual(list(self.config.tasks_dir.iterdir()), [])
        self.assertTrue(list((self.config.tasks_dir.parent / "registry-locks/tasks").glob("source-*.lock")))

    def test_public_authority_creation_derives_repository_binding_before_sql_write(self):
        install_marker_fixture(self.config)
        repository = initiative()["scope"]["repository"]
        with mock.patch("lib.control.orchestration.authority.repository_scope", return_value=repository) as probe:
            grant = add_authority(self.orchestration, root=Path(repository["root"]), label="small-work",
                                  scope_prefixes=["lib"], jj=object())
        probe.assert_called_once()
        self.assertEqual(grant["repository"], repository)
        self.assertEqual(list_authorities(self.orchestration), [grant])
        self.assertIsNotNone(revoke_authority(self.orchestration, grant["authority_id"])["revoked_at"])
        self.assertEqual(list_authorities(self.orchestration), [])
        self.assertFalse((self.config.tasks_dir.parent / "authorities").exists())
