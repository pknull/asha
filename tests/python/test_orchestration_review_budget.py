import copy
import json
import unittest
from unittest import mock
from types import SimpleNamespace

from lib.control.orchestration.actions import ActionRefused, action_outcome, build_action_document, submit_action
from lib.control.orchestration.review_budget import approve, validate_request
from lib.control.orchestration.scheduler import refresh_readiness
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.orchestration_increment3_fixtures import advance_node, save_candidate
from tests.python import test_orchestration_review as review_fixtures


class ReviewBudgetTests(ExecutionFixture, unittest.TestCase):
    _dispatch_review = review_fixtures.OrchestrationReviewTests._dispatch_review
    _publish = review_fixtures.OrchestrationReviewTests._publish
    _complete = review_fixtures.OrchestrationReviewTests._complete

    def setUp(self):
        super().setUp()
        self.candidate = save_candidate(self)
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        refresh_readiness(self.store, self.initiative_id)
        self._dispatch_review()

    def exhaust(self):
        for n in range(2):
            self._publish("pass")
            self._complete(mutating=True)
            if n == 0:
                self._dispatch_review()
        self.assertEqual(self.store.read_node(self.initiative_id, "review-a")["state"], "failed")

    def request(self):
        doc = build_action_document(self.initiative(), "request-review-budget", {
            "node_id": "review-a", "review_id": self.review["review_id"],
            "reason": "Retry the exact sealed review after correcting provider failure",
        })
        result = submit_action(self.store, self.initiative_id, doc)
        return doc, result

    def test_request_and_approval_preserve_seal_limits_and_old_reviews(self):
        self.exhaust()
        before = self.initiative()
        reviews = self.store.list_reviews_snapshot(self.initiative_id)
        doc, result = self.request()
        self.assertEqual(result["state"], "completed", action_outcome(result))
        request_id = action_outcome(result)["request_id"]
        requested = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(requested["state"], "requested")
        self.assertNotIn("decided_by", requested)
        self.assertEqual(submit_action(self.store, self.initiative_id, doc), result)
        signed = approve(self.store, self.initiative_id, request_id)
        self.assertEqual(signed["state"], "approved")
        self.assertEqual(approve(self.store, self.initiative_id, request_id), signed)
        self.assertEqual(before["limits"], self.initiative()["limits"])
        self.assertEqual(before["active_plan"], self.initiative()["active_plan"])
        self.assertEqual(reviews, self.store.list_reviews_snapshot(self.initiative_id))
        _, binding = validate_request(self.store, self.initiative_id, request_id)
        self.assertEqual(binding["additional_attempts"], 1)
        self.assertEqual(binding["target"]["seal_id"], self.candidate["seal_id"])

    def test_ordinary_budget_needs_no_extra_approval(self):
        self._publish("pass")
        self._complete(mutating=True)
        _, result = self.request()
        self.assertEqual(result["state"], "refused")
        self.assertFalse(any(a["action_class"] == "review-budget" for a in self.store.list_approvals_snapshot(self.initiative_id)))

    def test_accepted_findings_cannot_be_retried_as_budget_recovery(self):
        self._publish("findings")
        self._complete()
        _, result = self.request()
        self.assertEqual(result["state"], "refused")

    def test_non_operator_cannot_sign(self):
        self.exhaust()
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        for actor in ("coordinator", "scheduler", "standing-authority:12345678"):
            with self.subTest(actor=actor), self.assertRaisesRegex(ActionRefused, "operator"):
                approve(self.store, self.initiative_id, request_id, actor_id=actor)

    def test_changed_seal_refuses_stale_approval(self):
        self.exhaust()
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        from lib.control.orchestration.review import review_target
        original = review_target(self.store, self.initiative(), self.store.read_plan(self.initiative_id, self.initiative()["active_plan"]["revision"]), self.store.read_node(self.initiative_id, "review-a"))
        changed = copy.deepcopy(original[1])
        changed["diff_digest"] = "0" * 64
        with mock.patch("lib.control.orchestration.review.review_target", return_value=(original[0], changed)):
            with self.assertRaisesRegex(ActionRefused, "exact seal"):
                approve(self.store, self.initiative_id, request_id)

    def test_second_pending_request_is_refused(self):
        self.exhaust()
        _, first = self.request()
        _, second = self.request()
        self.assertEqual(first["state"], "completed")
        self.assertEqual(second["state"], "refused")

    def test_signed_budget_runs_one_fresh_review_against_same_seal(self):
        self.exhaust()
        old_target = copy.deepcopy(self.review["target"])
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        approve(self.store, self.initiative_id, request_id)
        self._dispatch_review()
        self.assertEqual(self.attempt["ordinal"], 3)
        self.assertEqual(self.review["target"], old_target)
        self.assertEqual(self.store.read_approval(self.initiative_id, request_id)["state"], "consumed")
        self._publish("pass")
        completed = self._complete()
        self.assertEqual(completed["state"], "accepted-pass")

    def test_failed_extra_review_cannot_reuse_consumed_grant(self):
        self.exhaust()
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        approve(self.store, self.initiative_id, request_id)
        self._dispatch_review()
        self._publish("pass")
        self._complete(mutating=True)
        from lib.control.orchestration.review_budget import available
        self.assertIsNone(available(self.store, self.initiative_id, "review-a"))
        with self.assertRaises(ActionRefused):
            self.store.reopen_failed_review(self.initiative_id, request_id)
        self.assertEqual(self.store.read_node(self.initiative_id, "review-a")["state"], "failed")

    def test_unapproved_request_cannot_reopen_failed_node(self):
        self.exhaust()
        _, result = self.request()
        with self.assertRaisesRegex(ActionRefused, "signature"):
            self.store.reopen_failed_review(self.initiative_id, action_outcome(result)["request_id"])

    def test_control_prompt_shows_exact_seal_and_requires_explicit_approval(self):
        from lib.control.tui import _approve_review_budget_prompt
        self.exhaust()
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        row = SimpleNamespace(kind="node", id="review-a")
        with mock.patch("lib.control.tui._prompt_line", return_value="cancel") as prompt, mock.patch("lib.control.tui._refresh_initiatives"):
            outcome = _approve_review_budget_prompt(None, None, None, self.env, self.store, self.initiative(), row)
            self.assertIn("cancelled", outcome)
            self.assertIn(self.candidate["seal_id"], prompt.call_args.kwargs["context"])
            self.assertEqual(self.store.read_approval(self.initiative_id, request_id)["state"], "requested")
        with mock.patch("lib.control.tui._prompt_line", return_value="approve"), mock.patch("lib.control.tui._refresh_initiatives"):
            outcome = _approve_review_budget_prompt(None, None, None, self.env, self.store, self.initiative(), row)
            self.assertIn("approved", outcome)
            self.assertEqual(self.store.read_approval(self.initiative_id, request_id)["decided_by"]["actor_id"], "tui")

    def test_control_projection_names_review_budget_not_salvage(self):
        from lib.control.orchestration.tui_model import attention_items
        from tests.python.test_control_tui_initiatives_mode import _view
        self.exhaust()
        _, result = self.request()
        approval = self.store.read_approval(self.initiative_id, action_outcome(result)["request_id"])
        view = _view("review", "running", approvals=[approval])
        view["initiative"] = self.initiative()
        items = attention_items([view])
        budget = [item for item in items if item["kind"] == "review-budget-approval"]
        self.assertEqual(len(budget), 1)
        self.assertIn("approve-review-budget", budget[0]["resolution"])

    def test_lost_consumption_response_recovers_same_attempt_once(self):
        from lib.control.orchestration.actions import reconcile_actions
        self.exhaust()
        _, result = self.request()
        request_id = action_outcome(result)["request_id"]
        approve(self.store, self.initiative_id, request_id)
        original = self.store.save_approval
        def lost_response(iid, record, **kwargs):
            value = original(iid, record, **kwargs)
            if record["state"] == "consumed":
                raise OSError("lost consumption response")
            return value
        doc = build_action_document(self.initiative(), "dispatch-node", {"node_id": "review-a"})
        with mock.patch.object(self.store, "save_approval", side_effect=lost_response), mock.patch(
                "lib.control.orchestration.scheduler.storage_report", return_value={"pause_recommended": False}), mock.patch(
                "lib.control.orchestration.scheduler.capture_bytes") as launch:
            from lib.control.orchestration.store import StoreError
            with self.assertRaisesRegex(StoreError, "lost consumption response"):
                submit_action(self.store, self.initiative_id, doc)
            launch.assert_not_called()
        interrupted = self.store.read_action(self.initiative_id, doc["action_id"])
        self.assertEqual(interrupted["state"], "dispatching")
        attempt_id = action_outcome(interrupted)["attempt_id"]
        def capture(argv, **kwargs):
            value = self.control_payload(argv)
            task = value["task"]
            workspace = self.config.control.workspace_root / task["task_id"]
            task["jj"].update(base_commit_id=argv[argv.index("--base") + 1], working_commit_id="f" * 40, workspace_path=str(workspace))
            value["workspace"]["path"] = str(workspace)
            return 0, json.dumps(value).encode(), b""
        with mock.patch("lib.control.orchestration.scheduler.storage_report", return_value={"pause_recommended": False}), mock.patch(
                "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture) as launch:
            reconcile_actions(self.store, self.initiative_id)
            reconcile_actions(self.store, self.initiative_id)
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.store.read_action(self.initiative_id, doc["action_id"])["state"], "completed")
        self.assertEqual(len([a for a in self.store.list_attempts_snapshot(self.initiative_id) if a["node_id"] == "review-a"]), 3)
        self.assertEqual(self.store.read_attempt(self.initiative_id, attempt_id)["state"], "running")

    def test_grant_does_not_bypass_deadline_or_storage_pause(self):
        from lib.control.orchestration.scheduler import readiness
        self.exhaust()
        _, result = self.request()
        approve(self.store, self.initiative_id, action_outcome(result)["request_id"])
        for path, value in (("_deadline_reached", True), ("storage_report", {"pause_recommended": True})):
            with self.subTest(gate=path), mock.patch("lib.control.orchestration.scheduler." + path, return_value=value):
                self.assertEqual(readiness(self.store, self.initiative())["review-a"], "blocked")

    def test_expired_request_is_retired_from_pending_approvals(self):
        from datetime import datetime, timedelta, timezone
        from lib.control.orchestration.review_budget import reconcile_requests
        self.exhaust()
        _, result = self.request()
        rid = action_outcome(result)["request_id"]
        with mock.patch("lib.control.orchestration.review_budget.datetime") as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=2)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.store.transaction_lock(self.initiative_id):
                reconcile_requests(self.store, self.initiative_id)
        self.assertEqual(self.store.read_approval(self.initiative_id, rid)["state"], "expired")

    def test_approval_repairs_exact_signing_event_despite_unrelated_or_null_payload(self):
        from lib.control.orchestration.review_budget import authority
        self.exhaust()
        _, result = self.request()
        rid = action_outcome(result)['request_id']
        real_events = self.store.list_events_snapshot
        def events(iid):
            unrelated = {'type': 'approval-decided', 'subject_ids': [rid],
                         'payload': None, 'actor_kind': 'controller', 'actor_id': 'action-reconciler'}
            return [unrelated, *real_events(iid)]
        with mock.patch.object(self.store, 'list_events_snapshot', side_effect=events):
            approve(self.store, self.initiative_id, rid)
            self.assertEqual(authority(self.store, self.initiative_id, rid)['approval']['state'], 'approved')
        signed = [e for e in real_events(self.initiative_id) if e['type'] == 'approval-decided'
                  and rid in e['subject_ids'] and e['actor_kind'] == 'operator']
        self.assertEqual(len(signed), 1)

    def test_reconcile_repairs_crash_after_terminal_record_without_restoring_authority(self):
        from datetime import datetime, timedelta, timezone
        from lib.control.orchestration.review_budget import reconcile_requests, authority
        self.exhaust()
        _, result = self.request()
        rid = action_outcome(result)['request_id']
        approve(self.store, self.initiative_id, rid)
        with mock.patch('lib.control.orchestration.review_budget.datetime') as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=2)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            with self.store.transaction_lock(self.initiative_id):
                with mock.patch('lib.control.orchestration.actions.append_event', side_effect=RuntimeError('crash')):
                    with self.assertRaisesRegex(RuntimeError, 'crash'):
                        reconcile_requests(self.store, self.initiative_id)
                reconcile_requests(self.store, self.initiative_id)
                reconcile_requests(self.store, self.initiative_id)
            with self.assertRaises(ActionRefused):
                authority(self.store, self.initiative_id, rid)
        self.assertEqual(self.store.read_approval(self.initiative_id, rid)['state'], 'expired')
        events = [e for e in self.store.list_events_snapshot(self.initiative_id)
                  if e['type'] == 'approval-decided' and rid in e['subject_ids']
                  and e['payload'].get('decision') == 'expired']
        self.assertEqual(len(events), 1)

    def test_expired_orphan_request_leaves_attention_queue(self):
        from datetime import datetime, timedelta, timezone
        from lib.control.orchestration.review_budget import reconcile_requests
        self.exhaust()
        _, result = self.request()
        rid = action_outcome(result)['request_id']
        with mock.patch.object(self.store, 'list_actions_snapshot', return_value=[]):
            with self.store.transaction_lock(self.initiative_id):
                reconcile_requests(self.store, self.initiative_id)
            self.assertEqual(self.store.read_approval(self.initiative_id, rid)['state'], 'requested')
            with mock.patch('lib.control.orchestration.review_budget.datetime') as clock:
                clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=2)
                clock.fromisoformat.side_effect = datetime.fromisoformat
                with self.store.transaction_lock(self.initiative_id):
                    reconcile_requests(self.store, self.initiative_id)
        self.assertEqual(self.store.read_approval(self.initiative_id, rid)['state'], 'expired')

    def test_global_task_amendment_does_not_create_capacity_for_other_nodes(self):
        from lib.control.orchestration.model import record_digest
        from lib.control.orchestration.scheduler import readiness
        self.exhaust()
        old = self.initiative()
        changed = copy.deepcopy(old)
        changed["limits"]["max_total_tasks"] = len(self.store.list_attempts_snapshot(self.initiative_id))
        changed["state_revision"] += 1
        self.store.save_initiative(changed, expected_digest=record_digest(old))
        _, result = self.request()
        approve(self.store, self.initiative_id, action_outcome(result)["request_id"])
        self._dispatch_review()
        self._publish("pass")
        self._complete()
        with mock.patch("lib.control.orchestration.scheduler.storage_report", return_value={"pause_recommended": False}):
            self.assertEqual(readiness(self.store, self.initiative())["verify-a"], "blocked")
        self.assertEqual(self.initiative()["limits"], changed["limits"])

    def test_convenience_cli_requests_and_signs_one_bound_amendment(self):
        from lib.control.orchestration.cli import _operator_action, _approve_review_budget_command
        from lib.control.orchestration.coordinator import CoordinatorError
        self.exhaust()
        result, _ = _operator_action("request-review-budget", [self.initiative_id, "--node", "review-a", "--review", self.review["review_id"], "--reason", "Provider recovered", "--json"], self.store, self.env)
        rid = action_outcome(result)["request_id"]
        with self.assertRaisesRegex(CoordinatorError, "operator verbs"):
            _approve_review_budget_command([self.initiative_id, "--request", rid], self.store, {**self.env, "ASHA_ORCHESTRATION_COORDINATOR_ID": "forbidden"}, None)
        result, _ = _approve_review_budget_command([self.initiative_id, "--request", rid], self.store, self.env, None)
        self.assertEqual(result["approval"]["state"], "approved")


class SQLiteReviewBudgetTests(ReviewBudgetTests):
    def setUp(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
        def factory(config):
            with ControlDatabase(config.control, create=True):
                pass
            return SQLiteInitiativeStore(config)
        with mock.patch("tests.python.orchestration_execution_fixtures.InitiativeStore", side_effect=factory):
            super().setUp()
