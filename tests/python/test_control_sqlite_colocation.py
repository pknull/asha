import copy
import json
import unittest
from unittest import mock

from lib.control.cli import RepositorySelection, _ensure_colocated
from lib.control.database import ControlDatabase, DATABASE_NAME
from lib.control.jj import JjError
from lib.control.record_registry import RecordRegistry
from lib.control.sqlite_colocation import SQLiteColocationIntentStore
from tests.python import test_control_task_start_smoke_fixes as legacy


class SQLiteColocationTests(unittest.TestCase):
    def setUp(self):
        legacy.ColocationIntentTests.setUp(self)
        with ControlDatabase(self.config, create=True):
            pass
        self.store = SQLiteColocationIntentStore(self.config)

    def verify(self):
        self.store.begin(self.source)
        (self.source / '.jj').mkdir()
        self.store.mark_verified(self.source)

    def test_intent_requires_explicit_verified_transition_and_current_identity(self):
        self.assertIsNone(self.store.read(self.source))
        self.assertEqual(self.store.classify(self.source).kind, 'missing')
        self.store.begin(self.source)
        self.assertEqual(self.store.read(self.source)['state'], 'intent')
        with self.assertRaises(JjError):
            self.store.begin(self.source)
        with self.assertRaises(JjError):
            self.store.mark_verified(self.source)
        (self.source / '.jj').mkdir()
        self.store.mark_verified(self.source)
        self.assertEqual(self.store.classify(self.source).kind, 'verified')
        self.assertFalse(self.store.directory.exists())
        (self.source / '.jj').rename(self.source / 'old-jj')
        (self.source / '.jj').mkdir()
        with self.assertRaises(JjError):
            self.store.read(self.source)

    def test_hardening_candidate_cannot_be_replayed_or_modify_other_fields(self):
        self.source.chmod(0o775)
        self.verify()
        before = self.store.read(self.source)
        self.source.chmod(0o755)
        assessment = self.store.classify(self.source)
        self.assertEqual(assessment.kind, 'verified_root_hardening_candidate')
        self.store.reauthenticate_root_hardening(self.source, assessment)
        updated = self.store.read(self.source)
        self.assertEqual(updated['root_fact']['mode'] & 0o777, 0o755)
        self.assertEqual({**updated, 'root_fact': before['root_fact']}, before)
        with self.assertRaises(JjError):
            self.store.reauthenticate_root_hardening(self.source, assessment)

    def test_device_rebind_requires_exact_typed_candidate(self):
        self.verify()
        current = self.store.read(self.source)
        value = copy.deepcopy(current)
        for fact in self.store._binding_facts(value):
            fact['dev'] += 1000
        registry = RecordRegistry('repository-inits')
        key = self.store._key(self.source)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            row = registry.read(c, key)
            registry.put(c, key, json.dumps(value).encode(), expected_digest=row['digest'])
        candidate = self.store.classify(self.source)
        self.assertEqual(candidate.kind, 'verified_device_rebind_candidate')
        self.store.reauthenticate_device_rebind(self.source, candidate)
        self.assertEqual(self.store.read(self.source), current)

    def test_filesystem_change_during_reauthentication_rolls_back(self):
        self.source.chmod(0o775)
        self.verify()
        self.source.chmod(0o755)
        candidate = self.store.classify(self.source)
        original = RecordRegistry.put
        def changed(*args, **kwargs):
            original(*args, **kwargs)
            self.source.chmod(0o700)
        with mock.patch.object(RecordRegistry, 'put', new=changed):
            with self.assertRaises(JjError):
                self.store.reauthenticate_root_hardening(self.source, candidate)
        self.source.chmod(0o755)
        self.assertEqual(self.store.classify(self.source).raw, candidate.raw)

    def test_ambiguous_intent_remediation_never_names_the_database(self):
        # Under SQLite the intent's path() is the whole Control database; the
        # remediation once told the operator to delete it.
        self.store.begin(self.source)
        (self.source / '.jj').mkdir()
        adapter = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'ambiguous Control colocation') as caught:
            _ensure_colocated(adapter, RepositorySelection(self.source, plain_git=False), self.store)
        message = str(caught.exception)
        self.assertNotIn(DATABASE_NAME, message)
        self.assertNotIn(str(self.store.path(self.source)), message)
        self.assertNotIn('`rm', message)
        self.assertIn('`repository-inits` record', message)
        self.assertIn(self.store._key(self.source), message)
        adapter.preflight.assert_not_called()
