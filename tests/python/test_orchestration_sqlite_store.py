from pathlib import Path
import json
import os
import tempfile
import unittest
import uuid
from unittest import mock

from lib.control.database import ControlDatabase, DatabaseError
from lib.control.record_registry import RecordRegistry
from lib.control.orchestration.config import load_config
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
from lib.control.orchestration.store import InitiativeStore, ObservationOnlyPlanError, PresentationBudget, StoreError
from lib.control.orchestration.preview import _Snapshot
from tests.python.test_orchestration_model import (HISTORICAL_PLAN_FIXTURE,
    HISTORICAL_PLAN_DIGEST, INITIATIVE_ID, initiative, node, plan)
from tests.python import test_orchestration_store as legacy_tests
from tests.python.test_orchestration_store import event


class SQLiteInitiativeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = load_config({"HOME": str(root), "ASHA_HOME": str(root / "asha"),
                                   "ASHA_CONFIG": str(root / "missing.json")})
        with ControlDatabase(self.config.control, create=True):
            pass
        self.store = SQLiteInitiativeStore(self.config)
        self.head = initiative()
        self.store.save_initiative(self.head)

    def test_initiative_and_node_updates_keep_existing_authority_rules(self):
        self.assertEqual(self.store.read_initiative(INITIATIVE_ID), self.head)
        self.assertFalse(self.config.initiatives_dir.exists())
        updated = {**self.head, "state_revision": 1}
        self.store.save_initiative(updated, expected_digest=record_digest(self.head))
        with self.assertRaises(StoreError):
            self.store.save_initiative(updated, expected_digest=record_digest(self.head))
        value = node()
        self.store.save_node(INITIATIVE_ID, value)
        self.assertEqual(self.store.read_node(INITIATIVE_ID, value["node_id"]), value)
        invalid = {**value, "node_id": "foreign-node"}
        with self.assertRaises(StoreError):
            self.store.save_node(INITIATIVE_ID, invalid, expected_digest=record_digest(value))
        self.assertEqual(self.store.list_nodes_snapshot(INITIATIVE_ID), [value])

    def seed_retained_heads(self):
        from lib.control.orchestration.model import validate_initiative
        pending = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            registry = RecordRegistry("initiatives")
            for index in range(128):
                iid = f"00000000-0000-4000-8000-{index:012x}"
                value = validate_initiative({**self.head, "initiative_id": iid, "state": "archived"})
                registry.put(c, iid, json.dumps(value).encode(), state="archived", updated_at=value["updated_at"])
            value = validate_initiative({**self.head, "initiative_id": pending, "state": "needs-input"})
            registry.put(c, pending, json.dumps(value).encode(), state="needs-input", updated_at=value["updated_at"])
        return pending

    def test_current_head_is_selected_before_archived_history_uses_the_cap(self):
        from lib.control.store import SnapshotBudget
        import time
        pending = self.seed_retained_heads()
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=1)
        heads = self.store.bounded_activity_snapshots(budget)
        self.assertEqual([head["initiative_id"] for head in heads], [pending])
        self.assertEqual(budget.scanned, 1)
        self.assertTrue(budget.truncated)
        presentation = PresentationBudget(head_limit=1)
        heads = self.store.bounded_head_snapshots(presentation)
        self.assertEqual([head["initiative_id"] for head in heads], [pending])
        self.assertTrue(presentation.truncated)

    def test_activity_head_lookup_uses_state_index_and_retains_all_view_history(self):
        from lib.control.store import SnapshotBudget
        import time
        self.seed_retained_heads()
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=256)
        with mock.patch.object(RecordRegistry, "keys", side_effect=AssertionError("full registry enumeration")):
            heads = self.store.bounded_activity_snapshots(budget)
        self.assertEqual(len(heads), 130)
        self.assertEqual(heads[0]["state"], "needs-input")
        self.assertEqual(sum(h["state"] == "archived" for h in heads), 128)
        self.assertTrue(budget.summary()["complete"])
        with ControlDatabase(self.config.control) as db, db.transaction() as c:
            plan = " ".join(row[3] for row in c.execute(
                "EXPLAIN QUERY PLAN SELECT record_key FROM records WHERE domain=? AND state=? AND scope=? ORDER BY updated_at DESC LIMIT ?",
                ("initiatives", "needs-input", "registry", 1)))
        self.assertTrue("records_state" in plan or "records_scope_state" in plan, plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_running_work_survives_the_cap_beside_an_input_request(self):
        from lib.control.store import SnapshotBudget
        from lib.control.orchestration.model import validate_initiative
        import time
        self.seed_retained_heads()
        iid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        value = validate_initiative({**self.head, "initiative_id": iid, "state": "running"})
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            RecordRegistry("initiatives").put(c, iid, json.dumps(value).encode(),
                                             state="running", updated_at=value["updated_at"])
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=2)
        heads = self.store.bounded_activity_snapshots(budget)
        self.assertEqual([h["state"] for h in heads], ["needs-input", "running"])
        self.assertFalse(budget.summary()["complete"])

    def test_activity_head_unknown_index_state_is_unavailable(self):
        from lib.control.store import SnapshotBudget
        import time
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET state='future-state' WHERE domain='initiatives'")
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=8)
        self.assertEqual(self.store.bounded_activity_snapshots(budget), [])
        self.assertGreater(budget.unavailable, 0)
        self.assertFalse(budget.summary()["complete"])

    def test_activity_head_payload_and_index_disagreement_is_unavailable(self):
        from lib.control.store import SnapshotBudget
        import time
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET state='needs-input' WHERE domain='initiatives'")
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=8)
        self.assertEqual(self.store.bounded_activity_snapshots(budget), [])
        self.assertEqual(budget.unavailable, 1)

    def test_expired_activity_budget_does_not_open_the_database(self):
        from lib.control.store import SnapshotBudget
        import time
        budget = SnapshotBudget(deadline=time.monotonic() - 1, limit=8)
        with mock.patch('lib.control.orchestration.sqlite_views.ControlDatabase', side_effect=AssertionError('expired read')):
            self.assertEqual(self.store.bounded_activity_snapshots(budget), [])
        self.assertTrue(budget.truncated)

    def test_activity_deadline_expiring_during_read_keeps_partial_evidence(self):
        from lib.control.store import SnapshotBudget
        import time
        pending = self.seed_retained_heads()
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=256)
        original = self.store._record
        def expire_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            budget.deadline = 0
            return result
        with mock.patch.object(self.store, '_record', side_effect=expire_after_read):
            heads = self.store.bounded_activity_snapshots(budget)
        self.assertEqual([h['initiative_id'] for h in heads], [pending])
        self.assertFalse(budget.summary()['complete'])
        self.assertTrue(budget.truncated)

    def test_activity_lifecycle_drift_degrades_the_observation(self):
        from lib.control.orchestration import sqlite_views
        from lib.control.store import SnapshotBudget
        import time
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=8)
        with mock.patch.object(sqlite_views, 'ACTIVITY_HEAD_STATES', ('needs-input',)):
            self.assertEqual(self.store.bounded_activity_snapshots(budget), [])
        self.assertEqual(budget.unavailable, 1)
        self.assertFalse(budget.summary()['complete'])

    def seed_attention_history(self):
        from tests.python import test_orchestration_model as fixtures
        from lib.control.orchestration import model
        records = dict(fixtures.OrchestrationModelTests().contract_records())
        ids = {}
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            for directory, validator, field, history, pending in (
                ("approvals", model.validate_approval, "request_id", "consumed", "requested"),
                ("actions", model.validate_action, "action_id", "completed", "indeterminate"),
            ):
                registry = RecordRegistry("initiative." + directory, scope=INITIATIVE_ID)
                base = records[validator]
                for index in range(129):
                    identity = ("ffffffff-ffff-4fff-8fff-ffffffffffff" if index == 128 else
                                f"00000000-0000-4000-8000-{index:012x}")
                    value = {**base, field: identity, "state": pending if index == 128 else history}
                    if directory == "approvals":
                        value["expires_at"] = "2099-01-01T00:00:00Z"
                    else:
                        value["outcome"] = "Retained outcome"
                    value = validator(value)
                    registry.put(c, identity + ".json", json.dumps(value).encode(),
                                 state=value["state"], updated_at=value["updated_at"])
                ids[directory] = identity
            for index in range(32):
                value = model.validate_node({**node(), "node_id": "history-" + str(index)})
                RecordRegistry("initiative.nodes", scope=INITIATIVE_ID).put(
                    c, value["node_id"] + ".json", json.dumps(value).encode(), state=value["state"])
        return ids

    def test_pending_approvals_and_uncertain_actions_precede_history_under_caps(self):
        from lib.control.orchestration import model
        from lib.control.store import SnapshotBudget
        import time
        ids = self.seed_attention_history()
        for directory, validator, field in (("approvals", model.validate_approval, "request_id"),
                                             ("actions", model.validate_action, "action_id")):
            with self.subTest(directory=directory):
                budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=1)
                values = self.store.bounded_records(INITIATIVE_ID, directory, validator, field, budget)
                self.assertEqual([v[field] for v in values], [ids[directory]])
                self.assertFalse(budget.summary()["complete"])

    def test_control_attention_reads_survive_a_large_graph_and_remain_bounded(self):
        from lib.control.tui import _bounded_head_view
        from datetime import datetime, timezone
        ids = self.seed_attention_history()
        for head_limit, nested_limit in ((8, 8), (64, 8), (8, 64), (16, 64)):
            with self.subTest(head_limit=head_limit, nested_limit=nested_limit):
                budget = PresentationBudget(per_head_limit=head_limit, nested_limit=nested_limit)
                view = _bounded_head_view(self.store, self.head, budget, adapter=mock.Mock(), observed=datetime.now(timezone.utc))
                self.assertIn(ids["approvals"], [r["request_id"] for r in view["approvals"]])
                self.assertIn(ids["actions"], [r["action_id"] for r in view["actions"]])
                self.assertLessEqual(budget.nested_scanned, min(head_limit, nested_limit))
                self.assertTrue(view["nodes"], "attention history must also leave room for the graph")
                self.assertFalse(budget.summary()["complete"])
                self.assertIn("record-class", budget.summary()["caps_reached"])

    def test_negative_outcomes_precede_successful_attention_history(self):
        from lib.control.orchestration import model
        from lib.control.store import SnapshotBudget
        import time
        ids = self.seed_attention_history()
        for directory, field, validator, state in (
            ("approvals", "request_id", model.validate_approval, "revoked-before-use"),
            ("actions", "action_id", model.validate_action, "refused"),
        ):
            with self.subTest(directory=directory):
                with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
                    registry = RecordRegistry("initiative." + directory, scope=INITIATIVE_ID)
                    key = ids[directory] + ".json"
                    old = registry.read(c, key)
                    value = validator({**old["value"], "state": state})
                    registry.put(c, key, json.dumps(value).encode(), expected_digest=old["digest"],
                                 state=state, updated_at=value["updated_at"])
                budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=1)
                rows = self.store.bounded_records(INITIATIVE_ID, directory, validator, field, budget)
                self.assertEqual([r[field] for r in rows], [ids[directory]])

    def test_one_capped_head_does_not_mark_another_heads_graph_incomplete(self):
        from lib.control.tui import _bounded_head_view
        from datetime import datetime, timezone
        self.seed_attention_history()
        other = {**initiative(), "initiative_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"}
        self.store.save_initiative(other)
        budget = PresentationBudget(per_head_limit=512, nested_limit=2048)
        first = _bounded_head_view(self.store, self.head, budget, adapter=mock.Mock(), observed=datetime.now(timezone.utc))
        second = _bounded_head_view(self.store, other, budget, adapter=mock.Mock(), observed=datetime.now(timezone.utc))
        self.assertFalse(first["_graph_complete"])
        self.assertTrue(second["_graph_complete"])
        self.assertFalse(budget.summary()["complete"], "the entire observation still contains a partial head")

    def test_attention_timestamp_projection_disagreement_reports_unavailable(self):
        from lib.control.orchestration import model
        from lib.control.store import SnapshotBudget
        import time
        self.seed_attention_history()
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET updated_at='' WHERE domain='initiative.approvals' AND state='requested'")
        budget = SnapshotBudget(deadline=time.monotonic() + 5, limit=1)
        self.assertEqual(self.store.bounded_records(INITIATIVE_ID, "approvals", model.validate_approval, "request_id", budget), [])
        self.assertEqual(budget.unavailable, 1)
        self.assertFalse(budget.summary()["complete"])

    def test_scoped_action_queries_seek_scope_and_state_without_history_sorting(self):
        with ControlDatabase(self.config.control) as db, db.transaction() as c:
            for domain in ("initiative.approvals", "initiative.actions"):
                query = "SELECT record_key FROM records WHERE domain=? AND scope=? AND state=? ORDER BY updated_at DESC LIMIT ?"
                plan = " ".join(row[3] for row in c.execute("EXPLAIN QUERY PLAN " + query,
                                   (domain, INITIATIVE_ID, "requested", 1)))
                self.assertIn("records_scope_state", plan)
                self.assertIn("scope=? AND state=?", plan)
                self.assertNotIn("TEMP B-TREE", plan)

    def test_plan_revision_and_digest_contract_is_unchanged(self):
        first = plan()
        self.store.save_plan(INITIATIVE_ID, first)
        loaded = self.store.read_plan(INITIATIVE_ID, 1)
        self.assertIsNotNone(loaded["digest"])
        self.assertEqual(self.store.list_plans_snapshot(INITIATIVE_ID), [loaded])
        with self.assertRaisesRegex(StoreError, "revision"):
            self.store.save_plan(INITIATIVE_ID, first)
        with self.assertRaisesRegex(StoreError, "revision"):
            self.store.save_plan(INITIATIVE_ID, {**first, "revision": 3})

    def test_event_and_head_publish_in_one_transaction(self):
        value = event(1, "11111111-2222-4333-8444-555555555555", self.head["updated_at"])
        original = RecordRegistry.put
        def fail_head(registry, *args, **kwargs):
            if registry.domain == "initiatives":
                raise StoreError("interrupted before head publication")
            return original(registry, *args, **kwargs)
        with mock.patch.object(RecordRegistry, "put", fail_head), self.assertRaisesRegex(StoreError, "interrupted"):
            self.store.append_event(INITIATIVE_ID, value)
        self.assertEqual(self.store.list_events(INITIATIVE_ID), [])
        self.assertEqual(self.store.peek(INITIATIVE_ID)["last_event_sequence"], 0)
        self.store.append_event(INITIATIVE_ID, value)
        self.assertEqual(self.store.verify_events(INITIATIVE_ID), [value])
        self.assertEqual(self.store.peek(INITIATIVE_ID)["last_event_sequence"], 1)
        with self.assertRaises(StoreError):
            self.store.append_event(INITIATIVE_ID, value)

    def test_output_artifacts_remain_files_and_need_a_retained_initiative(self):
        output = "11111111-2222-4333-8444-555555555555"
        path = self.store.save_output(INITIATIVE_ID, output, b"verification output")
        self.assertEqual(path.read_bytes(), b"verification output")
        self.assertEqual(self.store.read_output(INITIATIVE_ID, output), b"verification output")
        self.assertTrue(path.is_relative_to(self.config.control.tasks_dir.parent / "artifacts"))
        self.assertFalse(self.config.initiatives_dir.exists())
        self.assertFalse((self.config.initiatives_dir / INITIATIVE_ID / "initiative.json").exists())
        with self.assertRaises(StoreError):
            self.store.save_output("22222222-2222-4222-8222-222222222222", output, b"foreign")

    def legacy_artifact(self, *, assignment=False):
        legacy = InitiativeStore(self.config)
        legacy.save_initiative(self.head)
        identity = str(uuid.uuid4())
        content = b"retained evidence"
        path = (legacy.write_assignment if assignment else legacy.save_output)(
            INITIATIVE_ID, identity, content,
        )
        return legacy, identity, content, path

    def freeze_legacy_tree(self):
        paths = sorted(self.config.initiatives_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True)
        paths.append(self.config.initiatives_dir)
        modes = [(p, p.stat().st_mode & 0o777) for p in paths]
        def thaw():
            for path, mode in reversed(modes):
                path.chmod(mode)
        self.addCleanup(thaw)
        for path, _ in modes:
            path.chmod(0o500 if path.is_dir() else 0o400)

    def test_retained_output_path_and_bytes_survive_frozen_legacy_tree(self):
        legacy, identity, content, path = self.legacy_artifact()
        before = path.stat()
        self.freeze_legacy_tree()
        self.assertEqual(self.store.output_path(INITIATIVE_ID, identity), path)
        self.assertEqual(self.store.read_output(INITIATIVE_ID, identity), content)
        self.assertEqual(path.stat().st_ino, before.st_ino)
        for operation, args in ((self.store.reserve_output, ()),
                                (self.store.save_output, (b"replacement",)),
                                (self.store.finalize_reserved_output, (b"replacement",))):
            with self.assertRaisesRegex(StoreError, "retained"):
                operation(INITIATIVE_ID, identity, *args)
        with self.assertRaises(StoreError):
            legacy.save_output(INITIATIVE_ID, str(uuid.uuid4()), b"old writer")
        new_id = str(uuid.uuid4())
        new_path = self.store.save_output(INITIATIVE_ID, new_id, b"new output")
        self.assertNotEqual(new_path.parent, path.parent)
        self.assertEqual(self.store.inventory(INITIATIVE_ID)["outputs"]["bytes"], len(content) + len(b"new output"))

    def test_retained_assignment_replay_preserves_path_and_refuses_changed_bytes(self):
        _, identity, content, path = self.legacy_artifact(assignment=True)
        self.freeze_legacy_tree()
        self.assertEqual(self.store.assignment_path(INITIATIVE_ID, identity), path)
        self.assertEqual(self.store.write_assignment(INITIATIVE_ID, identity, content), path)
        with self.assertRaisesRegex(StoreError, "differs"):
            self.store.write_assignment(INITIATIVE_ID, identity, b"changed")

    def test_retained_artifact_lookup_refuses_symlink_hardlink_and_unsafe_mode(self):
        _, identity, content, path = self.legacy_artifact()
        for mode in (0o644, 0o444):
            path.chmod(mode)
            with self.assertRaises(StoreError):
                self.store.output_path(INITIATIVE_ID, identity)
        path.chmod(0o600)
        second = path.with_suffix(".link")
        os.link(path, second)
        with self.assertRaises(StoreError):
            self.store.read_output(INITIATIVE_ID, identity)
        second.unlink()
        path.rename(second)
        path.symlink_to(second)
        with self.assertRaises(StoreError):
            self.store.output_path(INITIATIVE_ID, identity)

    def test_duplicate_artifact_identity_across_roots_is_refused(self):
        _, identity, content, path = self.legacy_artifact()
        new = self.config.control.tasks_dir.parent / "artifacts" / INITIATIVE_ID / "outputs"
        new.mkdir(mode=0o700, parents=True)
        for directory in (new.parent, new.parent.parent):
            directory.chmod(0o700)
        duplicate = new / path.name
        duplicate.write_bytes(content)
        duplicate.chmod(0o600)
        with self.assertRaisesRegex(StoreError, "both"):
            self.store.output_path(INITIATIVE_ID, identity)

    def test_direct_artifact_reader_refuses_symlinked_directory(self):
        _, identity, _, path = self.legacy_artifact()
        outputs = path.parent
        moved = outputs.with_name("saved-outputs")
        outputs.rename(moved)
        outputs.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "cannot open artifact directory outputs"):
            self.store.retained_artifacts.inspect(INITIATIVE_ID, "outputs", identity, read=True)

    def test_locked_recovery_sweeps_only_current_write_residue(self):
        identity = str(uuid.uuid4())
        path = self.store.save_output(INITIATIVE_ID, identity, b"durable output")
        residue = path.with_name("." + path.name + ".tmp.interrupted")
        os.link(path, residue)
        with self.assertRaisesRegex(StoreError, "link count"):
            self.store.inventory(INITIATIVE_ID, locked=False)
        self.assertTrue(residue.exists())
        self.assertEqual(self.store.inventory(INITIATIVE_ID)["outputs"]["bytes"], len(b"durable output"))
        self.assertFalse(residue.exists())
        self.assertEqual(self.store.read_output(INITIATIVE_ID, identity), b"durable output")

        _, retained_id, _, retained_path = self.legacy_artifact()
        retained_residue = retained_path.with_name("." + retained_path.name + ".tmp.retained")
        os.link(retained_path, retained_residue)
        with self.assertRaisesRegex(StoreError, "link count"):
            self.store.inventory(INITIATIVE_ID)
        self.assertTrue(retained_residue.exists())

    def test_assignment_retry_recovers_linked_current_residue(self):
        identity = str(uuid.uuid4())
        path = self.store.write_assignment(INITIATIVE_ID, identity, b"assignment")
        residue = path.with_name("." + path.name + ".tmp.interrupted")
        os.link(path, residue)
        self.assertEqual(self.store.write_assignment(INITIATIVE_ID, identity, b"assignment"), path)
        self.assertFalse(residue.exists())

    def test_bounded_event_view_reports_missing_history(self):
        value = event(1, "11111111-2222-4333-8444-555555555555", self.head["updated_at"])
        self.store.append_event(INITIATIVE_ID, value)
        sample, complete = self.store.bounded_event_sample(INITIATIVE_ID, PresentationBudget())
        self.assertEqual(sample, [value])
        self.assertTrue(complete)
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain=? AND scope=?",
                      ("initiative.events", INITIATIVE_ID))
        budget = PresentationBudget()
        sample, complete = self.store.bounded_event_sample(INITIATIVE_ID, budget)
        self.assertEqual(sample, [])
        self.assertFalse(complete)
        self.assertEqual(budget.unavailable, 1)
        self.assertIn("event count", budget.failures[0]["reason"])

    def test_preview_and_presentation_read_sql_nodes_without_artifact_directories(self):
        value = node()
        self.store.save_node(INITIATIVE_ID, value)
        self.assertEqual(_Snapshot(self.store, INITIATIVE_ID).get("nodes"), [value])
        budget = PresentationBudget()
        self.assertEqual(self.store.bounded_presentation_records(INITIATIVE_ID, "nodes", budget), [value])
        self.assertEqual(budget.unavailable, 0)
        self.assertEqual(budget.nested_scanned, 1)
        self.assertFalse(self.config.initiatives_dir.exists())

    def test_inventory_counts_sql_bytes_and_file_artifacts(self):
        self.store.save_node(INITIATIVE_ID, node())
        before = self.store.inventory(INITIATIVE_ID)
        self.assertEqual(before["totals"]["rows"], 2)
        self.assertGreater(before["totals"]["bytes"], 0)
        self.assertEqual(before["totals"]["inodes"], 0)
        self.store.save_output(INITIATIVE_ID, "11111111-2222-4333-8444-555555555555", b"retained")
        after = self.store.inventory(INITIATIVE_ID)
        self.assertEqual(after["totals"]["rows"], 2)
        self.assertEqual(after["totals"]["bytes"], before["totals"]["bytes"] + len(b"retained"))
        self.assertGreater(after["totals"]["inodes"], 0)

    def test_event_cursor_and_tail_only_decode_selected_payloads(self):
        events = [event(i, str(uuid.uuid4()), self.head["updated_at"]) for i in range(1, 7)]
        for value in events:
            self.store.append_event(INITIATIVE_ID, value)
        original = self.store._record
        decoded = []
        def observe(c, iid, directory, *args, **kwargs):
            if directory == "events":
                decoded.append(args[0])
            return original(c, iid, directory, *args, **kwargs)
        with mock.patch.object(self.store, "_record", side_effect=observe):
            self.assertEqual(self.store.list_events_snapshot(INITIATIVE_ID, after=2, tail=2), events[-2:])
            self.assertEqual(len(decoded), 2)
            decoded.clear()
            self.assertEqual(self.store.list_events_snapshot(INITIATIVE_ID, after=5), events[-1:])
            self.assertEqual(len(decoded), 1)
            decoded.clear()
            self.assertEqual(self.store.list_events_snapshot(INITIATIVE_ID, after=10**30), [])
            self.assertEqual(self.store.list_events_snapshot(INITIATIVE_ID, tail=0), [])
            self.assertEqual(decoded, [])
            self.store.append_event(INITIATIVE_ID, event(7, str(uuid.uuid4()), self.head["updated_at"]))
            self.assertEqual(len(decoded), 1)

    def test_schema_refuses_duplicate_sequences_and_malformed_keys(self):
        value = event(1, str(uuid.uuid4()), self.head["updated_at"])
        self.store.append_event(INITIATIVE_ID, value)
        registry = RecordRegistry("initiative.events", scope=INITIATIVE_ID)
        for key in (f"000001-{uuid.uuid4()}.json", "bad-event-key", f"000000-{uuid.uuid4()}.json"):
            with self.subTest(key=key), self.assertRaises(DatabaseError):
                with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
                    registry.put(c, key, json.dumps(value).encode())
        self.assertEqual(self.store.verify_events(INITIATIVE_ID), [value])

    def test_missing_middle_event_refuses_tail_and_append(self):
        for i in range(1, 4):
            self.store.append_event(INITIATIVE_ID, event(i, str(uuid.uuid4()), self.head["updated_at"]))
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='initiative.events' AND record_key LIKE '000002-%'")
        with self.assertRaisesRegex(StoreError, "sequence disagrees"):
            self.store.list_events_snapshot(INITIATIVE_ID, tail=1)
        with self.assertRaisesRegex(StoreError, "sequence disagrees"):
            self.store.append_event(INITIATIVE_ID, event(4, str(uuid.uuid4()), self.head["updated_at"]))

    def test_retained_historical_plan_stays_observation_only(self):
        raw = HISTORICAL_PLAN_FIXTURE.read_bytes()
        registry = RecordRegistry("initiative.plans", scope=INITIATIVE_ID)
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            registry.put(c, "0001.json", raw)
        self.assertEqual(self.store.read_plan_snapshot(INITIATIVE_ID, 1)["digest"], HISTORICAL_PLAN_DIGEST)
        with self.assertRaises(ObservationOnlyPlanError):
            self.store.read_plan(INITIATIVE_ID, 1)
        with ControlDatabase(self.config.control) as db, db.transaction() as c:
            self.assertEqual(registry.read(c, "0001.json")["raw"], raw)

    def test_sql_evidence_freezes_reserved_output(self):
        from lib.control.orchestration import model
        value = legacy_tests.contract_record(model.validate_evidence)
        output_id = value["evidence_id"]
        self.store.reserve_output(INITIATIVE_ID, output_id)
        self.store.finalize_reserved_output(INITIATIVE_ID, output_id, b"published output")
        self.store.save_evidence(INITIATIVE_ID, value)
        with self.assertRaisesRegex(StoreError, "immutable after evidence"):
            self.store.finalize_reserved_output(INITIATIVE_ID, output_id, b"replacement")
        with self.assertRaisesRegex(StoreError, "after evidence publication"):
            self.store.reserve_output(INITIATIVE_ID, output_id)
        self.assertEqual(self.store.read_output(INITIATIVE_ID, output_id), b"published output")

    def test_public_aliases_and_invalid_identity_use_the_sql_store(self):
        self.assertEqual(self.store.read(INITIATIVE_ID), self.head)
        self.assertEqual(self.store.list(), [self.head])
        updated = {**self.head, "state_revision": 1}
        self.store.save(updated, expected_digest=record_digest(self.head))
        self.assertEqual(self.store.read(INITIATIVE_ID), updated)
        with self.assertRaises(StoreError):
            self.store.read("not-an-initiative")
        self.assertFalse(self.config.initiatives_dir.exists())

    def test_evidence_lookup_uses_explicit_identity_without_resolving_artifact_paths(self):
        from lib.control.orchestration import model
        value = legacy_tests.contract_record(model.validate_evidence)
        self.store.save_evidence(INITIATIVE_ID, value)
        other_id = "22222222-2222-4222-8222-222222222222"
        self.store.save_initiative({**self.head, "initiative_id": other_id})
        # SQLite needs no procfs pathname from this file-descriptor argument.
        self.assertTrue(self.store._output_evidence_exists(INITIATIVE_ID, -1, value["evidence_id"]))
        self.assertFalse(self.store._output_evidence_exists(other_id, -1, value["evidence_id"]))


