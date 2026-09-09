import contextlib
import io
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.session_harness import decode_claude
from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from tests.python import test_control_managed_sessions as fixtures


def quota(reset=None, status="rejected"):
    info = {"status": status, "rateLimitType": "five_hour"}
    if reset is not None:
        info["resetsAt"] = reset
    return {"type": "rate_limit_event", "rate_limit_info": info}


class ProviderDecodeTests(unittest.TestCase):
    def test_only_structured_provider_fields_report_quota(self):
        event, payload = list(decode_claude(quota(2000000000)))[0]
        self.assertEqual(event, "provider-status")
        self.assertEqual(payload["reason"], "rate_limit")
        self.assertEqual(payload["reset_at"], 2000000000)
        ordinary = list(decode_claude({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "The story mentions a rate limit, billing_error and quota."}]}}))
        self.assertEqual([kind for kind, _ in ordinary], ["text"])

    def test_assistant_error_and_result_status_are_typed_failure_observations(self):
        assistant = list(decode_claude({"type": "assistant", "error": "rate_limit", "message": {"content": []}}))
        self.assertEqual(assistant[0][1]["reason"], "rate_limit")
        terminal = list(decode_claude({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429}))
        self.assertEqual([kind for kind, _ in terminal], ["provider-status", "failed"])

    def test_invalid_rate_limit_metadata_is_refused(self):
        for reset in (True, -1, "tomorrow", float("nan"), float("inf")):
            with self.subTest(reset=reset), self.assertRaises(StoreError):
                list(decode_claude(quota(reset)))
        with self.assertRaises(StoreError):
            list(decode_claude(quota(status="pretend-success")))

    def test_error_diagnosis_does_not_depend_on_valid_diagnostic_body(self):
        from tests.python.test_control_claude_protocol import ClaudeProtocolTests
        frame = {"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": 123}]}}
        direct = list(decode_claude(frame))
        self.assertEqual(ClaudeProtocolTests().protocol().feed(frame), direct)
        self.assertEqual(direct[0][1]["reason"], "rate_limit")

    def test_native_budget_cancellation_and_http_error_are_terminal_failures(self):
        for field, value, reason in (("subtype", "error_max_turns", "native_budget"),
                                     ("subtype", "error_max_budget_usd", "native_budget"),
                                     ("terminal_reason", "aborted_streaming", "cancelled"),
                                     ("terminal_reason", "aborted_tools", "cancelled"),
                                     ("api_error_status", 429, "rate_limit")):
            with self.subTest(value=value):
                events = list(decode_claude({"type": "result", "subtype": "success", field: value}))
                self.assertEqual(events[0][1]["reason"], reason)
                self.assertEqual(events[-1][0], "failed")

    def test_protocol_accepts_limit_metadata_after_result_without_reopening_turn(self):
        from tests.python.test_control_claude_protocol import ClaudeProtocolTests
        protocol = ClaudeProtocolTests().protocol()
        self.assertEqual(protocol.feed({"type": "result", "subtype": "success"})[0][0], "completed")
        self.assertEqual(protocol.feed(quota(2000000000))[0][0], "provider-status")
        self.assertTrue(protocol.terminal)
        with self.assertRaises(StoreError):
            protocol.feed({"type": "assistant", "message": {"content": []}})


