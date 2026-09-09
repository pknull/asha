"""Real retained role proofs with a fake worker boundary; no tmux server."""
import json
import unittest
from unittest import mock

from lib.control.orchestration import coordinator
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.session_store import SessionStore, caller_anchor
from lib.control.sessions import bridge_initiative, refuse_managed_operator
from lib.control.store import StoreError
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class NoTmux:
    def __getattr__(self, name):
        raise AssertionError("managed coordination contacted tmux: " + name)


class ManagedCoordinatorTests(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sessions = SessionStore(self.config.control, create=True)
        self.addCleanup(self.sessions.close)
        self.session = self.sessions.create(cwd=str(self.repo), prompt="Implement the approved plan",
                                            initiative_id=self.initiative_id)
        self.sid = self.session["session_id"]
        self.session = self.sessions.claim_owner(self.sid)
        self.actor_env = {**self.env, "ASHA_MANAGED_SESSION_ID": self.sid,
                          "ASHA_MANAGED_GENERATION": str(self.session["generation"]),
                          "ASHA_MANAGED_STATE_DIR": str(self.config.control.tasks_dir.parent)}
        self.record = coordinator.claim(self.store, self.initiative(), env=self.actor_env,
                                        tmux=NoTmux(), harness="claude")

    def test_claim_and_liveness_do_not_need_a_terminal(self):
        again = coordinator.claim(self.store, self.initiative(), env=self.actor_env,
                                  tmux=NoTmux(), harness="claude")
        self.assertEqual(again, self.record)
        self.assertEqual(coordinator.anchor_liveness(self.record["anchor"], NoTmux())[0], "live")
        coordinator.require_anchored_caller(self.record, self.actor_env, NoTmux())

    def legacy_claim(self):
        from tests.python.test_orchestration_coordinator_claim import FakeTmux
        return coordinator.claim(self.store, self.initiative(), env={**self.env, 'TMUX_PANE': '%7'},
                                 tmux=FakeTmux(), harness='claude')

    def test_legacy_transport_cannot_replace_live_managed_owner(self):
        with self.assertRaisesRegex(StoreError, 'stop.*managed session'):
            self.legacy_claim()
        self.assertEqual(self.store.current_coordinator(self.initiative_id), self.record)

    def test_legacy_transport_cannot_replace_stopped_uncertain_submission(self):
        self.sessions.claim_turn(self.sid, self.session['generation'])
        self.sessions.stop(self.sid)
        self.sessions.stopped(self.sid, self.session['generation'])
        with mock.patch('lib.control.session_store.process_live', return_value=False):
            for state in ('active', 'stale', 'exited', 'fenced'):
                with self.subTest(state=state), mock.patch.object(self.store, 'current_coordinator',
                        return_value={**self.record, 'state': state}):
                    with self.assertRaisesRegex(StoreError, 'uncertain'):
                        self.legacy_claim()
        self.assertEqual(self.store.current_coordinator(self.initiative_id), self.record)

    def test_legacy_transport_refuses_failed_turn_without_native_submission_proof(self):
        turn = self.sessions.claim_turn(self.sid, self.session['generation'])
        self.sessions.finish(self.sid, self.session['generation'], turn['turn_id'], success=False,
                             reason='provider connection lost after input')
        self.sessions.stop(self.sid)
        self.sessions.stopped(self.sid, self.session['generation'])
        with mock.patch('lib.control.session_store.process_live', return_value=False):
            with self.assertRaisesRegex(StoreError, 'uncertain'):
                self.legacy_claim()

    def test_legacy_transport_can_replace_stopped_settled_session_without_new_initiative(self):
        turn = self.sessions.claim_turn(self.sid, self.session['generation'])
        self.sessions.finish(self.sid, self.session['generation'], turn['turn_id'], success=True)
        self.sessions.stop(self.sid)
        self.sessions.stopped(self.sid, self.session['generation'])
        with mock.patch('lib.control.session_store.process_live', return_value=False):
            legacy = self.legacy_claim()
        self.assertEqual(legacy['initiative_id'], self.initiative_id)
        self.assertEqual(legacy['predecessor_coordinator_id'], self.record['coordinator_id'])
        self.assertEqual(legacy['generation'], self.record['generation'] + 1)
        self.assertEqual(self.sessions.get(self.sid)['state'], 'stopped')

    def test_legacy_transport_refuses_foreign_predecessor_state_root(self):
        foreign = {**self.record, 'anchor': {**self.record['anchor'], 'state_dir': '/tmp/foreign-control'}}
        with mock.patch.object(self.store, 'current_coordinator', return_value=foreign):
            with self.assertRaisesRegex(coordinator.CoordinatorError, 'state root'):
                self.legacy_claim()

    def test_legacy_transport_uses_reservation_order_after_explicit_recovery(self):
        first = self.sessions.claim_turn(self.sid, self.session['generation'])
        self.sessions.stop(self.sid)
        self.sessions.stopped(self.sid, self.session['generation'])
        stopped = self.sessions.get(self.sid)
        self.sessions.resume(self.sid, prompt='Inspected prior work; continue',
                             expected_digest=self.sessions.recovery_digest(stopped))
        last = self.sessions.claim_turn(self.sid, self.session['generation'])
        self.sessions.finish(self.sid, self.session['generation'], last['turn_id'], success=True)
        self.sessions.stop(self.sid)
        self.sessions.stopped(self.sid, self.session['generation'])
        with self.sessions.db.transaction(write=True) as c:
            c.execute('UPDATE session_turns SET started_at=started_at+3600 WHERE turn_id=?', (first['turn_id'],))
        with mock.patch('lib.control.session_store.process_live', return_value=False):
            legacy = self.legacy_claim()
        self.assertEqual(legacy['generation'], self.record['generation'] + 1)

    def test_labels_do_not_override_generation(self):
        with self.assertRaisesRegex(StoreError, "stale"):
            caller_anchor({**self.actor_env, "ASHA_MANAGED_GENERATION": "99"})

    def test_operator_actions_refuse_even_with_environment_stripped(self):
        for env in (self.actor_env, self.env):
            with self.assertRaises(StoreError):
                refuse_managed_operator(self.config.control, env)
            with self.assertRaises((StoreError, coordinator.CoordinatorError)):
                coordinator.refuse_coordinator_pane(self.store, self.initiative_id, env, NoTmux())

    def test_existing_worker_dispatch_and_replay_from_managed_coordinator(self):
        document = build_action_document(self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
                                         actor_id=coordinator.actor_id(self.record), coordinator=self.record)
        def capture(argv, **kwargs):
            return 0, json.dumps(self.control_payload(argv)).encode(), b""
        with mock.patch("lib.control.orchestration.scheduler.storage_report", return_value={"pause_recommended": False}), \
             mock.patch("lib.control.orchestration.scheduler.capture_bytes", side_effect=capture) as dispatch:
            first = submit_action(self.store, self.initiative_id, document)
            again = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(first, again)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(first["actor_kind"], "coordinator")

    def test_coordinator_cannot_self_approve(self):
        document = build_action_document(self.initiative(), "activate-initiative", {},
                                         actor_id=coordinator.actor_id(self.record), coordinator=self.record)
        result = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(result["state"], "refused")

    def test_legacy_event_bridge_is_idempotent(self):
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        before = self.sessions.snapshot(self.sid)
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        self.assertEqual(before, self.sessions.snapshot(self.sid))

    def test_activation_and_resume_wake_an_idle_coordinator_once(self):
        from lib.control.orchestration.actions import append_event

        # The initial turn already read the approved plan and finished. An
        # operator activates later, without sending a separate chat message.
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        initial = self.sessions.claim_turn(self.sid, self.session["generation"])
        self.sessions.finish(self.sid, self.session["generation"], initial["turn_id"], success=True)
        for prior in ("approved", "paused", "needs-input"):
            with self.subTest(prior=prior):
                event = append_event(
                    self.store, self.initiative_id, "initiative-state-changed", [self.initiative_id],
                    {"from": prior, "to": "running"}, actor_kind="controller", actor_id="action-broker")
                bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
                notification = self.sessions.claim_turn(self.sid, self.session["generation"])
                self.assertIsNotNone(notification, "an operator state transition must resume coordination")
                self.assertIn("initiative-state-changed", notification["body"])
                self.assertIn(str(event["sequence"]), notification["body"])
                self.sessions.finish(self.sid, self.session["generation"], notification["turn_id"], success=True)
                bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
                self.assertIsNone(self.sessions.claim_turn(self.sid, self.session["generation"]))

    def test_initial_activation_and_later_heartbeats_do_not_spend_extra_turns(self):
        from lib.control.orchestration.actions import append_event

        append_event(self.store, self.initiative_id, "initiative-state-changed", [],
                     {"from": "approved", "to": "running"}, actor_kind="controller", actor_id="action-broker")
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        initial = self.sessions.claim_turn(self.sid, self.session["generation"])
        self.sessions.finish(self.sid, self.session["generation"], initial["turn_id"], success=True)
        append_event(self.store, self.initiative_id, "task-status-observed", [], {},
                     actor_kind="controller", actor_id="supervisor")
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        self.assertIsNone(self.sessions.claim_turn(self.sid, self.session["generation"]))
        self.assertEqual(self.sessions.get(self.sid)["turns"], 1)

    def test_new_session_keeps_prior_refusals_while_baselining_plan_approval(self):
        from lib.control.orchestration.actions import append_event
        append_event(self.store, self.initiative_id, "result-refused", [], {"reason": "verification failed"},
                     actor_kind="controller", actor_id="result-ingester")
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        with self.sessions.db.transaction() as c:
            bodies = [row[0] for row in c.execute("SELECT body FROM session_messages WHERE delivery_key LIKE 'initiative-events:%'")]
        self.assertEqual(len(bodies), 1)
        self.assertIn("result-refused", bodies[0])
        self.assertNotIn("plan-approved", bodies[0])

    def test_stale_event_snapshot_cannot_rewind_cursor_or_queue_old_notifications(self):
        from lib.control.orchestration.actions import append_event
        session = self.sessions.get(self.sid)
        old_events = self.store.list_events_snapshot(self.initiative_id)
        event = append_event(self.store, self.initiative_id, "result-refused", [], {},
                             actor_kind="controller", actor_id="result-ingester")
        bridge_initiative(self.sessions, session, self.store)
        before = self.sessions.snapshot(self.sid)
        with mock.patch.object(self.store, "list_events_snapshot", return_value=old_events):
            bridge_initiative(self.sessions, session, self.store)
        self.assertEqual(self.sessions.get(self.sid)["event_cursor"], event["sequence"])
        self.assertEqual(self.sessions.snapshot(self.sid), before)

    def test_large_event_summary_labels_omissions_with_a_read_cursor(self):
        from lib.control.orchestration.actions import append_event
        for index in range(41):
            append_event(self.store, self.initiative_id, "result-refused", [], {"ordinal": index},
                         actor_kind="controller", actor_id="result-ingester")
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        with self.sessions.db.transaction() as c:
            body = c.execute("SELECT body FROM session_messages WHERE delivery_key LIKE 'initiative-events:%'").fetchone()[0]
        self.assertIn("1 earlier relevant event omitted", body)
        self.assertIn(f"asha initiative events {self.initiative_id} --after 0 --json", body)

    def test_idle_bridge_does_not_compete_for_the_database_writer(self):
        from lib.control.database import ControlDatabase
        bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
        with ControlDatabase(self.config.control) as other, other.transaction(write=True):
            bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)

    def test_stale_owner_cannot_enqueue_an_addressed_message_before_refusal(self):
        from lib.control.orchestration import messages
        import uuid
        session = self.sessions.get(self.sid)
        before = self.sessions.snapshot(self.sid)
        pending = {"messages": [{"address_status": "current", "message_id": str(uuid.uuid4()),
                                 "content_digest": "a" * 64}]}
        with mock.patch.object(messages, "pending", return_value=pending), self.assertRaises(StoreError):
            bridge_initiative(self.sessions, {**session, "generation": session["generation"] + 1}, self.store)
        self.assertEqual(self.sessions.snapshot(self.sid), before)

    def test_unacked_but_retained_message_does_not_compete_for_writer(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration import messages
        import uuid
        pending = {"messages": [{"address_status": "current", "message_id": str(uuid.uuid4()),
                                 "content_digest": "a" * 64}]}
        with mock.patch.object(messages, "pending", return_value=pending):
            bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
            before = self.sessions.snapshot(self.sid)
            with ControlDatabase(self.config.control) as other, other.transaction(write=True):
                bridge_initiative(self.sessions, self.sessions.get(self.sid), self.store)
            self.assertEqual(self.sessions.snapshot(self.sid), before)

    def test_owner_replaced_between_bridge_transactions_cannot_enqueue(self):
        from contextlib import contextmanager
        from lib.control.orchestration import messages
        import uuid
        session = self.sessions.get(self.sid)
        pending = {"messages": [{"address_status": "current", "message_id": str(uuid.uuid4()),
                                 "content_digest": "a" * 64}]}
        original = self.sessions.db.transaction
        @contextmanager
        def replace_before_write(*, write=False):
            if write:
                with original(write=True) as c:
                    c.execute("UPDATE managed_sessions SET generation=generation+1 WHERE session_id=?", (self.sid,))
            with original(write=write) as c:
                yield c
        with mock.patch.object(messages, "pending", return_value=pending), \
             mock.patch.object(self.sessions.db, "transaction", replace_before_write), \
             self.assertRaisesRegex(StoreError, "stale"):
            bridge_initiative(self.sessions, session, self.store)
        with original() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM session_messages WHERE delivery_key LIKE 'legacy-message:%'").fetchone()[0], 0)

    def test_busy_bridge_retries_without_spending_a_turn(self):
        from lib.control.database import DatabaseBusyError
        from lib.control.sessions import run_owner
        real_bridge = bridge_initiative
        calls = []
        def busy_once(store, session, orchestration):
            calls.append(session["turns"])
            if len(calls) == 1:
                raise DatabaseBusyError("temporary writer contention")
            return real_bridge(store, session, orchestration)
        def finish(store, session, message, **kwargs):
            store.finish(self.sid, session["generation"], message["turn_id"], success=True)
            store.stop(self.sid)
        with mock.patch("lib.control.sessions.bridge_initiative", side_effect=busy_once), \
             mock.patch("lib.control.sessions.run_turn", side_effect=finish) as provider, \
             mock.patch("lib.control.sessions.time.sleep") as sleep:
            self.assertEqual(run_owner(self.config.control, self.sid, env=self.env), 0)
        self.assertEqual(calls, [0, 0])
        self.assertEqual(provider.call_count, 1)
        self.assertGreaterEqual(sleep.call_count, 1)
        self.assertEqual(self.sessions.get(self.sid)["turns"], 1)

    def test_lost_initiative_ownership_records_failure_before_dispatch(self):
        from lib.control.sessions import run_owner
        with mock.patch("lib.control.sessions.bridge_initiative", side_effect=StoreError("initiative owner changed")), \
             mock.patch("lib.control.sessions.run_turn") as provider, \
             self.assertRaisesRegex(StoreError, "initiative owner changed"):
            run_owner(self.config.control, self.sid, env=self.env, once=True)
        provider.assert_not_called()
        snapshot = self.sessions.snapshot(self.sid)
        self.assertEqual(self.sessions.get(self.sid)["state"], "failed")
        self.assertEqual(self.sessions.get(self.sid)["turns"], 0)
        self.assertTrue(any(e["kind"] == "owner-failed" for e in snapshot["events"]))


class SQLiteManagedCoordinatorTests(ManagedCoordinatorTests):
    def setUp(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
        def factory(config):
            with ControlDatabase(config.control, create=True):
                pass
            return SQLiteInitiativeStore(config)
        with mock.patch('tests.python.orchestration_execution_fixtures.InitiativeStore', side_effect=factory):
            super().setUp()
        # Owner entry points construct their own store. Keep the explicit
        # fixture backend there too; no active migration marker is fabricated.
        owner_store = mock.patch('lib.control.orchestration.store.InitiativeStore', side_effect=factory)
        owner_store.start()
        self.addCleanup(owner_store.stop)


if __name__ == "__main__":
    unittest.main()
