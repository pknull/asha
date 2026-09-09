import stat
import io
import os
from contextlib import redirect_stdout
import unittest
from unittest import mock

from lib.control.database import ControlDatabase
from lib.control.registry_activation import activate_registries, recover_activation, rollback_registries
from lib.control.registry_backend import selected_backend
from lib.control.registry_migration import stage_registries
from lib.control.registry_stage_validation import checked_stage
from lib.control.registry_tree import capture_tree
from lib.control.registry_guards import migration_lock
from lib.control.sqlite_tasks import SQLiteTaskStore
from lib.control.store import StoreError, TaskStore
from tests.python import test_control_registry_stage as stage_tests


class RegistryActivationTests(unittest.TestCase):
    staged_config = stage_tests.RegistryStageTests.staged_config
    initiative_fixture = stage_tests.RegistryStageTests.initiative_fixture

    def setUp(self):
        stage_tests.RegistryStageTests.setUp(self)
        self.probe = mock.patch("lib.control.registry_activation._owned_state", return_value=("open", "verified"))
        self.probe.start()
        self.addCleanup(self.probe.stop)
        # Frozen fixture directories must be restored for TemporaryDirectory.
        self.addCleanup(self.thaw_fixture)

    def thaw_fixture(self):
        for path in self.root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)

    def stage(self):
        return stage_registries(self.config, self.target)

    def fail_at(self, point):
        def inject(current):
            if current == point:
                raise RuntimeError("injected " + point)
        return inject

    def test_stage_authentication_covers_initiative_records_and_artifacts(self):
        self.initiative_fixture()
        manifest = self.stage()
        with checked_stage(self.config, self.target) as stage:
            self.assertEqual(len(stage["records"]), sum(manifest["counts"].values()))

    def test_activation_preserves_database_inode_and_source_payloads(self):
        path = self.config.tasks_dir / (self.task["task_id"] + ".json")
        raw = path.read_bytes()
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "retained", {"message": "keep"})
            inode = (self.config.tasks_dir.parent / "control.sqlite3").stat().st_ino
            self.stage()
            marker = activate_registries(self.config, self.target)
            self.assertEqual(db.get("fixture", "project", "retained"), {"message": "keep"})
            self.assertEqual(db.get("registry-backend", "control", "active"), marker)
        self.assertEqual((self.config.tasks_dir.parent / "control.sqlite3").stat().st_ino, inode)
        self.assertEqual(selected_backend(self.config), "sqlite")
        self.assertIsInstance(TaskStore(self.config), SQLiteTaskStore)
        self.assertEqual(TaskStore(self.config).read(self.task["task_id"]), self.task)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)

    def test_interrupted_activation_resumes_and_publishes_once(self):
        self.stage()
        with self.assertRaisesRegex(RuntimeError, "injected"):
            activate_registries(self.config, self.target, failure_injector=self.fail_at("imported"))
        with self.assertRaisesRegex(StoreError, "incomplete"):
            TaskStore(self.config)
        with ControlDatabase(self.config) as db:
            self.assertEqual(db.list("tasks"), [])
        marker = recover_activation(self.config)
        self.assertEqual(recover_activation(self.config), marker)
        self.assertEqual(activate_registries(self.config, self.target), marker)

    def test_abort_partial_freeze_restores_original_modes(self):
        self.stage()
        with self.assertRaises(RuntimeError):
            activate_registries(self.config, self.target, failure_injector=self.fail_at("mode:tasks"))
        recover_activation(self.config, action="abort")
        self.assertEqual(selected_backend(self.config), "files")
        self.assertEqual(TaskStore(self.config).read(self.task["task_id"]), self.task)
        self.assertTrue(all(entry["mode"] == (0o700 if entry["kind"] == "directory" else 0o600)
                            for entry in capture_tree(self.config)))

    def test_prewrite_rollback_refuses_stale_sql_store_and_can_be_restaged(self):
        self.stage()
        activate_registries(self.config, self.target)
        stale = TaskStore(self.config)
        rollback_registries(self.config)
        self.assertEqual(selected_backend(self.config), "files")
        self.assertEqual(TaskStore(self.config).read(self.task["task_id"]), self.task)
        with self.assertRaisesRegex(StoreError, "inactive"):
            stale.save(self.task)
        self.target = self.root / "stage-again"
        self.stage()
        activate_registries(self.config, self.target)
        self.assertEqual(selected_backend(self.config), "sqlite")

    def test_rollback_refuses_new_sql_work(self):
        self.stage()
        activate_registries(self.config, self.target)
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "new", {"message": "preserve"})
        with self.assertRaisesRegex(StoreError, "changed|new writes"):
            rollback_registries(self.config)
        self.assertEqual(selected_backend(self.config), "sqlite")

    def test_rollback_refuses_new_artifacts_even_without_a_registry_write(self):
        self.stage()
        activate_registries(self.config, self.target)
        artifact = self.config.tasks_dir.parent / "artifacts"
        artifact.mkdir(mode=0o700)
        with self.assertRaisesRegex(StoreError, "artifact"):
            rollback_registries(self.config)
        self.assertEqual(selected_backend(self.config), "sqlite")

    def test_room_mismatch_refuses_before_freeze(self):
        self.stage()
        before = capture_tree(self.config)
        with mock.patch("lib.control.registry_activation._owned_state", return_value=("mismatch", "foreign pane")):
            with self.assertRaisesRegex(StoreError, "Room"):
                activate_registries(self.config, self.target)
        self.assertEqual(capture_tree(self.config), before)

    def test_source_change_refuses_before_freeze(self):
        self.stage()
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "new", {"text": "arrived"})
        with self.assertRaisesRegex(StoreError, "changed"):
            activate_registries(self.config, self.target)
        self.assertEqual(selected_backend(self.config), "files")

    def test_interrupted_rollback_resumes(self):
        self.stage()
        activate_registries(self.config, self.target)
        with self.assertRaises(RuntimeError):
            rollback_registries(self.config, failure_injector=self.fail_at("thawed"))
        with self.assertRaisesRegex(StoreError, "incomplete"):
            TaskStore(self.config)
        result = recover_activation(self.config)
        self.assertEqual(result["state"], "rolled-back")
        self.assertEqual(selected_backend(self.config), "files")

    def test_read_only_status_reports_partial_activation_and_recovery_guidance(self):
        from lib.control.registry_cli import status
        from lib.control.doctor import _registry_backend_probe
        self.stage()
        with self.assertRaises(RuntimeError):
            activate_registries(self.config, self.target, failure_injector=self.fail_at("prepared"))
        before = capture_tree(self.config)
        result = status(self.config)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(_registry_backend_probe(self.config).outcome, "mismatch")
        self.assertEqual(capture_tree(self.config), before)
        recover_activation(self.config)
        result = status(self.config)
        self.assertEqual(result["counts"]["tasks"], 1)
        self.assertEqual(_registry_backend_probe(self.config).outcome, "match")

    def test_cli_blocks_actor_mutation_but_allows_status(self):
        from lib.control.registry_cli import main
        env = {"HOME": str(self.root), "ASHA_HOME": str(self.config.asha_home),
               "ASHA_MANAGED_SESSION_ID": self.room["room_id"]}
        with self.assertRaisesRegex(StoreError, "managed actors"):
            main(["stage", "--stage-home", str(self.target)], env=env)
        self.assertFalse(self.target.exists())
        with redirect_stdout(io.StringIO()) as output:
            main(["status", "--json"], env=env)
        self.assertIn('"backend":"files"', output.getvalue())

    def test_raw_sql_writes_from_existing_connection_are_refused_after_rollback(self):
        self.stage()
        activate_registries(self.config, self.target)
        with ControlDatabase(self.config) as stale:
            rollback_registries(self.config)
            with self.assertRaisesRegex(StoreError, "inactive"):
                stale.put("tasks", "registry", self.task["task_id"], self.task)
        self.assertEqual(selected_backend(self.config), "files")

    def test_existing_sql_store_is_fenced_while_migration_holds_writer_lock(self):
        self.stage()
        activate_registries(self.config, self.target)
        stale = TaskStore(self.config)
        with migration_lock(self.config, exclusive=True):
            with self.assertRaisesRegex(StoreError, "writer or migration"):
                stale.save(self.task)

    def test_inflight_sql_writer_prevents_rollback(self):
        self.stage()
        activate_registries(self.config, self.target)
        with TaskStore(self.config).transaction_lock(self.task["task_id"]):
            with self.assertRaisesRegex(StoreError, "writer or migration"):
                rollback_registries(self.config)
        self.assertEqual(selected_backend(self.config), "sqlite")

    def test_recovery_refuses_changed_source_inode_without_thawing_it(self):
        self.stage()
        with self.assertRaises(RuntimeError):
            activate_registries(self.config, self.target, failure_injector=self.fail_at("frozen"))
        path = self.config.tasks_dir / (self.task["task_id"] + ".json")
        raw = path.read_bytes()
        path.parent.chmod(0o700)
        path.rename(path.with_suffix(".old"))
        path.write_bytes(raw)
        path.chmod(0o400)
        path.with_suffix(".old").unlink()
        path.parent.chmod(0o500)
        with self.assertRaisesRegex(StoreError, "changed"):
            recover_activation(self.config, action="abort")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)

    def test_activation_refuses_tampered_retained_backup(self):
        self.stage()
        backup = self.target / "source-database.sqlite3"
        raw = bytearray(backup.read_bytes())
        raw[-1] ^= 1
        backup.write_bytes(raw)
        with self.assertRaisesRegex(StoreError, "snapshot bytes changed"):
            activate_registries(self.config, self.target)
        self.assertEqual(selected_backend(self.config), "files")

    def test_activation_refuses_missing_stage_ledger_page(self):
        self.stage()
        with ControlDatabase(self.staged_config()) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-migration-ledger' AND scope='records'")
        with self.assertRaisesRegex(StoreError, "count or digest"):
            activate_registries(self.config, self.target)
        self.assertEqual(selected_backend(self.config), "files")

    def test_lost_commit_response_reports_active_on_recovery(self):
        self.stage()
        with self.assertRaises(RuntimeError):
            activate_registries(self.config, self.target, failure_injector=self.fail_at("committed"))
        self.assertEqual(recover_activation(self.config)["state"], "active")

    def test_lost_rollback_commit_response_reports_completed_outcome(self):
        self.stage()
        activate_registries(self.config, self.target)
        with self.assertRaises(RuntimeError):
            rollback_registries(self.config, failure_injector=self.fail_at("committed"))
        self.assertEqual(recover_activation(self.config)["state"], "rolled-back")
        self.assertEqual(selected_backend(self.config), "files")

    def test_room_actor_can_read_status_but_cannot_change_registry_authority(self):
        from lib.control.registry_cli import main
        env = {"HOME": str(self.root), "ASHA_HOME": str(self.config.asha_home), "ASHA_ROOM_ID": self.room["room_id"]}
        for command in (["stage", "--stage-home", str(self.target)],
                        ["activate", "--stage-home", str(self.target)], ["recover"], ["rollback"]):
            with self.subTest(command=command), self.assertRaisesRegex(StoreError, "Room actors"):
                main(command, env=env)
        with redirect_stdout(io.StringIO()) as output:
            main(["status", "--json"], env=env)
        self.assertIn('"backend":"files"', output.getvalue())

    def test_cli_reports_filesystem_failure_as_a_refusal(self):
        from lib.control.registry_cli import main
        env = {"HOME": str(self.root), "ASHA_HOME": str(self.config.asha_home)}
        with mock.patch("lib.control.registry_cli.stage_registries", side_effect=OSError("injected IO failure")):
            with self.assertRaisesRegex(StoreError, "registry operation refused"):
                main(["stage", "--stage-home", str(self.target)], env=env)

    def test_aborting_partial_rollback_restores_sqlite_authority(self):
        self.stage()
        activate_registries(self.config, self.target)
        with self.assertRaises(RuntimeError):
            rollback_registries(self.config, failure_injector=self.fail_at("thawed"))
        self.assertEqual(recover_activation(self.config, action="abort")["state"], "active")
        self.assertEqual(selected_backend(self.config), "sqlite")
        self.assertEqual(stat.S_IMODE(self.config.tasks_dir.stat().st_mode), 0o500)

    def test_abort_preserves_new_source_file_from_an_older_writer(self):
        self._old_source_write_then_abort("prepared")

    def test_abort_preserves_extra_file_inside_a_directory_that_finished_freezing(self):
        self._old_source_write_then_abort("mode:tasks")

    def _old_source_write_then_abort(self, injection_point):
        from lib.control.prune import PruneRecordStore
        writer = PruneRecordStore(self.config)
        self.stage()
        def older_write(point):
            if point == injection_point:
                # The old implementation predates the shared migration guard.
                writer._write_legacy(self.task["task_id"], {"workspace_removed": True})
        with self.assertRaisesRegex(StoreError, "membership changed"):
            activate_registries(self.config, self.target, failure_injector=older_write)
        path = writer.path(self.task["task_id"])
        raw = path.read_bytes()
        if injection_point == "mode:tasks":
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o500)
        recover_activation(self.config, action="abort")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(selected_backend(self.config), "files")

    def test_legacy_initiative_layout_and_ingestion_lock_creation_join_migration_fence(self):
        from tests.python.test_orchestration_model import INITIATIVE_ID
        store = self.initiative_fixture()
        before = capture_tree(self.config)
        with migration_lock(self.config, exclusive=True):
            with self.assertRaisesRegex(StoreError, "writer or migration"):
                store.read_initiative(INITIATIVE_ID)
            with self.assertRaisesRegex(StoreError, "writer or migration"):
                with store.result_ingestion_lock(INITIATIVE_ID, self.task["task_id"]):
                    self.fail("legacy ingestion lock should have been refused")
        self.assertEqual(capture_tree(self.config), before)

    def test_cached_legacy_prune_writer_is_refused_before_source_mutation(self):
        from lib.control.prune import PruneRecordStore
        writer = PruneRecordStore(self.config)
        self.stage()
        def write(point):
            if point == "prepared":
                writer.write(self.task["task_id"], {"workspace_removed": True})
        with self.assertRaisesRegex(StoreError, "writer or migration"):
            activate_registries(self.config, self.target, failure_injector=write)
        self.assertFalse(writer.path(self.task["task_id"]).exists())
        recover_activation(self.config)
        with self.assertRaisesRegex(StoreError, "legacy registry writes are inactive"):
            writer.write(self.task["task_id"], {"workspace_removed": True})

    def test_current_task_writer_cannot_create_source_lock_during_preparation(self):
        from lib.control.store import task_digest
        writer = TaskStore(self.config)
        before = capture_tree(self.config)
        with migration_lock(self.config, exclusive=True):
            with self.assertRaisesRegex(StoreError, "writer or migration"):
                writer.save(self.task, expected_digest=task_digest(self.task))
        self.assertEqual(capture_tree(self.config), before)

    def test_legacy_journal_and_sidecar_writers_are_fenced_before_any_source_write(self):
        from types import SimpleNamespace
        from lib.control.transaction import CreationJournalStore, JournalError, MaterializationOwnershipStore
        from tests.python.test_control_increment2 import JournalStoreTests
        fixture = SimpleNamespace(temp=SimpleNamespace(name=str(self.root)), config=self.config, task_id=self.task['task_id'])
        journal = JournalStoreTests.journal(fixture)
        journals = CreationJournalStore(self.config)
        ownership = MaterializationOwnershipStore(self.config)
        before = capture_tree(self.config)
        with migration_lock(self.config, exclusive=True):
            with self.assertRaisesRegex(JournalError, "writer or migration"):
                journals.save(journal)
            with self.assertRaisesRegex(JournalError, "writer or migration"):
                ownership.write(self.task['task_id'], 'a' * 64, [[1, 2, 3, 4]])
        self.assertEqual(capture_tree(self.config), before)

    def test_rollback_status_explains_resume_and_abort_differently(self):
        from lib.control.registry_cli import main
        self.stage()
        activate_registries(self.config, self.target)
        with self.assertRaises(RuntimeError):
            rollback_registries(self.config, failure_injector=self.fail_at("thawed"))
        env = {"HOME": str(self.root), "ASHA_HOME": str(self.config.asha_home)}
        with redirect_stdout(io.StringIO()) as output:
            main(["status"], env=env)
        self.assertIn("Interrupted operation: rollback", output.getvalue())
        self.assertIn("Resume will finish rollback; abort will restore SQLite authority", output.getvalue())


