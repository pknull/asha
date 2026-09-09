import copy
import unittest
from unittest import mock

from lib.control.database import ControlDatabase
from lib.control.record_registry import RecordRegistry
from lib.control.transaction import JournalError
from lib.control.sqlite_journals import SQLiteCreationJournalStore
from tests.python import test_control_increment2 as legacy


class SQLiteJournalTests(unittest.TestCase):
    def setUp(self):
        legacy.JournalStoreTests.setUp(self)
        with ControlDatabase(self.config, create=True):
            pass
        self.store = SQLiteCreationJournalStore(self.config)

    journal = legacy.JournalStoreTests.journal
    test_rejects_foreign_paths = legacy.JournalStoreTests.test_journal_rejects_paths_not_bound_to_current_config_and_task
    test_rejects_rebinding = legacy.JournalStoreTests.test_journal_rejects_config_task_and_created_parent_chain_rebinding

    def test_phase_transition_and_digest_guards(self):
        value = self.journal()
        self.store.save(value)
        self.assertEqual(self.store.read(self.task_id), value)
        self.assertFalse(self.store.transactions_dir.exists())
        updated = copy.deepcopy(value)
        updated['phase'] = 'task-recorded'
        self.store.save(updated, expected_phase='intent', expected_digest=self.store.digest(value))
        with self.assertRaisesRegex(JournalError, 'phase changed'):
            self.store.save(updated, expected_phase='intent')
        with self.assertRaisesRegex(JournalError, 'digest changed'):
            self.store.save(updated, expected_phase='task-recorded', expected_digest=self.store.digest(value))
        with self.assertRaisesRegex(JournalError, 'illegal.*transition'):
            self.store.save(value, expected_phase='task-recorded')
        self.assertEqual(self.store.read(self.task_id), updated)

    def test_interrupted_update_rolls_back(self):
        value = self.journal()
        self.store.save(value)
        updated = {**value, 'phase': 'task-recorded'}
        original = RecordRegistry.put
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('after row update')
        with mock.patch.object(RecordRegistry, 'put', new=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'after row update'):
                self.store.save(updated, expected_phase='intent')
        self.assertEqual(self.store.read(self.task_id), value)

    def test_ownership_and_removal_facts_cannot_be_rewritten(self):
        value = self.journal()
        value['launch_attempted'] = True
        self.store.save(value)
        changed = {**value, 'launch_attempted': False}
        with self.assertRaisesRegex(JournalError, 'launch_attempted cannot be cleared'):
            self.store.save(changed, expected_phase='intent')
        changed = copy.deepcopy(value)
        changed['jj']['description'] = 'Different authority'
        with self.assertRaisesRegex(JournalError, 'immutable'):
            self.store.save(changed, expected_phase='intent')