class SQLiteInitiativeAuthorityTests(unittest.TestCase):
    """Run the existing authority scenarios against the new storage seam."""

    def setUp(self):
        legacy_tests.OrchestrationStoreTests.setUp(self)
        with ControlDatabase(self.config.control, create=True):
            pass
        self.store = SQLiteInitiativeStore(self.config)

    tearDown = legacy_tests.OrchestrationStoreTests.tearDown
    create = legacy_tests.OrchestrationStoreTests.create
    test_digest_guarded_update_and_conflict = legacy_tests.OrchestrationStoreTests.test_digest_guarded_update_and_conflict
    test_create_initiative_requires_exact_draft_baseline = legacy_tests.OrchestrationStoreTests.test_create_initiative_requires_exact_draft_baseline
    test_mutable_node_and_attempt_snapshots = legacy_tests.OrchestrationStoreTests.test_mutable_node_and_attempt_snapshots
    test_mutable_records_enforce_transitions_immutable_fields_and_terminality = legacy_tests.OrchestrationStoreTests.test_mutable_records_enforce_transitions_immutable_fields_and_terminality
    test_review_verification_and_bundle_are_mutable_until_terminal = legacy_tests.OrchestrationStoreTests.test_review_verification_and_bundle_are_mutable_until_terminal
