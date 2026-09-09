import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.control.database import ControlDatabase
from lib.control.orchestration.authority import AUTHORITY_CONTRACT, AuthorityError
from lib.control.orchestration.config import load_config
from lib.control.prune import PruneError
from lib.control.record_registry import RecordRegistry
from lib.control.sqlite_auxiliary import SQLiteAuthorityStore, SQLitePruneRecordStore
from tests.python.test_orchestration_model import initiative


class SQLiteAuxiliaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = load_config({'HOME': str(root), 'ASHA_HOME': str(root / 'asha'),
                                   'ASHA_CONFIG': str(root / 'missing.json')})
        with ControlDatabase(self.config.control, create=True):
            pass
        self.authorities = SQLiteAuthorityStore(self.config)
        self.prunes = SQLitePruneRecordStore(self.config.control)
        self.identity = '11111111-2222-4333-8444-555555555555'
        self.grant = {'contract': AUTHORITY_CONTRACT, 'authority_id': self.identity,
            'label': 'bounded-work', 'repository': initiative()['scope']['repository'],
            'constraints': {'scope_prefixes': ['lib'], 'max_nodes': 2, 'harnesses': ['claude'],
                            'max_attempts_per_node': 1, 'require_headless': True},
            'auto_activate': False, 'created_at': '2026-01-01T00:00:00Z', 'revoked_at': None}

    def test_grant_is_write_once_and_revocation_preserves_authority(self):
        self.authorities.create(self.grant)
        with self.assertRaises(AuthorityError):
            self.authorities.create(self.grant)
        self.assertEqual(self.authorities.list(), [self.grant])
        revoked = self.authorities.revoke(self.identity)
        self.assertIsNotNone(revoked['revoked_at'])
        self.assertEqual({**revoked, 'revoked_at': None}, self.grant)
        self.assertEqual(self.authorities.revoke(self.identity), revoked)
        self.assertEqual(self.authorities.list(), [])
        self.assertEqual(self.authorities.list(include_revoked=True), [revoked])
        self.assertFalse((self.config.control.tasks_dir.parent / 'authorities').exists())

    def test_foreign_authority_payload_refuses_listing(self):
        value = copy.deepcopy(self.grant)
        value['authority_id'] = '22222222-2222-4222-8222-222222222222'
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            RecordRegistry('authorities').put(c, self.identity, json.dumps(value).encode())
        with self.assertRaises(AuthorityError):
            self.authorities.list()

    def test_prune_facts_round_trip_without_identity_override(self):
        self.assertIsNone(self.prunes.read(self.identity))
        facts = {'workspace_removed': True, 'workspace_path': '/retained/workspace'}
        self.prunes.write(self.identity, facts)
        value = self.prunes.read(self.identity)
        self.assertTrue(value['workspace_removed'])
        self.assertEqual(value['task_id'], self.identity)
        with self.assertRaises(PruneError):
            self.prunes.write(self.identity, {'task_id': '22222222-2222-4222-8222-222222222222'})
        self.assertEqual(self.prunes.read(self.identity), value)
        self.assertFalse(self.prunes.directory.exists())

    def test_authority_revocation_interruption_does_not_publish(self):
        self.authorities.create(self.grant)
        original = RecordRegistry.put
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('revocation interrupted')
        with mock.patch.object(RecordRegistry, 'put', new=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'revocation interrupted'):
                self.authorities.revoke(self.identity)
        self.assertEqual(self.authorities.list(), [self.grant])


from tests.python import test_control_prune as prune_tests
from lib.control.sqlite_journals import SQLiteCreationJournalStore
from lib.control.sqlite_tasks import SQLiteTaskStore


class SQLitePruneLifecycleTests(prune_tests.PruneFixture):
    def setUp(self):
        super().setUp()
        with ControlDatabase(self.config, create=True):
            pass
        self.tasks = SQLiteTaskStore(self.config)
        self.journals = SQLiteCreationJournalStore(self.config)
        self.prune_records = SQLitePruneRecordStore(self.config)

    def prune(self, task, **kwargs):
        return super().prune(task, records=self.prune_records, **kwargs)

    test_archived_prune_and_repeat = prune_tests.PruneTaskTests.test_prunes_archived_task_without_touching_record
    test_live_pane_blocks_removal = prune_tests.PruneTaskTests.test_live_pane_blocks_everything

    def test_retained_sql_prune_prevents_successor_removal(self):
        task = self.archived_task()
        workspace = Path(task['jj']['workspace_path'])
        result, _, _ = self.prune(task)
        self.assertEqual(result.workspace.action, 'removed')
        workspace.mkdir(mode=0o700)
        retained = workspace / 'successor.txt'
        retained.write_text('new work')
        again, _, _ = self.prune(task, tmux=prune_tests.FakeTmux(present=False), jj=prune_tests.FakeJj())
        self.assertEqual(again.workspace.action, 'absent')
        self.assertEqual(retained.read_text(), 'new work')
        self.assertTrue(self.prune_records.read(task['task_id'])['workspace_removed'])