class AuxiliaryActivationTests(unittest.TestCase):
    staged_config = stage_tests.RegistryStageTests.staged_config
    thaw_fixture = RegistryActivationTests.thaw_fixture

    def setUp(self):
        from tests.python.test_control_auxiliary_stage import AuxiliaryStageTests
        AuxiliaryStageTests.setUp(self)
        self.addCleanup(self.thaw_fixture)

    @mock.patch("lib.control.registry_activation._owned_state", return_value=("open", "verified"))
    def test_auxiliary_records_and_ownership_survive_real_cutover_and_rollback(self, _probe):
        from lib.control.transaction import CreationJournalStore, MaterializationOwnershipStore
        from lib.control.jj import ColocationIntentStore
        stage_registries(self.config, self.target)
        activate_registries(self.config, self.target)
        self.assertEqual(CreationJournalStore(self.config).read(self.task["task_id"]), self.journal)
        self.assertEqual(MaterializationOwnershipStore(self.config).read(self.ownership), [[1, 2, 3, 4]])
        self.assertEqual(ColocationIntentStore(self.config).read(self.repository), self.intent)
        self.assertEqual(stat.S_IMODE(os.stat(self.ownership["path"]).st_mode), 0o600)
        rollback_registries(self.config)
        self.assertEqual(MaterializationOwnershipStore(self.config).read(self.ownership), [[1, 2, 3, 4]])
        self.assertEqual(stat.S_IMODE(self.authority_path.stat().st_mode), 0o644)