class ProviderRecoveryTests(unittest.TestCase):
    setUp = fixtures.SessionTests.setUp
    claim = fixtures.SessionTests.claim

    def fail_with_quota(self, reset=None, *, terminal=True):
        turn = self.claim()
        for event, payload in decode_claude(quota(reset)):
            self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        if terminal:
            self.store.observe(self.sid, self.generation, turn["turn_id"], "failed", {"reason": "error_during_execution"})
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, reason="provider stopped")
        return turn

    def test_quota_parks_with_durable_condition_and_no_automatic_retry(self):
        reset = int(time.time()) + 3600
        turn = self.fail_with_quota(reset)
        with SessionStore(self.config) as reopened:
            session = reopened.get(self.sid)
            self.assertEqual(session["state"], "failed")
            self.assertEqual(session["turns"], 1)
            self.assertEqual(session["recovery"]["category"], "quota")
            self.assertEqual(session["recovery"]["retry_not_before"], reset)
            self.assertEqual(session["recovery"]["turn_id"], turn["turn_id"])
            self.assertEqual(session["recovery"]["delivery"], "terminal-failure")
            self.assertIsNone(reopened.claim_turn(self.sid, self.generation))
            self.assertFalse(reopened.reserve_owner_launch(self.sid))
            with self.assertRaisesRegex(StoreError, "reset"):
                reopened.resume(self.sid, prompt="Continue after reset", expected_digest=reopened.recovery_digest(session))
        from lib.control.sessions import overview
        summary = overview(self.config)
        self.assertEqual(summary["recovery_counts"]["quota"], 1)
        self.assertIn("1 quota-blocked", summary["summary"])

    def test_explicit_recovery_after_reset_preserves_old_turn_and_queues_one_new_input(self):
        turn = self.fail_with_quota(int(time.time()) - 1)
        session = self.store.get(self.sid)
        seal = self.store.recovery_digest(session)
        message = self.store.resume(self.sid, prompt="Continue after checking retained state", expected_digest=seal)
        self.assertEqual(message, self.store.resume(self.sid, prompt="Continue after checking retained state", expected_digest=seal))
        self.assertIsNone(self.store.get(self.sid)["recovery"])
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        next_turn = self.claim()
        self.assertNotEqual(next_turn["turn_id"], turn["turn_id"])
        self.assertNotEqual(next_turn["message_id"], turn["message_id"])
        self.assertEqual(self.store.get(self.sid)["turns"], 2)

    def test_incomplete_stream_preserves_quota_reason_and_submission_uncertainty(self):
        self.fail_with_quota()
        session = self.store.get(self.sid)
        self.assertEqual(session["recovery"]["retry_condition"], "Confirm provider quota is available, inspect retained work, then resume explicitly")
        # A separate session with a rate-limit observation but no terminal frame.
        sid = self.store.create(cwd=self.tmp.name, prompt="Another assignment")["session_id"]
        generation = self.store.claim_owner(sid)["generation"]
        turn = self.store.claim_turn(sid, generation)
        event, payload = list(decode_claude(quota()))[0]
        self.store.observe(sid, generation, turn["turn_id"], event, payload)
        self.store.finish(sid, generation, turn["turn_id"], success=False, reason="EOF")
        self.assertEqual(self.store.get(sid)["recovery"]["delivery"], "uncertain")

    def test_warning_or_recovered_limit_does_not_misclassify_generic_failure(self):
        turn = self.claim()
        for value in (quota(2000000000), quota(status="allowed_warning")):
            event, payload = list(decode_claude(value))[0]
            self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, reason="stream interrupted")
        self.assertEqual(self.store.get(self.sid)["recovery"]["category"], "transport")
        self.assertIsNone(self.store.get(self.sid)["recovery"]["retry_not_before"])

    def test_success_after_transient_quota_has_no_pending_recovery(self):
        turn = self.claim()
        event, payload = list(decode_claude(quota(2000000000)))[0]
        self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.assertEqual(self.store.get(self.sid)["state"], "idle")
        self.assertIsNone(self.store.get(self.sid)["recovery"])

    def test_later_terminal_error_without_reset_keeps_known_quota_reset(self):
        turn = self.claim()
        reset = int(time.time()) + 3600
        frames = (quota(reset), {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429})
        for frame in frames:
            for event, payload in decode_claude(frame):
                self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        self.assertEqual(self.store.get(self.sid)["recovery"]["retry_not_before"], reset)

    def test_replayed_recovery_cannot_silently_change_budget_amendment(self):
        self.fail_with_quota()
        seal = self.store.recovery_digest(self.store.get(self.sid))
        self.store.resume(self.sid, prompt="Continue", expected_digest=seal, max_turns=14)
        with self.assertRaisesRegex(StoreError, "different.*budget|different.*content"):
            self.store.resume(self.sid, prompt="Continue", expected_digest=seal, max_turns=16)

    def test_old_resume_refuses_after_a_second_failure(self):
        self.fail_with_quota()
        seal = self.store.recovery_digest(self.store.get(self.sid))
        self.store.resume(self.sid, prompt="Continue", expected_digest=seal)
        self.fail_with_quota()
        with self.assertRaisesRegex(StoreError, "changed"):
            self.store.resume(self.sid, prompt="Continue", expected_digest=seal)

    def test_stop_resume_stop_has_a_fresh_recovery_digest(self):
        with mock.patch("lib.control.session_store.time.time", return_value=12345):
            self.store.stopped(self.sid, self.generation)
            seal = self.store.recovery_digest(self.store.get(self.sid))
            self.store.resume(self.sid, prompt="Continue", expected_digest=seal)
            self.store.stopped(self.sid, self.generation)
            current = self.store.recovery_digest(self.store.get(self.sid))
            self.assertNotEqual(current, seal)
            with self.assertRaisesRegex(StoreError, "changed"):
                self.store.resume(self.sid, prompt="Continue", expected_digest=seal)
            self.store.resume(self.sid, prompt="Continue", expected_digest=current)

    def test_unrelated_window_cannot_clear_quota_rejection(self):
        turn = self.claim()
        reset = int(time.time()) + 3600
        weekly = quota(reset + 3600, "allowed")
        weekly["rate_limit_info"]["rateLimitType"] = "seven_day"
        for frame in (quota(reset), weekly):
            for kind, payload in decode_claude(frame):
                self.store.observe(self.sid, self.generation, turn["turn_id"], kind, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        current = self.store.get(self.sid)
        self.assertEqual(current["recovery"]["category"], "quota")
        self.assertEqual(current["recovery"]["retry_not_before"], reset)

    def test_warning_reset_survives_following_terminal_rejection(self):
        turn = self.claim()
        reset = int(time.time()) + 3600
        for frame in (quota(reset, "allowed_warning"), {"type": "result", "subtype": "error_during_execution", "is_error": True, "api_error_status": 429}):
            for kind, payload in decode_claude(frame):
                self.store.observe(self.sid, self.generation, turn["turn_id"], kind, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        self.assertEqual(self.store.get(self.sid)["recovery"]["retry_not_before"], reset)

    def test_account_repair_stays_visible_alongside_independent_quota_gate(self):
        turn = self.claim()
        reset = int(time.time()) + 3600
        frames = (quota(reset), {"type": "assistant", "error": "authentication_failed"})
        for frame in frames:
            for kind, payload in decode_claude(frame):
                self.store.observe(self.sid, self.generation, turn["turn_id"], kind, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        current = self.store.get(self.sid)
        recovery = current["recovery"]
        self.assertEqual(recovery["category"], "authentication")
        self.assertIn("Restore provider authentication", recovery["retry_condition"])
        self.assertIn("also confirm quota", recovery["retry_condition"])
        self.assertEqual(recovery["retry_not_before"], reset)
        with self.assertRaisesRegex(StoreError, "reset"):
            self.store.resume(self.sid, prompt="Authentication repaired", expected_digest=self.store.recovery_digest(current))
        self.store.resume(self.sid, prompt="Authentication repaired and quota confirmed", expected_digest=self.store.recovery_digest(current), quota_reset_override="Corrected account confirmed available")

    def test_recovery_cancels_dead_turn_questions_and_precedes_queued_answers(self):
        turn = self.claim()
        answered, pending = str(uuid.uuid4()), str(uuid.uuid4())
        for rid in (answered, pending):
            self.store.request(self.sid, turn["turn_id"], "Which section?", request_id=rid)
        self.store.answer(answered, "Section two", expected_digest=self.store.get_request(answered)["digest"])
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        message = self.store.resume(self.sid, prompt="Inspect before continuing", expected_digest=self.store.recovery_digest(self.store.get(self.sid)))
        self.assertEqual(self.store.get_request(pending)["state"], "cancelled")
        self.assertEqual(self.claim()["message_id"], message["message_id"])

    def test_failure_metadata_participates_in_recovery_digest(self):
        self.fail_with_quota()
        session = self.store.get(self.sid)
        original = self.store.recovery_digest(session)
        session["recovery"]["retry_not_before"] = 2000000000
        self.assertNotEqual(self.store.recovery_digest(session), original)

    def test_quota_after_completed_turn_parks_next_work_without_rewriting_completion(self):
        turn = self.claim()
        self.store.observe(self.sid, self.generation, turn["turn_id"], "completed", {"reason": "success"})
        event, payload = list(decode_claude(quota(int(time.time()) + 3600)))[0]
        self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        with self.store.db.transaction() as c:
            self.assertEqual(c.execute("SELECT state FROM session_turns WHERE turn_id=?", (turn["turn_id"],)).fetchone()[0], "completed")
        self.assertEqual(self.store.get(self.sid)["recovery"]["delivery"], "terminal-completed")
        self.assertIsNone(self.claim())

    def test_transport_error_after_result_preserves_terminal_evidence(self):
        turn = self.claim()
        self.store.observe(self.sid, self.generation, turn["turn_id"], "completed", {"reason": "success"})
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, reason="unexpected post-result frame")
        with self.store.db.transaction() as c:
            self.assertEqual(c.execute("SELECT state FROM session_turns WHERE turn_id=?", (turn["turn_id"],)).fetchone()[0], "completed")
        current = self.store.get(self.sid)
        self.assertEqual(current["state"], "failed")
        self.assertIn("completed", current["recovery"]["retry_condition"])
        self.assertNotIn("submission may have occurred", current["recovery"]["retry_condition"])

    def test_non_unicode_failure_reason_does_not_strand_turn(self):
        turn = self.claim()
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, reason="bad byte \udcff")
        self.assertEqual(self.store.get(self.sid)["state"], "failed")

    def test_stopped_quota_session_is_not_counted_as_waiting_recovery(self):
        from lib.control.sessions import overview
        self.fail_with_quota()
        self.store.stopped(self.sid, self.generation)
        self.assertNotIn("quota-blocked", overview(self.config)["summary"])

    def test_incorrect_reset_has_an_explicit_recorded_override(self):
        from lib.control.record_registry import RecordRegistry
        self.fail_with_quota(253402300799)
        seal = self.store.recovery_digest(self.store.get(self.sid))
        with self.assertRaisesRegex(StoreError, "reset"):
            self.store.resume(self.sid, prompt="Inspect retained work", expected_digest=seal)
        reason = "Provider account confirms availability; reset metadata is incorrect"
        message = self.store.resume(self.sid, prompt="Inspect retained work", expected_digest=seal, quota_reset_override=reason)
        self.assertEqual(message, self.store.resume(self.sid, prompt="Inspect retained work", expected_digest=seal, quota_reset_override=reason))
        with self.assertRaisesRegex(StoreError, "different"):
            self.store.resume(self.sid, prompt="Inspect retained work", expected_digest=seal)
        with self.store.db.transaction() as c:
            recorded = RecordRegistry("session-recovery").read(c, self.sid)["value"]
            self.assertEqual(recorded["quota_reset_override"], reason)
            self.assertEqual(recorded["retry_not_before"], 253402300799)
        events = self.store.snapshot(self.sid)["events"]
        self.assertEqual([e for e in events if e["kind"] == "operator-resumed"][-1]["payload"]["quota_reset_override"], reason)

    def test_quota_override_cannot_override_other_recovery_conditions(self):
        turn = self.claim()
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
        seal = self.store.recovery_digest(self.store.get(self.sid))
        for reason in ("", "Quota available"):
            with self.subTest(reason=reason), self.assertRaises(StoreError):
                self.store.resume(self.sid, prompt="Continue", expected_digest=seal, quota_reset_override=reason)

    def test_cli_quota_override_is_operator_only_and_reaches_the_receipt(self):
        from lib.control.sessions import main
        from lib.control.record_registry import RecordRegistry
        self.fail_with_quota(253402300799)
        # The provider owner has exited; this invocation represents the operator.
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?", (self.sid,))
        seal = self.store.recovery_digest(self.store.get(self.sid))
        args = ["resume", self.sid, "--text", "Inspect retained work", "--digest", seal,
                "--quota-reset-override", "Confirmed corrected account availability", "--json"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            for key in ("ASHA_MANAGED_SESSION_ID", "ASHA_CONTROL_MANAGED", "ASHA_ORCHESTRATION_COORDINATOR_ID"):
                self.assertEqual(main(args, env={**self.env, key: self.sid}), 2)
            self.assertEqual(main(args, env=self.env), 0)
        with self.store.db.transaction() as c:
            self.assertEqual(RecordRegistry("session-recovery").read(c, self.sid)["value"]["quota_reset_override"], args[-2])

    def test_replacement_owner_keeps_observed_quota_and_does_not_replay(self):
        turn = self.claim()
        event, payload = list(decode_claude(quota(2000000000)))[0]
        self.store.observe(self.sid, self.generation, turn["turn_id"], event, payload)
        with mock.patch("lib.control.session_store.process_live", return_value=False):
            current = self.store.claim_owner(self.sid)
        self.assertEqual(current["state"], "uncertain")
        self.assertEqual(current["recovery"]["category"], "quota")
        self.assertEqual(current["recovery"]["retry_not_before"], 2000000000)
        self.assertIsNone(self.store.claim_turn(self.sid, current["generation"]))

    def test_explicit_recovery_instructions_run_before_older_queued_work(self):
        self.store.enqueue(self.sid, "Older queued assignment", key="old")
        self.fail_with_quota()
        message = self.store.resume(self.sid, prompt="Inspect before continuing", expected_digest=self.store.recovery_digest(self.store.get(self.sid)))
        self.assertEqual(self.claim()["message_id"], message["message_id"])

    def test_real_pipe_failure_and_explicit_resume_keep_the_same_session_and_initiative(self):
        from lib.control.session_harness import ClaudeTransport
        from lib.control.sessions import run_turn
        iid = str(uuid.uuid4())
        sid = self.store.create(cwd=self.tmp.name, prompt="Review retained work", initiative_id=iid)["session_id"]
        generation = self.store.claim_owner(sid)["generation"]
        message = self.store.claim_turn(sid, generation)
        frames = [{"type": "system", "subtype": "init", "session_id": "same-native-session"},
                  quota(int(time.time()) - 1),
                  {"type": "result", "subtype": "error_during_execution", "is_error": True}]
        def factory(script):
            return lambda _argv, **kwargs: ClaudeTransport([sys.executable, "-c", script], **kwargs)
        script = "import sys,json; sys.stdin.read(); frames=" + repr(frames) + "; [print(json.dumps(f),flush=True) for f in frames]; sys.exit(1)"
        with self.assertRaisesRegex(StoreError, "provider exited 1"):
            run_turn(self.store, self.store.get(sid), message, env=self.env,
                     root=Path(__file__).resolve().parents[2], transport_factory=factory(script))
        failed = self.store.get(sid)
        self.assertEqual(failed["initiative_id"], iid)
        self.assertEqual(failed["native_id"], "same-native-session")
        self.assertEqual(failed["recovery"]["category"], "quota")
        self.assertEqual(failed["recovery"]["delivery"], "terminal-failure")
        self.store.resume(sid, prompt="Provider available; inspect existing work then continue", expected_digest=self.store.recovery_digest(failed))
        next_message = self.store.claim_turn(sid, generation)
        successful = "import sys,json; sys.stdin.read(); print(json.dumps({'type':'system','subtype':'init','session_id':'same-native-session'})); print(json.dumps({'type':'result','subtype':'success'}))"
        run_turn(self.store, self.store.get(sid), next_message, env=self.env,
                 root=Path(__file__).resolve().parents[2], transport_factory=factory(successful))
        complete = self.store.get(sid)
        self.assertEqual((complete["session_id"], complete["initiative_id"], complete["native_id"]),
                         (sid, iid, "same-native-session"))
        self.assertEqual(complete["turns"], 2)
        self.assertEqual(complete["state"], "idle")
        self.assertNotEqual(next_message["turn_id"], message["turn_id"])

    def test_pre_handshake_rejection_preserves_proof_that_assignment_was_not_sent(self):
        from lib.control.session_harness import ClaudeTransport
        from lib.control.sessions import run_turn
        turn = self.claim()
        script = "import sys,json; frame=json.loads(sys.stdin.readline()); assert frame['type']=='control_request'; print(json.dumps(" + repr(quota()) + "),flush=True)"
        def factory(_argv, **kwargs):
            return ClaudeTransport([sys.executable, "-c", script], structured=True, **kwargs)
        with self.assertRaises(StoreError):
            run_turn(self.store, self.store.get(self.sid), turn, env=self.env,
                     root=Path(__file__).resolve().parents[2], transport_factory=factory)
        current = self.store.get(self.sid)
        self.assertEqual(current["recovery"]["category"], "quota")
        self.assertEqual(current["recovery"]["delivery"], "not-submitted")
        self.assertIn("restate the retained assignment", current["recovery"]["retry_condition"])
        self.assertNotIn("submission may have occurred", current["recovery"]["retry_condition"])
        original = self.store.snapshot(self.sid)["messages"][0]
        self.assertEqual(original["state"], "cancelled")
        self.assertEqual(original["body"], turn["body"])
        self.assertIsNone(self.claim())

    def test_not_submitted_cannot_downgrade_retained_consumption(self):
        from lib.control.session_store import digest
        turn = self.claim()
        self.store.observe(self.sid, self.generation, turn["turn_id"], "consumed", {"digest": digest(turn["body"])})
        with self.assertRaisesRegex(StoreError, "conflicts"):
            self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, input_not_submitted=True)
