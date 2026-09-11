import unittest
import uuid
import time
from unittest import mock

from lib.control.store import StoreError
from tests.python import test_control_managed_sessions as fixtures


class SessionActivityTests(unittest.TestCase):
    def setUp(self):
        fixtures.SessionTests.setUp(self)

    def test_stopped_history_cannot_hide_current_work_or_read_output(self):
        for _ in range(12):
            sid = self.store.create(cwd=self.tmp.name, prompt="history")["session_id"]
            self.store.stop(sid)
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET created_at=0 WHERE state='stopped'")
        statements = []
        self.store.db._live().set_trace_callback(statements.append)
        page = self.store.current_work(limit=1)
        self.assertEqual([r["session_id"] for r in page["rows"]], [self.sid])
        self.assertTrue(page["complete"])
        self.assertFalse(any("session_events" in sql or "session-output" in sql for sql in statements))

    def test_keyset_pages_are_disjoint_and_explicitly_live(self):
        for _ in range(4):
            self.store.create(cwd=self.tmp.name, prompt="new")
        seen, after = [], None
        for _ in range(5):
            value = self.store.current_work(limit=2, after=after)
            seen.extend(r["session_id"] for r in value["rows"])
            after = value["next_cursor"]
            if value["complete"]:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)
        self.assertIn("mutable", value["snapshot"])
        with self.assertRaises(StoreError):
            self.store.current_work(kind="requests", after=after)

    def test_question_is_independent_of_session_page_and_keeps_exact_identity(self):
        turn = self.store.claim_turn(self.sid, self.generation)
        request = self.store.request(self.sid, turn["turn_id"], "Which chapter?", request_id=str(uuid.uuid4()))
        value = self.store.current_work(kind="requests", limit=1)
        row = value["rows"][0]
        self.assertEqual(row["request_id"], request["request_id"])
        self.assertEqual(row["digest"], request["digest"])
        self.assertEqual(row["waiting_on"], "keeper")
        self.assertEqual(row["next_action"], "answer")
        self.store.answer(row["request_id"], "Two", expected_digest=row["digest"])
        self.assertEqual(self.store.current_work(kind="requests")["rows"], [])

    def test_read_does_not_change_custody_or_authorize_dispatch(self):
        from lib.control.runtime import set_admission
        set_admission(self.config, "paused")
        before = self.store.snapshot(self.sid)
        value = self.store.current_work(kind="deliveries")
        row = value["rows"][0]
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["waiting_on"], "operator")
        self.assertEqual(row["next_action"], "inspect-runtime")
        self.assertEqual(before, self.store.snapshot(self.sid))

    def test_native_permission_wait_is_visible_on_session_and_queued_delivery(self):
        from lib.control.native_requests import NativeRequests
        from lib.control.session_activity import summary
        turn = self.store.claim_turn(self.sid, self.generation)
        requests = NativeRequests(self.store)
        self.store.observe(self.sid, self.generation, turn['turn_id'], 'initialized', {'native_id': 'native-test'})
        request = requests.open(self.sid, self.generation, turn['turn_id'], 'provider-wait', {
            'subtype': 'can_use_tool', 'tool_name': 'Bash',
            'input': {'command': 'git status'}, 'tool_use_id': 'tool-wait',
        })
        self.store.enqueue(self.sid, 'Follow up', key='permission-follow-up')
        before = self.store.snapshot(self.sid)
        for kind in ('sessions', 'deliveries'):
            row = self.store.current_work(kind=kind)['rows'][0]
            self.assertEqual(row['waiting_on'], 'keeper')
            self.assertEqual(row['next_action'], 'inspect-requests')
        self.assertEqual(summary(self.store)['pages']['sessions']['rows'][0]['waiting_on'], 'keeper')
        self.assertEqual(self.store.snapshot(self.sid), before)
        requests.decide(request['request_id'], 'allow', expected_digest=request['digest'])
        self.assertEqual(self.store.current_work()['rows'][0]['waiting_on'], 'agent')

    def test_pending_native_request_does_not_hide_recovery_or_stop(self):
        from lib.control.native_requests import NativeRequests
        turn = self.store.claim_turn(self.sid, self.generation)
        self.store.observe(self.sid, self.generation, turn['turn_id'], 'initialized', {'native_id': 'native-test'})
        NativeRequests(self.store).open(self.sid, self.generation, turn['turn_id'], 'provider-wait', {
            'subtype': 'can_use_tool', 'tool_name': 'Bash',
            'input': {'command': 'git status'}, 'tool_use_id': 'tool-wait',
        })
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET state='uncertain' WHERE session_id=?", (self.sid,))
        self.assertEqual(self.store.current_work()['rows'][0]['next_action'], 'inspect-recovery')
        with self.store.db.transaction(write=True) as c:
            c.execute('UPDATE managed_sessions SET stop_requested=1 WHERE session_id=?', (self.sid,))
        self.assertEqual(self.store.current_work()['rows'][0]['next_action'], 'inspect-session')

    def test_invalid_cursors_and_limits_fail_cleanly(self):
        for cursor in ("bad", "[]", '{"kind":"sessions","position":[true,0,"x"]}'):
            with self.assertRaises(StoreError):
                self.store.current_work(after=cursor)
        for limit in (True, 0, 1001):
            with self.assertRaises(StoreError):
                self.store.current_work(limit=limit)

    def test_summary_reports_lower_bounds_when_capped_and_matches_overview(self):
        from lib.control.session_activity import summary
        from lib.control.sessions import overview
        self.store.create(cwd=self.tmp.name, prompt="second")
        bounded = summary(self.store, limit=1)
        self.assertFalse(bounded["complete"])
        self.assertEqual(bounded["count_kind"], "lower-bound")
        self.assertIn("at least", bounded["summary"])
        self.assertEqual(summary(self.store)["summary"], overview(self.config)["summary"])

    def test_summary_never_needs_history_projection(self):
        from lib.control.sessions import overview
        with mock.patch.object(type(self.store), "snapshot", side_effect=AssertionError("history read")):
            self.assertEqual(overview(self.config)["queued"], 1)

    def test_cli_current_and_chair_render_use_current_projection(self):
        import io
        import json
        from contextlib import redirect_stdout
        from lib.control.sessions import main
        from lib.control.orchestration.config import load_config
        from lib.control.orchestration.observation import current_activity, render_startup_observation
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["current", "--json"], env=self.env), 0)
        self.assertEqual(json.loads(output.getvalue())["rows"][0]["session_id"], self.sid)
        with mock.patch("lib.control.orchestration.observation.BoundedTmux.inventory", side_effect=OSError("offline")):
            activity = current_activity(load_config(self.env))
        self.assertEqual(activity["sources"]["managed-sessions"]["observed_count"], 1)
        self.assertTrue(activity["sources"]["managed-sessions"]["complete"])
        rendered = render_startup_observation(activity, observed_at="now")
        self.assertIn("Current managed sessions: >= 1", rendered)
        self.assertNotIn("Do the work", rendered)

    def test_current_queries_use_state_indexes(self):
        turn = self.store.claim_turn(self.sid, self.generation)
        self.store.request(self.sid, turn["turn_id"], "fixture", request_id=str(uuid.uuid4()))
        self.store.enqueue(self.sid, "next", key="index-fixture")
        statements = []
        self.store.db._live().set_trace_callback(statements.append)
        for kind in ("sessions", "requests", "deliveries"):
            value = self.store.current_work(kind=kind, limit=1)
            if value["next_cursor"]:
                self.store.current_work(kind=kind, limit=1, after=value["next_cursor"])
        with self.store.db.transaction() as c:
            for sql in statements[:]:
                if sql.startswith("SELECT") and any(clause in sql for clause in (
                        "WHERE state", "WHERE session_id=", "WHERE t.state=")):
                    plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
                    self.assertIn("INDEX", plan)
                    self.assertNotIn("SCAN ", plan)
                    self.assertNotIn("TEMP B-TREE", plan)

    def test_delivery_waits_on_recovery_and_idle_with_input_waits_on_supervisor(self):
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET state='failed' WHERE session_id=?", (self.sid,))
        self.assertEqual(self.store.current_work(kind="deliveries")["rows"][0]["next_action"], "inspect-recovery")
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET state='idle' WHERE session_id=?", (self.sid,))
        row = self.store.current_work()["rows"][0]
        self.assertEqual(row["waiting_on"], "supervisor")

    def test_capacity_wait_is_visible_and_clears_without_spending_a_turn(self):
        occupied = []
        for _ in range(2):
            sid = self.store.create(cwd=self.tmp.name, prompt="Busy")['session_id']
            generation = self.store.claim_owner(sid)['generation']
            occupied.append((sid, generation, self.store.claim_turn(sid, generation)))
        before = self.store.snapshot(self.sid)
        self.assertIsNone(self.store.claim_turn(self.sid, self.generation))
        for kind in ('sessions', 'deliveries'):
            row = next(r for r in self.store.current_work(kind=kind)['rows']
                       if r['session_id'] == self.sid)
            self.assertEqual(row['waiting_on'], 'capacity')
            self.assertIn('2 managed turns', row['reason'])
        self.assertEqual(self.store.snapshot(self.sid), before)
        from lib.control.session_activity import summary
        self.assertIn('1 waiting for managed turn capacity (limit 2)', summary(self.store)['summary'])
        busy_sid = occupied[0][0]
        self.store.enqueue(busy_sid, 'Follow up', key='follow-up')
        row = next(r for r in self.store.current_work(kind='deliveries')['rows']
                   if r['session_id'] == busy_sid)
        self.assertEqual(row['waiting_on'], 'agent')
        from lib.control.runtime import set_admission
        set_admission(self.config, 'paused')
        row = next(r for r in self.store.current_work()['rows'] if r['session_id'] == self.sid)
        self.assertEqual(row['next_action'], 'inspect-runtime')
        set_admission(self.config, 'running')
        sid, generation, turn = occupied[0]
        self.store.finish(sid, generation, turn['turn_id'], success=True)
        row = next(r for r in self.store.current_work()['rows'] if r['session_id'] == self.sid)
        self.assertEqual(row['waiting_on'], 'supervisor')
        self.assertIsNotNone(self.store.claim_turn(self.sid, self.generation))

    def test_request_and_delivery_pages_resume_after_reopen(self):
        from lib.control.session_store import SessionStore
        turn = self.store.claim_turn(self.sid, self.generation)
        for index in range(4):
            self.store.request(self.sid, turn["turn_id"], f"Question {index}", request_id=str(uuid.uuid4()))
            self.store.enqueue(self.sid, f"Message {index}", key=f"fixture-{index}")
        for kind, key, expected in (("requests", "request_id", 4), ("deliveries", "message_id", 4)):
            with self.subTest(kind=kind):
                first = self.store.current_work(kind=kind, limit=2)
                seen = [r[key] for r in first["rows"]]
                after = first["next_cursor"]
                with SessionStore(self.config) as reopened:
                    while True:
                        value = reopened.current_work(kind=kind, limit=2, after=after)
                        seen.extend(r[key] for r in value["rows"])
                        after = value["next_cursor"]
                        if value["complete"]:
                            break
                self.assertEqual(len(seen), expected)
                self.assertEqual(len(set(seen)), expected)

    def test_operational_priority_does_not_drop_later_states_from_pages(self):
        expected = {self.sid}
        for state in ("waiting-input", "failed", "idle", "running", "uncertain"):
            sid = self.store.create(cwd=self.tmp.name, prompt=state)["session_id"]
            expected.add(sid)
            with self.store.db.transaction(write=True) as c:
                c.execute("UPDATE managed_sessions SET state=? WHERE session_id=?", (state, sid))
        seen, after = [], None
        while True:
            value = self.store.current_work(limit=1, after=after)
            seen.extend(r["session_id"] for r in value["rows"])
            after = value["next_cursor"]
            if value["complete"]:
                break
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(seen), len(expected))
        self.assertEqual(self.store.current_work(limit=1)["rows"][0]["state"], "waiting-input")

    def test_queued_delivery_precedes_consumed_and_export_columns_are_explicit(self):
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE session_messages SET state='consumed' WHERE session_id=?", (self.sid,))
        queued = self.store.enqueue(self.sid, "next", key="next")
        first = self.store.current_work(kind="deliveries", limit=1)
        self.assertEqual(first["rows"][0]["message_id"], queued["message_id"])
        self.assertTrue(first["complete"])
        row = self.store.current_work()["rows"][0]
        self.assertEqual(set(row), {"session_id", "initiative_id", "harness", "cwd", "state", "generation",
            "stop_requested", "turns", "max_turns", "created_at", "updated_at", "session_state",
            "has_queued_input", "active_turn", "waiting_on", "next_action", "reason", "age_seconds"})

    def test_expired_observation_deadline_reports_unknown_counts(self):
        from lib.control.session_activity import summary
        value = summary(self.store, deadline=time.monotonic() - 1)
        self.assertFalse(value["complete"])
        self.assertEqual(value["count_kind"], "lower-bound")
        for page in value["pages"].values():
            self.assertTrue(page["deadline_exceeded"])
            self.assertEqual(page["rows"], [])

    def test_finished_turn_receipt_is_history_and_active_receipt_stays_visible(self):
        turn = self.store.claim_turn(self.sid, self.generation)
        current = self.store.current_work()["rows"][0]
        self.assertEqual(current["active_turn"]["message_id"], turn["message_id"])
        self.assertEqual(current["active_turn"]["delivery_state"], "submitted")
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.assertEqual(self.store.current_work(kind="deliveries")["rows"], [])
        self.assertIsNone(self.store.current_work()["rows"][0]["active_turn"])
        self.assertEqual(self.store.snapshot(self.sid)["messages"][0]["state"], "submitted")

    def test_unknown_state_is_not_reported_as_exact_empty_current_work(self):
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET state='new-unhandled-state' WHERE session_id=?", (self.sid,))
        with self.assertRaisesRegex(StoreError, "unknown current-work state"):
            self.store.current_work()
