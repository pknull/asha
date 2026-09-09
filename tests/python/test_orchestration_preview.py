"""Public typed preview over real retained bindings; fakes are not native acceptance."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest import mock

from lib.control.orchestration import cli, model, preview, scheduler, verification
from lib.control.orchestration.actions import (ActionRefused, approve_salvage,
    action_outcome, build_action_document, salvage_dispatch_binding, submit_action)
from lib.control.orchestration.store import InitiativeStore
from lib.control.database import ControlDatabase
from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
from tests.python.test_control_registry_backend import install_marker_fixture
from tests.python import test_orchestration_salvage as salvage_tests
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_actions import CoordinatorEnvelope


class AssignmentPreviewTests(CoordinatorEnvelope, ExecutionFixture, unittest.TestCase):
    seal = salvage_tests.OrchestrationRecoveryActionTests.seal
    request = salvage_tests.OrchestrationRecoveryActionTests.request
    retry_reservation = salvage_tests.OrchestrationRecoveryActionTests.retry_reservation

    def customize_plan(self, plan):
        plan["nodes"][0]["goal"] = "Attest only the first command, not the controller-only second. 雪é"
        (self.repo / "checks" / "é space").mkdir(parents=True)
        self.gate_script = self.repo / "gate-check"
        self.gate_script.write_text("#!/bin/sh\nexit 0\n")
        self.gate_script.chmod(0o755)
        plan["declared_gates"][1]["commands"] = [
            {"argv": ["python3", "-c", "print('雪')", 'quoted"\\é'],
             "cwd": "checks/é space", "timeout_seconds": 899},
            {"argv": ["python3", "-c", "print('controller')"], "cwd": ".", "timeout_seconds": 17},
            {"argv": ["./gate-check"], "cwd": ".", "timeout_seconds": 10},
        ]

    def setUp(self):
        super().setUp()
        self.coordinator()

    def fingerprint(self):
        return {str(p.relative_to(self.root)): (
            p.lstat().st_mode, p.lstat().st_mtime_ns, p.lstat().st_ctime_ns,
            p.read_bytes() if p.is_file() else None,
        ) for p in self.root.rglob("*")}

    def invoke(self, *, node="implementation-a", request=None, extra=(), iid=None):
        args = ["initiative", "assignment-preview", iid or self.initiative_id,
                "--node", node, "--json"]
        if request:
            args += ["--salvage-request", request]
        out, err = io.StringIO(), io.StringIO()
        before = self.fingerprint()
        real_open = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC), path)
            return real_open(path, flags, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "TmuxAdapter", return_value=self.tmux))
            stack.enter_context(mock.patch("os.open", side_effect=readonly))
            for name in ("mkdir", "unlink", "rename", "replace", "fchmod"):
                stack.enter_context(mock.patch("os." + name, side_effect=AssertionError("write forbidden")))
            stack.enter_context(mock.patch.object(InitiativeStore, "transaction_lock", side_effect=AssertionError("lock forbidden")))
            stack.enter_context(mock.patch.object(InitiativeStore, "_locked_fds", side_effect=AssertionError("locking reader forbidden")))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            code = cli.main(args + list(extra), env=self.env)
        self.assertEqual(self.fingerprint(), before)
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def success(self, **kwargs):
        code, value, error = self.invoke(**kwargs)
        self.assertEqual(code, 0, error)
        self.assertIsNotNone(value)
        raw = value["rendering"]["text"].encode("utf-8")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), value["rendering"]["sha256"])
        self.assertEqual(len(raw), value["capacity"]["assignment_bytes"])
        self.assertLessEqual(len(preview.encode_preview(value)), preview.MAX_PREVIEW_BYTES)
        return value

    def test_ordinary_exact_commands_subset_and_production_renderer(self):
        value = self.success()
        self.assertEqual(value["controller_gates"][0]["commands"], self.plan["declared_gates"][1]["commands"])
        self.assertIsNone(value["worker_attestations"]["commands"])
        self.assertIn("first command", value["worker_attestations"]["goal"])
        self.assertIn("not all controller", value["worker_attestations"]["selection"])
        node = self.store.read_node(self.initiative_id, "implementation-a")
        expected = scheduler.assignment_bytes(self.initiative(), self.plan, node,
            {"attempt_id": preview.PREVIEW_ATTEMPT_ID, "base": value["base"]}, value["exact_base_commit"])
        self.assertEqual(expected.decode(), value["rendering"]["text"])
        self.assertEqual(value["coordinator"]["generation"], self.coordinator()["generation"])
        self.assertEqual(value["coordinator"]["liveness"], "live")
        self.assertIsNone(value["approval"])

    def test_public_preview_uses_selected_sqlite_store_without_artifact_writes(self):
        # Load fixture records to exercise the public constructor. Production
        # activation/quiescence is tested separately by the migration suite.
        sql = SQLiteInitiativeStore(self.config)
        with ControlDatabase(self.config.control, create=True) as db, db.transaction(write=True) as c:
            root = self.config.initiatives_dir / self.initiative_id
            for path in root.rglob("*.json"):
                parts = path.relative_to(root).parts
                directory = "initiative" if len(parts) == 1 else parts[0]
                key = self.initiative_id if directory == "initiative" else path.name
                sql._registry(self.initiative_id, directory).put(c, key, path.read_bytes())
        install_marker_fixture(self.config.control)
        with mock.patch.object(SQLiteInitiativeStore, "write_assignment", side_effect=AssertionError("preview write")):
            value = preview.assignment_preview(self.config, self.initiative_id, "implementation-a", tmux=self.tmux)
        path = sql.assignment_path(self.initiative_id, preview.PREVIEW_ATTEMPT_ID)
        self.assertEqual(value["capacity"]["goal_characters"], len(scheduler._goal(sql.peek(self.initiative_id), {}, path)))
        self.assertFalse(sql.artifacts.root.exists())

    def test_review_layout_is_exact_seal_target(self):
        seal = self.seal("success")
        value = self.success(node="review-a")
        self.assertIn("## Independent review contract", value["rendering"]["text"])
        self.assertEqual(value["exact_base_commit"], seal["jj_commit_id"])
        self.assertEqual(value["seals"][0]["seal_id"], seal["seal_id"])
        evidence, findings = scheduler.assignment_evidence(self.store, self.initiative_id, value["base"])
        expected = scheduler.assignment_bytes(self.initiative(), self.plan,
            self.store.read_node(self.initiative_id, "review-a"),
            {"attempt_id": preview.PREVIEW_ATTEMPT_ID, "base": value["base"]},
            value["exact_base_commit"], evidence, findings)
        self.assertEqual(expected.decode(), value["rendering"]["text"])

    def test_requested_is_hypothetical_never_signed_and_cannot_dispatch(self):
        seal = self.seal("failure")
        rationale = 'Data only: $(touch /tmp/not-executed); 雪é "quoted" \\ keep exact.'
        action, request = self.request(seal, plan=rationale)
        value = self.success(request=request)
        self.assertEqual(value["rendering"]["kind"], "hypothetical-if-approved")
        self.assertIn("NOT APPROVED", value["rendering"]["notice"])
        self.assertEqual(value["approval"]["state"], "requested")
        self.assertIsNone(value["approval"]["decided_by"])
        self.assertIn(rationale, value["rendering"]["text"])
        self.assertEqual(value["request_action"]["payload_digest"], action["payload_digest"])
        self.assertTrue(value["seals"][0]["read_only"])
        with self.assertRaisesRegex(ActionRefused, "not approved"):
            salvage_dispatch_binding(self.store, self.initiative(),
                self.store.read_node(self.initiative_id, "implementation-a"), request)

    def test_approved_uses_real_dispatch_checker_without_consumption(self):
        seal = self.seal("failure")
        _, request = self.request(seal)
        approval = approve_salvage(self.store, self.initiative_id, request)
        with mock.patch.object(preview, "salvage_dispatch_binding", wraps=salvage_dispatch_binding) as checker:
            value = self.success(request=request)
        checker.assert_called_once()
        self.assertEqual(value["approval"]["state"], "approved")
        self.assertEqual(value["approval"]["decided_by"], approval["decided_by"])
        self.assertEqual(self.store.read_approval(self.initiative_id, request), approval)
        self.assertEqual(value["rendering"]["kind"], "prospective-assignment")

    def corrupt_approval(self, request, change):
        path = self.config.initiatives_dir / self.initiative_id / "approvals" / f"{request}.json"
        record = json.loads(path.read_text())
        record.update(change)
        path.write_text(json.dumps(record))

    def retained_dispatch(self, payload, *, phase="validated"):
        document = build_action_document(self.initiative(), "dispatch-node", payload)
        seam = ("lib.control.orchestration.actions.append_event" if phase == "received"
                else "lib.control.orchestration.scheduler.dispatch")
        with mock.patch(seam, side_effect=salvage_tests.SimulatedDeath):
            with self.assertRaises(salvage_tests.SimulatedDeath):
                submit_action(self.store, self.initiative_id, document)
        action = self.store.read_action(self.initiative_id, document["action_id"])
        self.assertEqual(action["state"], phase)
        self.assertEqual(action_outcome(action)["payload"], payload)
        self.assertEqual(self.store.list_attempts_snapshot(self.initiative_id), [])
        return action

    def test_validated_salvage_intent_refuses_before_attempt_allocation(self):
        _, request = self.request(self.seal("failure"))
        approve_salvage(self.store, self.initiative_id, request)
        self.retained_dispatch({"node_id": "implementation-a", "salvage_request_id": request})
        code, _, error = self.invoke(request=request)
        self.assertEqual(code, 2, error)
        self.assertIn("dispatch", error)

    def assert_pending_intent_refused(self, node, phase):
        if node == "review-a":
            self.seal("success")
        self.retained_dispatch({"node_id": node}, phase=phase)
        code, _, error = self.invoke(node=node)
        self.assertEqual(code, 2, error)
        self.assertIn("dispatch", error)

    def test_ordinary_received_intent_refuses_without_attempt(self):
        self.assert_pending_intent_refused("implementation-a", "received")

    def test_ordinary_validated_intent_refuses_without_attempt(self):
        self.assert_pending_intent_refused("implementation-a", "validated")

    def test_review_received_intent_refuses_without_attempt(self):
        self.assert_pending_intent_refused("review-a", "received")

    def test_review_validated_intent_refuses_without_attempt(self):
        self.assert_pending_intent_refused("review-a", "validated")

    def test_ordinary_and_review_unrelated_or_refused_intents_do_not_block(self):
        self.seal("success")
        action = self.retained_dispatch({"node_id": "review-a"})
        path = self.config.initiatives_dir / self.initiative_id / "actions" / (action["action_id"] + ".json")
        for node, other in (("implementation-a", "review-a"), ("review-a", "implementation-a")):
            for state, target in (("received", other), ("validated", other), ("refused", node)):
                with self.subTest(node=node, state=state):
                    path.write_text(json.dumps({**action, "state": state,
                        "outcome": json.dumps({"payload": {"node_id": target}})}))
                    self.success(node=node)

    def test_ordinary_and_review_ambiguous_intents_refuse(self):
        self.seal("success")
        action = self.retained_dispatch({"node_id": "review-a"})
        path = self.config.initiatives_dir / self.initiative_id / "actions" / (action["action_id"] + ".json")
        for node in ("implementation-a", "review-a"):
            for outcome in ({"payload": {}}, {"payload": []},
                            {"node_id": "review-a", "payload": {"node_id": "implementation-a"}}):
                with self.subTest(node=node, outcome=outcome):
                    path.write_text(json.dumps({**action, "outcome": json.dumps(outcome)}))
                    code, _, error = self.invoke(node=node)
                    self.assertEqual(code, 2, error)
                    self.assertIn("ambiguous", error)

    def test_ordinary_and_review_revalidate_action_snapshot(self):
        self.seal("success")
        document = build_action_document(self.initiative(), "dispatch-node", {"node_id": "implementation-a"})
        with mock.patch.object(scheduler, "dispatch", side_effect=scheduler.SchedulerError("not dispatched")):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        real = preview._Snapshot._load
        for node in ("implementation-a", "review-a"):
            calls = 0
            def changed_actions(snapshot, key):
                nonlocal calls
                value = real(snapshot, key)
                if key == "actions":
                    calls += 1
                    if calls > 1:
                        return value + [value[0]]
                return value
            with self.subTest(node=node), mock.patch.object(preview._Snapshot, "_load", new=changed_actions):
                code, _, error = self.invoke(node=node)
                self.assertEqual(code, 2, error)
                self.assertIn("snapshot changed", error)

    def test_digest_bound_denied_wrapper_gate_refuses(self):
        self.seal("success")
        retained = self.store.read_plan(self.initiative_id, self.plan["revision"])
        retained["declared_gates"][1]["commands"][0]["argv"] = ["bash", "tests/test-seat.sh"]
        retained["digest"] = model.plan_digest(retained)
        directory = self.config.initiatives_dir / self.initiative_id
        (directory / "plans" / "0001.json").write_text(json.dumps(retained))
        initiative = self.initiative()
        changed = json.loads(json.dumps(initiative))
        changed["active_plan"]["digest"] = retained["digest"]
        changed["state_revision"] += 1
        self.store.save_initiative(changed, expected_digest=model.record_digest(initiative))
        with self.assertRaisesRegex(verification.GatePreflightError, "denied"):
            verification.preflight_verification_gates(retained, changed)
        for node in ("implementation-a", "review-a"):
            with self.subTest(node=node):
                code, _, error = self.invoke(node=node)
                self.assertEqual(code, 2, error)
                self.assertIn("denied", error)

    def test_approved_direct_gate_losing_shebang_refuses_ordinary_and_review(self):
        self.seal("success")
        for node in ("implementation-a", "review-a"):
            self.success(node=node)
        self.gate_script.write_text("exit 0\n")
        with self.assertRaisesRegex(verification.GatePreflightError, "shebang"):
            verification.preflight_verification_gates(self.plan, self.initiative())
        for node in ("implementation-a", "review-a"):
            with self.subTest(node=node):
                code, _, error = self.invoke(node=node)
                self.assertEqual(code, 2, error)
                self.assertIn("shebang", error)

    def test_unrunnable_gate_refuses_requested_and_approved_salvage(self):
        _, request = self.request(self.seal("failure"))
        for approved in (False, True):
            with self.subTest(approved=approved):
                self.gate_script.write_text("#!/bin/sh\nexit 0\n")
                if approved:
                    approve_salvage(self.store, self.initiative_id, request)
                self.success(request=request)
                self.gate_script.write_text("exit 0\n")
                code, _, error = self.invoke(request=request)
                self.assertEqual(code, 2, error)
                self.assertIn("shebang", error)

    def test_gate_preflight_stops_at_aggregate_preview_deadline(self):
        with mock.patch.object(preview.time, "monotonic", return_value=0) as clock:
            def checked_then_expired(*args, **kwargs):
                result = verification.preflight_verification_gates(*args, **kwargs)
                clock.return_value = preview.DEADLINE_SECONDS + 1
                return result
            with mock.patch.object(preview, "preflight_verification_gates",
                                   side_effect=checked_then_expired) as check:
                code, _, error = self.invoke()
                self.assertEqual(code, 2, error)
                self.assertIn("deadline", error)
                check.assert_called_once()

    def test_received_salvage_intent_refuses_before_validation(self):
        _, request = self.request(self.seal("failure"))
        approve_salvage(self.store, self.initiative_id, request)
        self.retained_dispatch({"node_id": "implementation-a", "salvage_request_id": request},
                               phase="received")
        code, _, error = self.invoke(request=request)
        self.assertEqual(code, 2, error)
        self.assertIn("dispatch", error)

    def test_competing_and_ambiguous_nonterminal_dispatch_intents_refuse(self):
        _, request = self.request(self.seal("failure"))
        approve_salvage(self.store, self.initiative_id, request)
        action = self.retained_dispatch({"node_id": "implementation-a"})
        self.retry_reservation()
        path = self.config.initiatives_dir / self.initiative_id / "actions" / (action["action_id"] + ".json")
        for state, outcome in (
            ("validated", {"payload": {"node_id": "implementation-a"}}),
            ("dispatching", {"node_id": "implementation-a", "salvage_request_id": request}),
            ("indeterminate", {"payload": {"node_id": "implementation-a"}}),
            ("received", {"payload": {}}),
            ("validated", {"payload": []}),
            ("received", {"node_id": "review-a"}),
            ("validated", {"node_id": "review-a", "payload": {"node_id": "implementation-a"}}),
            ("validated", {"payload": {"node_id": "review-a", "salvage_request_id": request}}),
        ):
            with self.subTest(state=state, outcome=outcome):
                path.write_text(json.dumps({**action, "state": state, "outcome": json.dumps(outcome)}))
                code, _, error = self.invoke(request=request)
                self.assertEqual(code, 2, error)
                self.assertIn("dispatch", error)

    def test_other_node_intent_and_refused_unbound_intent_do_not_block_preview(self):
        _, request = self.request(self.seal("failure"))
        approve_salvage(self.store, self.initiative_id, request)
        self.retained_dispatch({"node_id": "review-a"})
        self.success(request=request)
        document = build_action_document(self.initiative(), "dispatch-node", {
            "node_id": "implementation-a", "salvage_request_id": request,
        })
        with mock.patch.object(scheduler, "dispatch", side_effect=scheduler.SchedulerError("not dispatched")):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.success(request=request)

    def test_salvage_preview_supersedes_only_one_unbound_retry_without_consumption(self):
        _, request = self.request(self.seal("failure"))
        reservation = self.retry_reservation()
        for approved in (False, True):
            with self.subTest(approved=approved):
                if approved:
                    approve_salvage(self.store, self.initiative_id, request)
                value = self.success(request=request)
                self.assertEqual(value["approval"]["state"], "approved" if approved else "requested")
                self.assertIn("supersede", value["rendering"]["notice"])
                self.assertEqual(self.store.read_attempt(self.initiative_id, reservation["attempt_id"]), reservation)
                approval, base, seal = (preview.salvage_dispatch_binding if approved else
                    preview.salvage_request_binding)(self.store, self.initiative(),
                        self.store.read_node(self.initiative_id, "implementation-a"), request)
                evidence, findings = scheduler.assignment_evidence(self.store, self.initiative_id, base)
                expected = scheduler.assignment_bytes(self.initiative(), self.plan,
                    self.store.read_node(self.initiative_id, "implementation-a"),
                    {"attempt_id": preview.PREVIEW_ATTEMPT_ID, "base": base},
                    value["exact_base_commit"], evidence, findings,
                    salvage_recovery=scheduler.salvage_assignment_context(approval, seal))
                self.assertEqual(expected.decode(), value["rendering"]["text"])
        code, _, error = self.invoke()
        self.assertEqual(code, 2, error)
        self.assertIn("reserved/live/indeterminate", error)
        self.retry_reservation()
        code, _, error = self.invoke(request=request)
        self.assertEqual(code, 2, error)
        self.assertIn("multiple allocated", error)

    def test_salvage_cannot_substitute_a_bound_retry_reservation(self):
        import uuid
        _, request = self.request(self.seal("failure"))
        approve_salvage(self.store, self.initiative_id, request)
        self.retry_reservation(action_id=str(uuid.uuid4()))
        code, _, error = self.invoke(request=request)
        self.assertEqual(code, 2, error)
        self.assertIn("reserved/live/indeterminate", error)

    def test_refuses_expired_foreign_forged_stale_and_unsigned_bindings(self):
        _, request = self.request(self.seal("failure"))
        path = self.config.initiatives_dir / self.initiative_id / "approvals" / f"{request}.json"
        original = path.read_bytes()
        for change, match in (({"created_at": "2000-01-01T00:00:00Z",
                               "expires_at": "2001-01-01T00:00:00Z"}, "expired"),
                              ({"binding_digest": "0" * 64}, "binding changed"),
                              ({"active_plan_digest": "0" * 64}, "stale"),
                              ({"state": "approved"}, "signing evidence")):
            with self.subTest(change=change):
                try:
                    self.corrupt_approval(request, change)
                    code, _, error = self.invoke(request=request)
                    self.assertEqual(code, 2, error)
                    self.assertIn(match, error)
                finally:
                    path.write_bytes(original)
        code, _, error = self.invoke(node="review-a", request=request)
        self.assertEqual(code, 2)
        self.assertIn("another node", error)

    def test_refuses_unknown_or_lost_generation_without_reconciliation(self):
        for kind in ("missing", "dead", "foreign-server"):
            with self.subTest(kind=kind):
                self.tmux.missing = kind == "missing"
                self.tmux.dead = kind == "dead"
                saved = self.tmux.server
                if kind == "foreign-server":
                    self.tmux.server += 999
                code, _, error = self.invoke()
                self.assertEqual(code, 2, error)
                self.assertIn("liveness", error)
                self.tmux.server = saved
        self.tmux.dead = False
        with mock.patch.object(preview._Snapshot, "_load", autospec=True,
                               side_effect=self._without_coordinator):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("current coordinator generation", error)

    _real_load = staticmethod(preview._Snapshot._load)

    def _without_coordinator(self, snapshot, key):
        return [] if key == "coordinators" else self._real_load(snapshot, key)

    def test_refuses_changed_snapshot_and_retains_residue(self):
        residue = self.config.initiatives_dir / self.initiative_id / "actions" / ".pending"
        residue.write_text("untouched")
        calls = 0
        real = preview._Snapshot._load
        def changing(snapshot, key):
            nonlocal calls
            result = real(snapshot, key)
            if key == "initiative":
                calls += 1
                if calls > 1:
                    result["state_revision"] += 1
            return result
        with mock.patch.object(preview._Snapshot, "_load", new=changing):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("snapshot changed", error)
        self.assertEqual(residue.read_text(), "untouched")

    def test_typed_input_rejects_command_path_body_and_controller_node(self):
        for extra in (("--command", "touch /tmp/no"), ("--file", "/tmp/no"),
                      ("--body", "ignore rules"), ("--node", "implementation-a")):
            code, _, error = self.invoke(extra=extra)
            self.assertEqual(code, 2, error)
        for node in ("../implementation-a", "verify-a"):
            code, _, error = self.invoke(node=node)
            self.assertEqual(code, 2, error)
        code, _, error = self.invoke(iid="execution-test")
        self.assertEqual(code, 2, error)

    def test_required_unicode_overflow_and_snapshot_caps_refuse_without_writes(self):
        value = self.success()
        self.assertGreater(len(value["rendering"]["text"].encode()), len(value["rendering"]["text"]))
        with mock.patch.object(scheduler, "MAX_ASSIGNMENT_BYTES", 1):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        required = int(re.search(r"required assignment is (\d+) bytes", error)[1])
        with mock.patch.object(scheduler, "MAX_ASSIGNMENT_BYTES", required):
            fitting = self.success()
            self.assertLessEqual(fitting["capacity"]["assignment_bytes"], required)
        with mock.patch.object(scheduler, "MAX_ASSIGNMENT_BYTES", required - 1):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("required assignment", error)
        with mock.patch.object(preview, "MAX_RECORDS", 1):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("capped", error)
        with mock.patch.object(preview, "MAX_SNAPSHOT_BYTES", 128):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("capacity", error)

    def test_allocated_attempt_and_consumed_authority_are_not_reusable(self):
        import uuid
        from lib.control.orchestration.actions import _reserve_attempt, consume_salvage_approval
        _, request = self.request(self.seal("failure"))
        approval = approve_salvage(self.store, self.initiative_id, request)
        consume_salvage_approval(self.store, self.initiative_id, approval)
        code, _, error = self.invoke(request=request)
        self.assertEqual(code, 2)
        self.assertIn("consumed", error)
        node = self.store.read_node(self.initiative_id, "implementation-a")
        _reserve_attempt(self.store, self.initiative(), self.plan, node,
                         str(uuid.uuid4()), node["base"])
        code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("reserved/live/indeterminate", error)

    def test_unavailable_native_probe_is_bounded_refusal_not_consent(self):
        import subprocess
        from lib.control.tmux import TmuxAdapter
        calls = []
        def unavailable(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 1, b"", b"permission denied")
        self.tmux = TmuxAdapter(runner=unavailable)
        code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("liveness unknown", error)
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0][1]["timeout"], 2.0)

    def test_generation_change_during_final_live_probe_is_detected(self):
        real = preview.anchor_liveness
        calls = 0
        changed = False
        real_load = preview._Snapshot._load
        def live_then_change(anchor, tmux):
            nonlocal calls, changed
            state = real(anchor, tmux)
            calls += 1
            changed = calls == 2
            return state
        def changed_record(snapshot, key):
            value = real_load(snapshot, key)
            if key == "coordinators" and changed:
                value[0]["generation"] += 1
            return value
        with mock.patch.object(preview, "anchor_liveness", side_effect=live_then_change), \
                mock.patch.object(preview._Snapshot, "_load", new=changed_record):
            code, _, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("snapshot changed", error)
