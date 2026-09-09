import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from lib.control.config import load_config
from lib.control.database import ControlDatabase
from lib.control.record_registry import RecordRegistry
from lib.control.rooms import RoomError, RoomStore
from lib.control.sqlite_rooms import SQLiteRoomStore
from lib.control.store import SnapshotBudget, StoreError


class SQLiteRoomTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = load_config({"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")})
        with ControlDatabase(self.config, create=True):
            pass
        self.store = SQLiteRoomStore(self.config)
        self.record = {"contract": "asha.room.v1", "room_id": "11111111-1111-4111-8111-111111111111",
            "name": "Draft Room", "slug": "draft-room", "project_id": "novel",
            "project_name": "A Novel", "project_root": str(self.root), "harness": "claude",
            "tmux": {"session": "asha-draft", "session_id": "$7", "window": "room", "pane_id": "%42"},
            "created_at": "2026-09-08T12:00:00Z", "updated_at": "2026-09-08T12:00:00Z",
            "lifecycle": "open", "prompt_digest": "a" * 64}

    def test_record_lifecycle_uses_sqlite_without_a_json_registry(self):
        self.assertEqual(self.store.create(self.record), self.record)
        self.assertFalse(self.store.root.exists())
        updated = {**self.record, "lifecycle": "ended", "updated_at": "2026-09-08T12:01:00Z"}
        with self.store.transaction(create=False):
            self.store.save(updated, expected_digest=RoomStore.digest(self.record))
        self.assertEqual(self.store.read(self.record["room_id"]), updated)
        self.assertEqual(self.store.resolve("Draft Room"), updated)
        with self.assertRaisesRegex(RoomError, "stale"):
            self.store.save(self.record, expected_digest=RoomStore.digest(self.record))
        self.assertFalse(self.store.root.exists())

    def test_names_and_identity_remain_unique(self):
        self.store.create(self.record)
        with self.assertRaisesRegex(RoomError, "already exists"):
            self.store.create(self.record)
        other = {**self.record, "room_id": "22222222-2222-4222-8222-222222222222", "name": "DRAFT ROOM"}
        with self.assertRaisesRegex(RoomError, "already exists"):
            self.store.create(other)
        self.assertEqual(len(self.store.list()), 1)

    def test_import_bytes_and_digest_are_preserved_until_an_explicit_save(self):
        # Import must not normalize whitespace or Unicode in sealed source bytes.
        value = {**self.record, "project_name": "La cité"}
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            self.store.records.put(c, value["room_id"], raw, state="open", updated_at=value["updated_at"])
        self.assertEqual(self.store.read(value["room_id"]), value)
        with ControlDatabase(self.config) as db, db.transaction() as c:
            row = self.store.records.read(c, value["room_id"])
        self.assertEqual(row["raw"], raw)
        self.assertEqual(row["digest"], hashlib.sha256(raw).hexdigest())
        self.store.save({**value, "lifecycle": "ended"}, expected_digest=row["digest"])

    def test_failed_transaction_never_publishes_half_a_batch(self):
        registry = RecordRegistry("rooms")
        with ControlDatabase(self.config) as db:
            with self.assertRaises(RuntimeError), db.transaction(write=True) as c:
                registry.put(c, self.record["room_id"], RoomStore._raw(self.record))
                raise RuntimeError("import interrupted")
        self.assertEqual(self.store.list(), [])

    def test_bounded_snapshot_reports_truncation(self):
        self.store.create(self.record)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=1)
        self.assertEqual(self.store.bounded_snapshots(budget), [self.record])
        self.assertTrue(budget.truncated)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=10)
        self.assertEqual(self.store.bounded_snapshots(budget), [self.record])
        self.assertTrue(budget.summary()["complete"])

    def test_active_snapshot_excludes_ended_rooms_before_limiting(self):
        for index in range(12):
            value = {**self.record, "room_id": str(uuid.uuid4()), "lifecycle": "ended",
                     "name": f"Old Room {index}", "slug": f"old-room-{index}"}
            self.store.create(value)
        self.store.create(self.record)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=2)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [self.record])
        self.assertTrue(budget.summary()["complete"])

    def test_corrupt_active_room_is_counted_without_hiding_a_valid_room(self):
        self.store.create(self.record)
        other = {**self.record, "room_id": str(uuid.uuid4()), "slug": "other", "name": "Other"}
        self.store.create(other)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET digest=? WHERE domain='rooms' AND record_key=?", ("f" * 64, self.record["room_id"]))
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=10)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [other])
        self.assertEqual(budget.unavailable, 1)

    def test_open_room_precedes_stuck_creations_at_the_cap(self):
        opened = {**self.record, "lifecycle": "open"}
        self.store.create(opened)
        for index in range(3):
            self.store.create({**self.record, "room_id": str(uuid.uuid4()), "lifecycle": "creating",
                               "name": f"Starting {index}", "slug": f"starting-{index}"})
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=1)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [opened])
        self.assertTrue(budget.truncated)

    def test_corrupt_digest_is_reported_instead_of_importing_a_different_identity(self):
        self.store.create(self.record)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET digest=? WHERE domain='rooms'", ("b" * 64,))
        with self.assertRaisesRegex(RoomError, "digest mismatch"):
            self.store.read(self.record["room_id"])

    def test_malformed_json_and_stale_cas_cannot_change_retained_records(self):
        registry = RecordRegistry("fixture")
        with ControlDatabase(self.config) as db:
            for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'):
                with self.assertRaises(StoreError), db.transaction(write=True) as c:
                    registry.put(c, "first", raw)
            with db.transaction(write=True) as c:
                registry.put(c, "first", b'{"a":1}\n')
            with self.assertRaises(StoreError), db.transaction(write=True) as c:
                registry.put(c, "first", b'{"a":2}\n', expected_digest="wrong")
            with db.transaction() as c:
                self.assertEqual(registry.read(c, "first")["value"], {"a": 1})
