import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.control.database import ControlDatabase
from lib.control.record_registry import RecordRegistry
from lib.control.orchestration.config import load_config
from lib.control.orchestration import model
from lib.control.orchestration.current_actions import approval_demand, page
from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
from lib.control.orchestration.store import StoreError
from tests.python import test_orchestration_model as fixtures
from tests.python.test_control_registry_backend import install_marker_fixture


class CurrentActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.env = {"HOME": str(root), "ASHA_HOME": str(root / "asha")}
        self.config = load_config(self.env)
        install_marker_fixture(self.config.control)
        self.store = SQLiteInitiativeStore(self.config)

    def head(self, index, state="running"):
        iid = f"{index:08x}-0000-4000-8000-000000000000"
        value = model.validate_initiative({**fixtures.initiative(), "initiative_id": iid,
                   "state": state, "active_plan": {"revision": 1, "digest": "b" * 64,
                                                     "approval_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}})
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            RecordRegistry("initiatives").put(c, iid, json.dumps(value).encode(),
                                             state=state, updated_at=value["updated_at"])
        return value

    def approval(self, head, index=1, **changes):
        rid = f"{index:08x}-1111-4111-8111-111111111111"
        base = dict(fixtures.OrchestrationModelTests().contract_records())[model.validate_approval]
        value = model.validate_approval({**base, "initiative_id": head["initiative_id"], "request_id": rid,
                     "action_class": "salvage", "expires_at": "2099-01-01T00:00:00Z", **changes})
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            RecordRegistry("initiative.approvals", scope=head["initiative_id"]).put(c, rid + ".json",
                json.dumps(value).encode(), state=value["state"], updated_at=value["updated_at"])
        return value

    def test_global_approvals_do_not_depend_on_head_sample_or_history(self):
        for i in range(1, 40):
            self.head(i, "archived")
        target = self.head(100)
        request = self.approval(target)
        with mock.patch.object(self.store, "bounded_head_snapshots", side_effect=AssertionError("head sampling")), \
             mock.patch.object(self.store, "list_events_snapshot", side_effect=AssertionError("history scan")):
            result = page(self.store, family="approvals", limit=1)
        self.assertEqual([r["request_id"] for r in result["rows"]], [request["request_id"]])
        self.assertEqual(result["rows"][0]["waiting_on"], "keeper")
        self.assertTrue(result["complete"])
        self.assertFalse(result["bindings_checked"])

    def test_pages_cover_independent_heads_and_cursor_family_is_bound(self):
        for i in range(1, 5):
            self.approval(self.head(i), index=i)
        first = page(self.store, family="approvals", limit=2)
        second = page(self.store, family="approvals", limit=2, after=first["next"])
        self.assertFalse(first["complete"])
        self.assertTrue(second["complete"])
        self.assertEqual(len({r["request_key"] for r in first["rows"] + second["rows"]}), 4)
        with self.assertRaisesRegex(StoreError, "cursor"):
            page(self.store, family="initiatives", after=first["next"])

    def test_expired_stale_and_inactive_requests_do_not_offer_approval(self):
        running = self.head(1)
        expired = self.approval(running, 1, expires_at="2000-01-01T00:00:00Z",
                                created_at="1999-01-01T00:00:00Z", updated_at="1999-01-01T00:00:00Z")
        stale = self.approval(running, 2, active_plan_digest="c" * 64)
        inactive = self.approval(self.head(2, "archived"), 3)
        for value, head, expected in ((expired, running, "expired"), (stale, running, "stale-plan"),
                                      (inactive, self.store.peek(inactive["initiative_id"]), "inactive")):
            row = approval_demand(value, head)
            self.assertEqual(row["disposition"], expected)
            self.assertNotEqual(row["waiting_on"], "keeper")
            self.assertNotIn("approve-salvage", row["resolution"])

    def test_settled_approvals_disappear_without_a_refresh_write(self):
        head = self.head(1)
        value = self.approval(head)
        self.assertEqual(len(page(self.store, family="approvals")["rows"]), 1)
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            registry = RecordRegistry("initiative.approvals", scope=head["initiative_id"])
            key = value["request_id"] + ".json"
            old = registry.read(c, key)
            changed = model.validate_approval({**value, "state": "approved"})
            registry.put(c, key, json.dumps(changed).encode(), expected_digest=old["digest"],
                         state="approved", updated_at=changed["updated_at"])
        self.assertEqual(page(self.store, family="approvals")["rows"], [])

    def test_head_requests_are_distinct_from_terminal_history(self):
        for index, state in enumerate(("archived", "running", "approved", "ready-for-integration", "needs-input", "awaiting-plan-approval"), 1):
            self.head(index, state)
        result = page(self.store, limit=10)
        self.assertEqual([r["kind"] for r in result["rows"]],
                         ["operator-decision", "plan-approval", "integration", "activation"])
        self.assertTrue(result["complete"])

    def test_corrupt_selected_record_is_unavailable_not_an_empty_success(self):
        head = self.head(1, "needs-input")
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("UPDATE records SET digest='bad' WHERE domain='initiatives'")
        result = page(self.store)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["unavailable_records"], 1)
        self.assertFalse(result["complete"])

    def test_deadline_and_invalid_cursor_do_not_manufacture_complete_empty(self):
        self.head(1, "needs-input")
        result = page(self.store, deadline=time.monotonic() - 1)
        self.assertFalse(result["complete"])
        self.assertTrue(result["deadline_exceeded"])
        with self.assertRaisesRegex(StoreError, "cursor"):
            page(self.store, after='{"family":"approvals"}')

    def test_unknown_approval_type_does_not_become_salvage(self):
        head = self.head(1)
        value = self.approval(head, action_class="task-start")
        row = approval_demand(value, head)
        self.assertEqual(row["disposition"], "unsupported")
        self.assertEqual(row["kind"], "approval-inspection")
        self.assertNotIn("approve-salvage", row["resolution"])

    def test_cli_page_routes_to_same_snapshot_and_requires_explicit_page(self):
        from lib.control.orchestration import cli
        from contextlib import redirect_stdout, redirect_stderr
        import io
        self.approval(self.head(1))
        output = io.StringIO()
        with mock.patch.object(cli, "InitiativeStore", return_value=self.store), redirect_stdout(output):
            code = cli.main(["initiative", "attention", "--page", "approvals", "--limit", "1", "--json"], env=self.env)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["rows"][0]["request_key"],
                         page(self.store, family="approvals")["rows"][0]["request_key"])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["initiative", "attention", "--limit", "1", "--json"], env=self.env), 2)
            with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
                c.execute("DELETE FROM records WHERE domain='registry-backend'")
            self.assertEqual(cli.main(["initiative", "attention", "--page", "approvals", "--json"], env=self.env), 2,
                             "a file-backed active root must not read an unactivated SQL registry")

    def test_backend_selection_is_rechecked_in_the_page_snapshot(self):
        self.head(1, "needs-input")
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
        with self.assertRaisesRegex(StoreError, "active SQLite"):
            page(self.store)

    def test_doctor_accepts_frozen_legacy_tree_only_when_sqlite_is_authoritative(self):
        from lib.control.orchestration.doctor import _storage_probe
        self.config.initiatives_dir.mkdir(mode=0o500)
        self.addCleanup(self.config.initiatives_dir.chmod, 0o700)
        self.assertEqual(_storage_probe(self.config).outcome, "match")
        with ControlDatabase(self.config.control) as db, db.transaction(write=True) as c:
            c.execute("DELETE FROM records WHERE domain='registry-backend'")
        self.assertEqual(_storage_probe(self.config).outcome, "mismatch")

    def test_timeout_preserves_position_and_explicit_retry(self):
        for index in (1, 2):
            self.head(index, "needs-input")
        first = page(self.store, limit=1)
        stalled = page(self.store, limit=1, after=first["next"], deadline=time.monotonic() - 1)
        self.assertEqual(stalled["next"], first["next"])
        self.assertTrue(stalled["retry"])
        resumed = page(self.store, limit=1, after=stalled["next"])
        self.assertTrue(resumed["complete"])
        self.assertNotEqual(resumed["rows"][0]["request_key"], first["rows"][0]["request_key"])

    def test_lifecycle_overlap_refuses_and_timestamp_overflow_is_unavailable(self):
        self.head(1, "needs-input")
        with mock.patch("lib.control.orchestration.current_actions.QUIET_HEAD_STATES", set(model.INITIATIVE_STATES)):
            with self.assertRaisesRegex(StoreError, "lifecycle"):
                page(self.store)
        with mock.patch("lib.control.orchestration.current_actions._epoch", side_effect=OverflowError):
            result = page(self.store)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unavailable_records"], 1)

    def test_admission_is_reported_beside_inspection_candidates(self):
        self.head(1, "needs-input")
        self.assertIn("mode", page(self.store)["admission"])

    def test_shared_tree_classifier_does_not_revive_expired_or_stale_approvals(self):
        from lib.control.orchestration.tui_model import initiative_demand
        head = self.head(1)
        expired = self.approval(head, expires_at="2000-01-01T00:00:00Z",
                                 created_at="1999-01-01T00:00:00Z", updated_at="1999-01-01T00:00:00Z")
        stale = self.approval(head, 2, active_plan_digest="c" * 64)
        live = self.approval(head, 3)
        view = {"initiative": head, "nodes": [], "attempts": [], "events": [], "approvals": [expired, stale, live]}
        demands = [row for row in initiative_demand(view) if row["kind"] == "salvage-approval"]
        self.assertEqual([row["request_id"] for row in demands], [live["request_id"]])
        self.assertEqual(demands[0], approval_demand(live, head))

    def test_state_index_query_plan_skips_terminal_history_without_a_sort(self):
        with ControlDatabase(self.config.control) as db, db.transaction() as c:
            plan = " ".join(row[3] for row in c.execute(
                "EXPLAIN QUERY PLAN SELECT state,updated_at,scope,record_key FROM records WHERE domain=? AND state=? ORDER BY updated_at,scope,record_key LIMIT ?",
                ("initiative.approvals", "requested", 51)))
        self.assertIn("records_state", plan)
        self.assertNotIn("TEMP B-TREE", plan)
