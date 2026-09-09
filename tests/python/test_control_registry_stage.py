from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lib.control.config import load_config
from lib.control.database import ControlDatabase
from lib.control.registry_migration import stage_registries
from lib.control.rooms import RoomStore
from lib.control.runtime import set_admission
from lib.control.sqlite_rooms import SQLiteRoomStore
from lib.control.sqlite_tasks import SQLiteTaskStore
from lib.control.stage_ledger import iter_ledger, write_ledger
from lib.control.store import StoreError, TaskStore
from tests.python.test_control_config_model import task_record
from lib.control.initiative_migration import _RECORDS
from lib.control.orchestration import model
from lib.control.orchestration.config import from_control
from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
from lib.control.orchestration.store import InitiativeStore, ObservationOnlyPlanError
from tests.python.test_orchestration_model import (HISTORICAL_PLAN_FIXTURE,
    INITIATIVE_ID, initiative, node, plan)
from tests.python import test_orchestration_model as model_tests
from tests.python.test_orchestration_store import event


class RegistryStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config({"HOME": str(self.root), "ASHA_HOME": str(self.root / "source-home")})
        self.target = self.root / "stage-home"
        self.task = task_record(repository_root=str(self.root / "project"),
            workspace_path=str(self.config.workspace_root / "repo" / "control-test"))
        self.task["lifecycle"] = "failed"
        TaskStore(self.config).save(self.task)
        self.room = {"contract": "asha.room.v1", "room_id": "11111111-1111-4111-8111-111111111111",
            "name": "Draft Room", "slug": "draft-room", "project_id": "novel", "project_name": "A Novel",
            "project_root": str(self.root), "harness": "claude",
            "tmux": {"session": "asha-draft", "session_id": "$7", "window": "room", "pane_id": "%42"},
            "created_at": "2026-09-08T12:00:00Z", "updated_at": "2026-09-08T12:00:00Z",
            "lifecycle": "open", "prompt_digest": "a" * 64}
        RoomStore(self.config).create(self.room)
        set_admission(self.config, "paused")

    def staged_config(self):
        return replace(self.config, asha_home=self.target, tasks_dir=self.target / "state/control/tasks")

    def test_stage_preserves_source_bytes_and_existing_database_without_activating(self):
        paths = [self.config.tasks_dir / (self.task["task_id"] + ".json"),
                 self.config.tasks_dir.parent / "rooms" / (self.room["room_id"] + ".json")]
        before = [path.read_bytes() for path in paths]
        with ControlDatabase(self.config) as db:
            db.put("fixture", "project", "retained", {"message": "keep me"})
        manifest = stage_registries(self.config, self.target)
        self.assertEqual(manifest["state"], "staged")
        self.assertEqual(manifest["counts"]["tasks"], 1)
        self.assertEqual(manifest["counts"]["rooms"], 1)
        self.assertEqual(sum(manifest["counts"].values()), 2)
        self.assertEqual(hashlib.sha256((self.target / manifest["source_database_snapshot"]).read_bytes()).hexdigest(),
                         manifest["source_database_digest"])
        self.assertEqual(manifest["external_rooms"], {"open_room_count": 1,
            "liveness": "not-probed", "activation_requires_revalidation": True})
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(SQLiteTaskStore(self.staged_config()).read(self.task["task_id"]), self.task)
        self.assertEqual(SQLiteRoomStore(self.staged_config()).read(self.room["room_id"]), self.room)
        with ControlDatabase(self.staged_config()) as db:
            self.assertEqual(db.get("fixture", "project", "retained"), {"message": "keep me"})
            self.assertIsNone(db.get("registry-backend", "control", "active"))
            with db.transaction() as c:
                imported = c.execute("SELECT payload FROM records WHERE domain='tasks'").fetchone()[0].encode()
                self.assertEqual(c.execute("SELECT mode FROM control_runtime").fetchone()[0], "paused")
            self.assertEqual(imported, before[0])

    def test_stage_v3_binds_source_tree_inodes_modes_and_database_state(self):
        from lib.control.registry_tree import capture_tree
        from lib.control.registry_snapshot import state_digest
        expected_tree = capture_tree(self.config)
        with ControlDatabase(self.config) as db, db.transaction() as c:
            expected_state = state_digest(c)
        manifest = stage_registries(self.config, self.target)
        self.assertEqual(manifest["contract"], "asha.registry-stage.v3")
        self.assertEqual(manifest["source_database_state"], expected_state)
        with ControlDatabase(self.staged_config()) as db, db.transaction() as c:
            self.assertEqual(list(iter_ledger(c, "source", manifest["source_tree"])), expected_tree)

    def test_identical_bytes_on_replaced_task_inode_invalidate_source_snapshot(self):
        replaced = False
        def replace_file(domain, key):
            nonlocal replaced
            if domain == "tasks" and not replaced:
                path = self.config.tasks_dir / (key + ".json")
                raw = path.read_bytes()
                previous = path.with_suffix(".old")
                path.rename(previous)
                path.write_bytes(raw)
                path.chmod(0o600)
                previous.unlink()
                replaced = True
        with self.assertRaisesRegex(StoreError, "snapshot changed"):
            stage_registries(self.config, self.target, after_import=replace_file)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))

    def test_malformed_records_abort_without_a_stage_manifest(self):
        (self.config.tasks_dir / "malformed.json").write_text("{}")
        with self.assertRaisesRegex(StoreError, "record name"):
            stage_registries(self.config, self.target)
        target_db = self.target / "state/control/control.sqlite3"
        if target_db.exists():
            with ControlDatabase(self.staged_config()) as db:
                self.assertIsNone(db.get("registry-migration", "control", "stage"))
                self.assertEqual(db.list("tasks"), [])

    def test_interrupted_batch_is_not_published(self):
        set_admission(self.config, "draining")
        def interrupt(_domain, _key):
            raise RuntimeError("interrupted import")
        with self.assertRaisesRegex(RuntimeError, "interrupted import"):
            stage_registries(self.config, self.target, after_import=interrupt)
        with ControlDatabase(self.staged_config()) as db:
            self.assertEqual(db.list("tasks"), [])
            self.assertEqual(db.list("rooms"), [])
            self.assertIsNone(db.get("registry-migration", "control", "stage"))
            self.assertEqual(db.get("registry-migration", "control", "attempt")["state"], "incomplete")
            with db.transaction() as c:
                self.assertEqual(c.execute("SELECT mode FROM control_runtime").fetchone()[0], "paused")

    def test_linked_file_registry_records_are_refused(self):
        for kind in ("symlink", "hardlink"):
            for domain, identity in (("tasks", self.task["task_id"]), ("rooms", self.room["room_id"])):
                with self.subTest(kind=kind, domain=domain):
                    path = self.config.tasks_dir.parent / domain / (identity + ".json")
                    raw = path.read_bytes()
                    outside = self.root / (domain + "-" + kind + ".json")
                    outside.write_bytes(raw)
                    outside.chmod(0o600)
                    path.unlink()
                    if kind == "symlink":
                        path.symlink_to(outside)
                    else:
                        os.link(outside, path)
                    with self.assertRaises(StoreError):
                        stage_registries(self.config, self.root / (domain + "-" + kind + "-stage"))
                    path.unlink()
                    path.write_bytes(raw)
                    path.chmod(0o600)

    def test_noncooperating_source_write_invalidates_the_stage(self):
        def change_source(_domain, _key):
            (self.config.tasks_dir / "unexpected.json").write_text("{}")
        with self.assertRaisesRegex(StoreError, "membership changed"):
            stage_registries(self.config, self.target, after_import=change_source)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))
            self.assertEqual(db.list("tasks"), [])

    def test_source_database_write_invalidates_the_stage(self):
        def change_source(_domain, _key):
            with ControlDatabase(self.config) as db:
                db.put("fixture", "project", "new-message", {"text": "arrived during snapshot"})
        with self.assertRaisesRegex(StoreError, "source database changed"):
            stage_registries(self.config, self.target, after_import=change_source)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))

    def test_mixed_authority_is_refused_instead_of_merging_unaccounted_rows(self):
        with ControlDatabase(self.config) as db:
            db.put("tasks", "unrelated", "old-row", {"text": "unaccounted registry data"})
        with self.assertRaisesRegex(StoreError, "reconcile domain authority"):
            stage_registries(self.config, self.target)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))

    def test_replaced_registry_directory_is_not_mistaken_for_the_locked_snapshot(self):
        changed = False
        def replace_source(_domain, _key):
            nonlocal changed
            if not changed:
                rooms = self.config.tasks_dir.parent / "rooms"
                rooms.rename(rooms.with_name("retired-rooms"))
                rooms.mkdir(mode=0o700)
                changed = True
        with self.assertRaisesRegex(StoreError, "changed identity"):
            stage_registries(self.config, self.target, after_import=replace_source)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))

    def test_stage_refuses_running_admission_and_existing_destinations(self):
        set_admission(self.config, "running")
        with self.assertRaisesRegex(StoreError, "pause"):
            stage_registries(self.config, self.target)
        self.assertFalse(self.target.exists())
        set_admission(self.config, "paused")
        stage_registries(self.config, self.target)
        with self.assertRaisesRegex(StoreError, "empty"):
            stage_registries(self.config, self.target)

    def initiative_fixture(self):
        store = InitiativeStore(from_control(self.config))
        store.save_initiative(initiative())
        store.save_node(INITIATIVE_ID, node())
        store.save_plan(INITIATIVE_ID, plan())
        store.append_event(INITIATIVE_ID, event(1, self.room["room_id"], initiative()["updated_at"]))
        store.write_assignment(INITIATIVE_ID, self.task["task_id"], b"Review chapter one.\n")
        store.save_output(INITIATIVE_ID, self.task["task_id"], b"Retained output\x00\xff")
        return store

    def test_initiative_stage_preserves_records_artifacts_and_historical_authority(self):
        store = self.initiative_fixture()
        root = store.config.initiatives_dir / INITIATIVE_ID
        (root / "plans/0001.json").write_bytes(HISTORICAL_PLAN_FIXTURE.read_bytes())
        # Exercise the remaining established contract families, including exact
        # seals, reviews, approvals and the retained coordinator generation.
        for validator, value in model_tests.OrchestrationModelTests().contract_records():
            if validator is model.validate_event:
                continue
            directory, (_, field) = next((directory, spec) for directory, spec in _RECORDS.items() if spec[0] is validator)
            path = root / directory / (value[field] + ".json")
            path.write_text(json.dumps(value, ensure_ascii=False, indent=3) + "\n")
            path.chmod(0o600)
        before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        manifest = stage_registries(self.config, self.target)
        after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        staged = SQLiteInitiativeStore(from_control(self.staged_config()))
        self.assertEqual(staged.peek(INITIATIVE_ID), store.peek(INITIATIVE_ID))
        self.assertEqual(staged.verify_events(INITIATIVE_ID), store.verify_events(INITIATIVE_ID))
        self.assertEqual(staged.read_plan_snapshot(INITIATIVE_ID, 1), store.read_plan_snapshot(INITIATIVE_ID, 1))
        with self.assertRaises(ObservationOnlyPlanError):
            staged.read_plan(INITIATIVE_ID, 1)
        self.assertEqual(staged.read_output(INITIATIVE_ID, self.task["task_id"]), b"Retained output\x00\xff")
        self.assertEqual(manifest["artifacts"]["entry_count"], 2)
        with ControlDatabase(self.staged_config()) as db, db.transaction() as c:
            for item in iter_ledger(c, "records", manifest["records"]):
                if "path" not in item:
                    continue
                row = c.execute("SELECT payload,digest FROM records WHERE domain=? AND scope=? AND record_key=?",
                                (item["domain"], item["scope"], item["key"])).fetchone()
                self.assertEqual(row[0].encode(), before[item["path"].split("/", 2)[2]])
                self.assertEqual(row[1], item["digest"])
        target_root = staged.config.initiatives_dir / INITIATIVE_ID
        self.assertFalse((target_root / "initiative.json").exists())
        self.assertEqual((target_root / "assignments" / (self.task["task_id"] + ".md")).read_bytes(), b"Review chapter one.\n")

    def test_initiative_event_gap_aborts_the_whole_stage(self):
        store = self.initiative_fixture()
        next((store.config.initiatives_dir / INITIATIVE_ID / "events").iterdir()).unlink()
        with self.assertRaisesRegex(StoreError, "sequence disagrees"):
            stage_registries(self.config, self.target)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))
            self.assertEqual(db.list("tasks"), [])
            self.assertEqual(db.list("initiatives"), [])

    def test_initiative_source_mutation_aborts_the_whole_stage(self):
        store = self.initiative_fixture()
        def change_source(domain, key):
            if domain == "initiative.nodes":
                path = store.config.initiatives_dir / INITIATIVE_ID / "outputs" / (self.task["task_id"] + ".bin")
                # Mutate an already imported assignment as well as an output
                # which may not have been enumerated yet.
                assignment = path.parent.parent / "assignments" / (self.task["task_id"] + ".md")
                assignment.write_bytes(b"changed after its copy")
        with self.assertRaisesRegex(StoreError, "source changed"):
            stage_registries(self.config, self.target, after_import=change_source)
        with ControlDatabase(self.staged_config()) as db:
            self.assertIsNone(db.get("registry-migration", "control", "stage"))

    def test_live_legacy_coordinator_refuses_staging(self):
        store = self.initiative_fixture()
        value = next(value for validator, value in model_tests.OrchestrationModelTests().contract_records() if validator is model.validate_coordinator)
        path = store.config.initiatives_dir / INITIATIVE_ID / "coordinators" / (value["coordinator_id"] + ".json")
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        with mock.patch("lib.control.initiative_migration.process_live", return_value=True):
            with self.assertRaisesRegex(StoreError, "stop live initiative coordinators"):
                stage_registries(self.config, self.target)

    def test_retired_legacy_generation_preserves_bytes_with_live_terminal_parent(self):
        store = self.initiative_fixture()
        value = next(value for validator, value in model_tests.OrchestrationModelTests().contract_records() if validator is model.validate_coordinator)
        value['state'] = 'exited'
        path = store.config.initiatives_dir / INITIATIVE_ID / 'coordinators' / (value['coordinator_id'] + '.json')
        raw = (json.dumps(value, indent=3) + '\n').encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        with mock.patch('lib.control.initiative_migration.process_live', return_value=True):
            stage_registries(self.config, self.target)
        self.assertEqual(path.read_bytes(), raw)
        staged = SQLiteInitiativeStore(from_control(self.staged_config()))
        self.assertEqual(staged.read_coordinator(INITIATIVE_ID, value['coordinator_id']), value)
        with ControlDatabase(self.staged_config()) as db, db.transaction() as c:
            retained = staged._registry(INITIATIVE_ID, 'coordinators').read(c, value['coordinator_id'] + '.json')
            self.assertEqual(retained['raw'], raw)

    def test_migration_refuses_only_authorized_live_legacy_generations(self):
        from lib.control.initiative_migration import InitiativeImport
        from lib.control.registry_activation import _external_records
        value = next(value for validator, value in model_tests.OrchestrationModelTests().contract_records() if validator is model.validate_coordinator)
        parts = (INITIATIVE_ID, 'coordinators', value['coordinator_id'] + '.json')
        with mock.patch('lib.control.initiative_migration.process_live', return_value=True), \
             mock.patch('lib.control.registry_activation.process_live', return_value=True):
            for state in model.COORDINATOR_STATES:
                if state == 'absent':
                    continue
                with self.subTest(state=state):
                    raw = json.dumps({**value, 'state': state}).encode()
                    if state in model.COORDINATOR_LIVE_STATES:
                        with self.assertRaisesRegex(StoreError, 'stop live initiative coordinators'):
                            InitiativeImport._validate(parts, raw)
                        with self.assertRaisesRegex(StoreError, 'stop live initiative coordinators'):
                            _external_records([{'domain': 'initiative.coordinators', 'value': {**value, 'state': state}}], None)
                    else:
                        InitiativeImport._validate(parts, raw)
                        _external_records([{'domain': 'initiative.coordinators', 'value': {**value, 'state': state}}], None)
                    managed = {**value, 'state': state, 'anchor': {
                        'kind': 'managed-session-v1', 'session_id': self.task['task_id'],
                        'state_dir': str(self.config.tasks_dir.parent), 'owner_pid': os.getpid(),
                        'process_start_identity': 'live-owner-fixture', 'generation': 1,
                    }}
                    with self.assertRaisesRegex(StoreError, 'stop live initiative coordinators'):
                        InitiativeImport._validate(parts, json.dumps(managed).encode())
                    with self.assertRaisesRegex(StoreError, 'stop live initiative coordinators'):
                        _external_records([{'domain': 'initiative.coordinators', 'value': managed}], None)

    def test_initiative_artifact_symlink_is_never_copied(self):
        store = self.initiative_fixture()
        path = store.config.initiatives_dir / INITIATIVE_ID / "outputs" / (self.task["task_id"] + ".bin")
        path.unlink()
        path.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(StoreError, "symlink"):
            stage_registries(self.config, self.target)

    def test_missing_or_foreign_initiative_heads_refuse_import(self):
        store = self.initiative_fixture()
        path = store.config.initiatives_dir / INITIATIVE_ID / "initiative.json"
        for kind in ("foreign", "missing"):
            with self.subTest(kind=kind):
                if kind == "foreign":
                    value = store.peek(INITIATIVE_ID)
                    value["initiative_id"] = "22222222-2222-4222-8222-222222222222"
                    path.write_text(json.dumps(value))
                else:
                    path.unlink()
                with self.assertRaises(StoreError):
                    stage_registries(self.config, self.root / (kind + "-head-stage"))

    def test_large_stage_ledger_is_paged_and_integrity_checked(self):
        entries = [{"domain": "initiative.events", "scope": INITIATIVE_ID,
            "key": f"{i:06d}-11111111-1111-4111-8111-111111111111.json", "digest": "a" * 64,
            "bytes": 400, "path": INITIATIVE_ID + f"/events/{i:06d}-11111111-1111-4111-8111-111111111111.json"}
            for i in range(1, 5001)]
        self.assertGreater(len(json.dumps(entries).encode()), 1024 * 1024)
        with ControlDatabase(self.config) as db:
            with db.transaction(write=True) as c:
                summary = write_ledger(c, "records", entries)
            self.assertLess(len(json.dumps(summary)), 256)
            with db.transaction() as c:
                self.assertEqual(list(iter_ledger(c, "records", summary)), entries)
            with db.transaction(write=True) as c:
                c.execute("DELETE FROM records WHERE domain='registry-migration-ledger' AND record_key='000000000002'")
            with db.transaction() as c, self.assertRaisesRegex(StoreError, "pages are missing"):
                list(iter_ledger(c, "records", summary))
