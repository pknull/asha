import copy
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from lib.control.config import load_config
from lib.control.database import ControlDatabase
from lib.control.sqlite_tasks import SQLiteTaskStore
from lib.control.store import SnapshotBudget, StoreError, task_digest
from tests.python.test_control_config_model import task_record


class SQLiteTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config({"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")})
        with ControlDatabase(self.config, create=True):
            pass
        self.store = SQLiteTaskStore(self.config)
        self.record = task_record(repository_root=str(self.root / "source"),
                                  workspace_path=str(self.config.workspace_root / "repo" / "control-test"))

    def test_lifecycle_roundtrip_preserves_digests_and_uses_no_json_registry(self):
        location = self.store.save(self.record)
        self.assertEqual(location, self.config.tasks_dir.parent / "control.sqlite3")
        self.assertFalse(self.config.tasks_dir.exists())
        self.assertEqual(self.store.read(self.record["task_id"]), self.record)
        updated = {**self.record, "lifecycle": "failed"}
        with self.store.transaction_lock(self.record["task_id"]):
            self.store.save(updated, expected_digest=task_digest(self.record))
        self.assertEqual(self.store.resolve(self.record["slug"]), updated)
        with self.assertRaisesRegex(StoreError, "digest"):
            self.store.save(self.record, expected_digest=task_digest(self.record))

    def test_existing_update_guards_apply_to_sqlite(self):
        self.store.save(self.record)
        invalid = copy.deepcopy(self.record)
        invalid["label"] = "changed immutable field"
        with self.assertRaisesRegex(StoreError, "immutable"):
            self.store.save(invalid, expected_digest=task_digest(self.record))
        invalid = copy.deepcopy(self.record)
        invalid["lifecycle"] = "failed"
        invalid["runs"] = []
        with self.assertRaisesRegex(StoreError, "runs"):
            self.store.save(invalid, expected_digest=task_digest(self.record))
        with self.assertRaisesRegex(StoreError, "expected digest"):
            self.store.save(self.record)
        for digest in (42, "é" * 64, "bad"):
            with self.assertRaisesRegex(StoreError, "expected digest"):
                self.store.save(self.record, expected_digest=digest)
        self.assertEqual(self.store.read(self.record["task_id"]), self.record)

    def test_bounded_projection_and_invalid_records_remain_visible(self):
        self.store.save(self.record)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=1)
        self.assertEqual(self.store.bounded_snapshots(budget), [self.record])
        self.assertTrue(budget.truncated)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET digest=? WHERE domain='tasks'", ("f" * 64,))
        self.assertEqual(self.store.list(), [])
        self.assertEqual(len(self.store.skipped), 1)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=10)
        self.assertEqual(self.store.bounded_snapshots(budget), [])
        self.assertEqual(budget.unavailable, 1)

    def test_no_invalid_new_record_is_published(self):
        invalid = copy.deepcopy(self.record)
        invalid["lifecycle"] = []
        with self.assertRaises(StoreError):
            self.store.save(invalid)
        self.assertEqual(self.store.list(), [])

    def test_active_snapshot_filters_history_before_the_cap(self):
        for _ in range(12):
            value = copy.deepcopy(self.record)
            value["task_id"] = str(uuid.uuid4())
            value["lifecycle"] = "ended"
            for run in value["runs"]:
                run["state"] = "exited"
            self.store.save(value)
        self.store.save(self.record)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=2)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [self.record])
        self.assertTrue(budget.summary()["complete"])

    def test_active_snapshot_reports_a_selected_corrupt_record(self):
        self.store.save(self.record)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET digest=? WHERE domain='tasks'", ("f" * 64,))
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=2)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [])
        self.assertEqual(budget.unavailable, 1)

    def test_failed_task_preserving_a_live_run_stays_in_active_candidates(self):
        value = {**self.record, "lifecycle": "failed"}
        self.store.save(value)
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=2)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [value])

    def test_running_task_precedes_stuck_creations_at_the_cap(self):
        self.store.save(self.record)
        for _ in range(3):
            self.store.save({**self.record, "task_id": str(uuid.uuid4()), "lifecycle": "creating", "runs": []})
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=1)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [self.record])
        self.assertTrue(budget.truncated)

    def test_missing_activity_projection_is_unavailable_instead_of_empty(self):
        self.store.save(self.record)
        with ControlDatabase(self.config) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET state='' WHERE domain='tasks'")
        budget = SnapshotBudget(deadline=time.monotonic() + 2, limit=2)
        self.assertEqual(self.store.bounded_active_snapshots(budget), [])
        self.assertEqual(budget.unavailable, 1)
        self.assertFalse(budget.summary()["complete"])

    def test_active_root_key_selection_uses_the_state_index_without_sorting(self):
        with ControlDatabase(self.config) as db:
            statements = []
            db._live().set_trace_callback(statements.append)
            with db.transaction() as c:
                self.store.records.active_root_keys(c, ("creating", "running"), limit=2)
                for sql in statements[:]:
                    if sql.startswith("SELECT") and "FROM records WHERE domain=" in sql:
                        plan = " ".join(row[3] for row in c.execute("EXPLAIN QUERY PLAN " + sql))
                        self.assertTrue("records_state" in plan or "records_scope_state" in plan, plan)
                        self.assertNotIn("TEMP B-TREE", plan)
