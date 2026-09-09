import unittest

from lib.control.database import ControlDatabase
from lib.control.record_registry import RecordRegistry
from lib.control.registry_snapshot import state_digest, backup_state_digest
from tests.python import test_control_registry_stage as fixtures


class RegistrySnapshotTests(unittest.TestCase):
    def setUp(self):
        fixtures.RegistryStageTests.setUp(self)

    def test_wal_backup_has_same_authoritative_digest(self):
        backup = self.root / "snapshot.sqlite3"
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "value", {"text": "preserve 雪"})
            with db.transaction() as c:
                expected = state_digest(c)
            db.backup(backup)
        with backup.open("rb") as stream:
            self.assertEqual(backup_state_digest(stream.fileno()), expected)

    def test_preparing_marker_and_search_rebuild_do_not_change_source_proof(self):
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "value", {"text": "keep"})
            with db.transaction() as c:
                expected = state_digest(c)
            with db.transaction(write=True) as c:
                RecordRegistry("registry-backend", scope="control").put(c, "transition", b'{"state":"preparing"}')
                c.execute("UPDATE records SET search_text='derived index repair' WHERE domain='fixture'")
            with db.transaction() as c:
                self.assertEqual(state_digest(c), expected)
            db.put("fixture", "project", "other", {"text": "new state"})
            with db.transaction() as c:
                self.assertNotEqual(state_digest(c), expected)

    def test_state_changes_are_detected_even_when_database_size_is_unchanged(self):
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "value", {"text": "same"})
            with db.transaction() as c:
                expected = state_digest(c)
            with db.transaction(write=True) as c:
                c.execute("UPDATE control_runtime SET reason='different pause reason'")
            with db.transaction() as c:
                self.assertNotEqual(state_digest(c), expected)
