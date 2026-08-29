from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

from lib.control.jj import ImmutableTree, WorkspaceIdentity
from lib.control.orchestration import model
from lib.control.orchestration.cli import main as orchestration_main
from lib.control.orchestration.actions import (
    ActionRefused,
    _continue_node,
    _parse_document,
    action_outcome,
    append_event,
    approve_salvage,
    build_action_document,
    consume_salvage_approval,
    reconcile_actions,
    salvage_dispatch_binding,
    submit_action,
)
from lib.control.orchestration.model import (
    ATTEMPT_CONTRACT,
    record_digest,
    validate_attempt,
    validate_result,
    validate_seal,
)
from lib.control.orchestration.reconcile import _failure_target
from lib.control.orchestration.seals import prepare_and_publish_seal
from lib.control.orchestration.scheduler import _exact_base
from lib.control.orchestration.scheduler import readiness
from lib.control.orchestration.store import InitiativeStore, ObservationOnlyPlanError
from lib.control.store import StoreError
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.test_orchestration_actions import CoordinatorEnvelope
from tests.python.test_orchestration_seals import SealJj, _entry
from tests.python.test_orchestration_store import contract_record


class SimulatedDeath(BaseException):
    pass


class OrchestrationRecoveryActionTests(
    CoordinatorEnvelope, ExecutionFixture, unittest.TestCase
):
    def seal(self, outcome: str, *, node_id: str = "implementation-a") -> dict:
        node = self.store.read_node(self.initiative_id, node_id)
        result_id = str(uuid.uuid4()) if outcome in {"success", "paused"} else None
        seal = validate_seal({
            "contract": model.SEAL_CONTRACT,
            "seal_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node_id,
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "outcome": outcome,
            "repository_id": node["repository_id"],
            "scope_origin": copy.deepcopy(self.plan["nodes"][0]["base"]["scope_origin"]),
            "base": {
                "kind": "repository-baseline",
                "jj_commit_id": self.plan["nodes"][0]["base"]["scope_origin"]["jj_commit_id"],
                "tree_digest": self.plan["nodes"][0]["base"]["scope_origin"]["tree_digest"],
                "seal_ids": [],
            },
            "read_only_failure_seal_ids": [],
            "jj_commit_id": "d" * 40,
            "tree_digest": "e" * 64,
            "diff_digest": "f" * 64,
            "cumulative_diff_digest": "1" * 64,
            "changed_paths": ["lib/control/orchestration/partial.py"],
            "changed_paths_truncated": 0,
            "changed_paths_digest": hashlib.sha256(json.dumps(
                ["lib/control/orchestration/partial.py"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            "cumulative_changed_paths": ["lib/control/orchestration/partial.py"],
            "cumulative_changed_paths_truncated": 0,
            "cumulative_changed_paths_digest": hashlib.sha256(json.dumps(
                ["lib/control/orchestration/partial.py"], ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            "result_id": result_id,
            "process_evidence_id": str(uuid.uuid4()),
            "sealed_at": now_text(),
        })
        if result_id is not None:
            self.store.save_result(self.initiative_id, validate_result({
                "contract": model.RESULT_CONTRACT,
                "publication_id": str(uuid.uuid4()),
                "result_id": result_id,
                "payload_digest": "a" * 64,
                "supersedes_result_id": None,
                "initiative_id": self.initiative_id,
                "node_id": node_id,
                "attempt_id": seal["attempt_id"],
                "task_id": seal["task_id"],
                "run_id": seal["run_id"],
                "claim_status": "completed" if outcome == "success" else "blocked",
                "summary": "retained candidate",
                "files_changed": list(seal["changed_paths"]),
                "verification_attestations": [],
                "concerns": [],
                "follow_up": [],
                "published_at": now_text(),
            }))
        self.store.save_seal(self.initiative_id, seal)
        return seal

    def request(self, seal: dict, *, node_id: str = "implementation-a"):
        action = build_action_document(
            self.initiative(), "request-salvage", {
                "node_id": node_id,
                "failure_seal_id": seal["seal_id"],
                "plan": "Recover the bounded useful work without inheriting its base.",
            },
        )
        stored = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(stored["state"], "completed", stored["outcome"])
        return stored, action_outcome(stored)["request_id"]

    def needs_input(self, *, node_id: str = "implementation-a") -> dict:
        node = self.store.read_node(self.initiative_id, node_id)
        if node["state"] == "blocked":
            ready = copy.deepcopy(node)
            ready["state"] = "ready"
            self.store.save_node(
                self.initiative_id, ready, expected_digest=record_digest(node),
            )
            node = ready
        changed = copy.deepcopy(node)
        changed["state"] = "needs-input"
        self.store.save_node(
            self.initiative_id, changed, expected_digest=record_digest(node),
        )
        return changed

    def approved_salvage(self, *, node_id: str = "implementation-a"):
        failure = self.seal("failure", node_id=node_id)
        _, request_id = self.request(failure, node_id=node_id)
        approval = approve_salvage(self.store, self.initiative_id, request_id)
        return failure, request_id, approval

    def control_start(self, calls: list[list[str]]):
        def capture(argv, **_kwargs):
            calls.append(argv)
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            return 0, json.dumps(payload).encode(), b""

        return capture

    def salvage_release_events(self, request_id: str) -> list[dict]:
        return [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "node-state-changed"
            and event["payload"].get("salvage_request_id") == request_id
        ]

    def approval_event(self, event_type: str, request_id: str) -> dict:
        return next(
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == event_type and request_id in event["subject_ids"]
        )

    def coordinator_request(self, seal: dict, *, node_id: str = "implementation-a"):
        document = self.coordinator_document("request-salvage", {
            "node_id": node_id,
            "failure_seal_id": seal["seal_id"],
            "plan": "Recover the bounded useful work without inheriting its base.",
        })
        stored = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(stored["state"], "completed", stored["outcome"])
        return document, action_outcome(stored)["request_id"]

    def test_coordinator_salvage_request_records_the_coordinator_as_requester(self) -> None:
        failure = self.seal("failure")
        document, request_id = self.coordinator_request(failure)
        requested = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(
            model.approval_requester(requested),
            {"actor_id": document["actor_id"], "actor_kind": "coordinator"},
        )
        self.assertIsNone(model.approval_decider(requested))
        # The mirrored event-side conflation: the request is the coordinator's
        # own act and must not be journalled as an operator-kind write.
        requested_event = self.approval_event("approval-requested", request_id)
        self.assertEqual(requested_event["actor_kind"], "coordinator")
        self.assertEqual(requested_event["actor_id"], document["actor_id"])

        approved = approve_salvage(self.store, self.initiative_id, request_id)
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(
            model.approval_requester(approved), model.approval_requester(requested),
            "the decision must not rewrite who asked",
        )
        decided_event = self.approval_event("approval-decided", request_id)
        self.assertEqual(
            (decided_event["actor_kind"], decided_event["actor_id"]), ("operator", "cli"),
        )

    def test_operator_salvage_request_records_the_operator_as_requester(self) -> None:
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        requested = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(
            model.approval_requester(requested),
            {"actor_id": "cli", "actor_kind": "operator"},
        )
        self.assertEqual(
            self.approval_event("approval-requested", request_id)["actor_kind"], "operator",
        )

    def test_approve_salvage_refuses_a_signer_that_is_not_an_operator_surface(self) -> None:
        failure = self.seal("failure")
        document, request_id = self.coordinator_request(failure)
        # Standing authorities never cover salvage, so only the operator's own
        # surface signs one; the requesting pane least of all.
        for signer in (document["actor_id"], "standing-authority:74a1f315", "scheduler"):
            with self.subTest(signer=signer):
                with self.assertRaisesRegex(ActionRefused, "operator surface"):
                    approve_salvage(
                        self.store, self.initiative_id, request_id, actor_id=signer,
                    )
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"], "requested",
        )
        self.assertEqual(
            [event for event in self.store.list_events_snapshot(self.initiative_id)
             if event["type"] == "approval-decided"],
            [],
        )
        approve_salvage(
            self.store, self.initiative_id, request_id, actor_id="tui",
        )
        self.assertEqual(
            self.approval_event("approval-decided", request_id)["actor_id"], "tui",
        )

    def test_pre_split_approval_record_still_reads_and_still_approves(self) -> None:
        """Records written before the provenance split stay readable and signable."""
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        stored = self.store.read_approval(self.initiative_id, request_id)
        historical = copy.deepcopy(stored)
        historical.pop("requested_by")
        self.store.save_approval(
            self.initiative_id, historical, expected_digest=record_digest(stored),
        )
        reread = self.store.read_approval(self.initiative_id, request_id)
        self.assertNotIn("requested_by", reread)
        self.assertIsNone(model.approval_requester(reread))
        approved = approve_salvage(self.store, self.initiative_id, request_id)
        self.assertEqual(approved["state"], "approved")
        self.assertNotIn("requested_by", approved)
        self.assertEqual(
            self.approval_event("approval-decided", request_id)["actor_id"], "cli",
        )

    def test_decision_records_the_signer_and_binds_it_once(self) -> None:
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        requested = self.store.read_approval(self.initiative_id, request_id)
        self.assertNotIn("decided_by", requested)

        approve_salvage(self.store, self.initiative_id, request_id)

        decided = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(
            decided["decided_by"], {"actor_id": "cli", "actor_kind": "operator"},
        )
        self.assertEqual(
            model.approval_decider(decided),
            {"actor_id": "cli", "actor_kind": "operator"},
        )
        # The requester stays the coordinator that asked; only the decider is
        # the operator surface that signed.
        self.assertEqual(
            model.approval_requester(decided)["actor_id"],
            requested["actor_id"],
        )

        rewritten = copy.deepcopy(decided)
        rewritten["decided_by"] = {"actor_id": "tui", "actor_kind": "operator"}
        with self.assertRaisesRegex(StoreError, "decided_by"):
            self.store.save_approval(
                self.initiative_id, rewritten,
                expected_digest=record_digest(decided),
            )

    def test_approved_salvage_releases_needs_input_and_consumes_once(self) -> None:
        _, request_id, _ = self.approved_salvage()
        self.needs_input()
        calls = []
        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.control_start(calls),
        ), mock.patch(
            "lib.control.orchestration.actions.consume_salvage_approval",
            wraps=consume_salvage_approval,
        ) as consume:
            result = submit_action(self.store, self.initiative_id, dispatch)

        self.assertEqual(result["state"], "completed", result["outcome"])
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"],
            "consumed",
        )
        consume.assert_called_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        events = self.salvage_release_events(request_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor_kind"], "controller")
        self.assertEqual(events[0]["actor_id"], "scheduler")
        self.assertEqual(events[0]["payload"], {
            "from": "needs-input", "to": "ready",
            "salvage_request_id": request_id,
        })

    def test_unapproved_salvage_does_not_release_needs_input(self) -> None:
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        self.needs_input()

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            }),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "salvage approval is not approved",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(self.salvage_release_events(request_id), [])

    def test_consumed_salvage_does_not_release_needs_input(self) -> None:
        _, request_id, approval = self.approved_salvage()
        consume_salvage_approval(self.store, self.initiative_id, approval)
        self.needs_input()

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            }),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "salvage approval was already consumed",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(self.salvage_release_events(request_id), [])

    def test_expired_salvage_does_not_release_needs_input(self) -> None:
        _, request_id, approval = self.approved_salvage()
        self.needs_input()

        expires = datetime.fromisoformat(approval["expires_at"][:-1] + "+00:00")

        class AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                value = expires + timedelta(microseconds=1)
                return value if tz is None else value.astimezone(tz)

        with mock.patch("lib.control.orchestration.actions.datetime", AfterExpiry):
            refused = submit_action(
                self.store, self.initiative_id,
                build_action_document(self.initiative(), "dispatch-node", {
                    "node_id": "implementation-a", "salvage_request_id": request_id,
                }),
            )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "salvage approval expired before dispatch",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(self.salvage_release_events(request_id), [])

    def test_wrong_node_salvage_does_not_release_needs_input(self) -> None:
        _, request_id, _ = self.approved_salvage(node_id="review-a")
        self.needs_input()

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            }),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"],
            "salvage approval cannot be substituted onto another node",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(self.salvage_release_events(request_id), [])

    def test_attempt_cap_refuses_salvage_without_releasing_needs_input(self) -> None:
        _, request_id, _ = self.approved_salvage()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        at = now_text()
        cap = min(
            self.initiative()["limits"]["max_attempts_per_node"],
            self.plan["limits"]["max_attempts_per_node"],
        )
        for ordinal in range(1, cap + 1):
            self.store.save_attempt(self.initiative_id, validate_attempt({
                "contract": ATTEMPT_CONTRACT,
                "attempt_id": str(uuid.uuid4()),
                "initiative_id": self.initiative_id,
                "node_id": node["node_id"],
                "task_id": str(uuid.uuid4()),
                "action_id": str(uuid.uuid4()),
                "ordinal": ordinal,
                "base": copy.deepcopy(self.plan["nodes"][0]["base"]),
                "state": "sealed-failure",
                "result_publication_id": None,
                "result_id": None,
                "seal_id": str(uuid.uuid4()),
                "created_at": at,
                "updated_at": at,
            }))
        self.needs_input()

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            }),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "node max_attempts_per_node exhausted",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"],
            "approved",
        )
        self.assertEqual(self.salvage_release_events(request_id), [])

    def test_same_dispatch_replay_after_consumption_returns_stored_outcome(self) -> None:
        _, request_id, _ = self.approved_salvage()
        self.needs_input()
        calls = []
        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.control_start(calls),
        ):
            first = submit_action(self.store, self.initiative_id, dispatch)
            replay = submit_action(self.store, self.initiative_id, dispatch)

        self.assertEqual(first, replay)
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(len(self.salvage_release_events(request_id)), 1)
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"],
            "consumed",
        )

    def test_plain_dispatch_does_not_release_needs_input(self) -> None:
        self.needs_input()

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            ),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "node is not deterministically ready",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )

    def test_resume_does_not_release_needs_input_node_without_salvage(self) -> None:
        self.needs_input()
        initiative = self.initiative()
        waiting = copy.deepcopy(initiative)
        waiting.update({
            "state": "needs-input",
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            waiting, expected_digest=record_digest(initiative),
        )

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            resumed = submit_action(
                self.store, self.initiative_id,
                build_action_document(self.initiative(), "resume", {}),
            )

        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )

    def test_dependency_regression_refuses_after_durable_salvage_release(self) -> None:
        _, request_id, _ = self.approved_salvage(node_id="review-a")
        self.needs_input(node_id="review-a")

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            refused = submit_action(
                self.store, self.initiative_id,
                build_action_document(self.initiative(), "dispatch-node", {
                    "node_id": "review-a", "salvage_request_id": request_id,
                }),
            )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"], "node is not deterministically ready",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"], "ready",
        )
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"],
            "approved",
        )
        self.assertEqual(len(self.salvage_release_events(request_id)), 1)

    def test_exact_seal_base_ignores_later_seals_from_the_same_node(self) -> None:
        exact = self.seal("success")
        later = copy.deepcopy(exact)
        later.update({
            "seal_id": str(uuid.uuid4()),
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "jj_commit_id": "a" * 40,
            "tree_digest": "2" * 64,
            "diff_digest": "3" * 64,
            "cumulative_diff_digest": "4" * 64,
            "result_id": str(uuid.uuid4()),
            "process_evidence_id": str(uuid.uuid4()),
            "sealed_at": now_text(),
        })
        self.store.save_seal(self.initiative_id, validate_seal(later))
        attempt = {
            "base": {
                "policy": "upstream-seal",
                "scope_origin": copy.deepcopy(exact["scope_origin"]),
                "upstream_node_ids": [exact["node_id"]],
                "seal_inputs": [{
                    "seal_id": exact["seal_id"], "outcome": "success",
                    "read_only": False,
                    "scope_origin": copy.deepcopy(exact["scope_origin"]),
                }],
            },
        }
        self.assertEqual(
            _exact_base(
                self.store, self.initiative_id,
                self.store.read_node(self.initiative_id, exact["node_id"]), attempt,
            ),
            exact["jj_commit_id"],
        )

    def test_request_approve_consume_replay_and_substitution_refusals(self) -> None:
        failure = self.seal("failure")
        request_action, request_id = self.request(failure)
        requested = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(requested["state"], "requested")
        binding = action_outcome(request_action)["salvage_binding"]
        self.assertEqual(binding["failure_seal_id"], failure["seal_id"])
        self.assertEqual(binding["scope_origin"], failure["scope_origin"])
        self.assertEqual(
            binding["hard_write_scope"],
            self.store.read_node(self.initiative_id, "implementation-a")["hard_write_scope"],
        )

        output = StringIO()
        with redirect_stdout(output):
            status = orchestration_main([
                "initiative", "approve-salvage", self.initiative_id,
                "--request", request_id, "--json",
            ], env=self.env)
        self.assertEqual(status, 0, output.getvalue())
        approved = json.loads(output.getvalue())["approval"]
        self.assertEqual(approved["state"], "approved")
        review = self.store.read_node(self.initiative_id, "review-a")
        with self.assertRaisesRegex(ActionRefused, "substituted"):
            salvage_dispatch_binding(self.store, self.initiative(), review, request_id)
        append_event(
            self.store, self.initiative_id, "node-state-changed", ["review-a"],
            {"from": "blocked", "to": "blocked"},
            actor_kind="controller", actor_id="scheduler",
        )

        calls = []

        def capture(argv, **_kwargs):
            calls.append(argv)
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            result = submit_action(self.store, self.initiative_id, dispatch)
        self.assertEqual(result["state"], "completed", result["outcome"])
        approval = self.store.read_approval(self.initiative_id, request_id)
        self.assertEqual(approval["state"], "consumed")
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(attempt["base"]["policy"], "scope-baseline")
        self.assertEqual(
            attempt["base"]["scope_origin"]["jj_commit_id"],
            failure["scope_origin"]["jj_commit_id"],
        )
        self.assertTrue(attempt["base"]["seal_inputs"][0]["read_only"])
        assignment = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempt['attempt_id']}.md"
        ).read_text()
        self.assertIn(failure["seal_id"], assignment)
        self.assertIn(failure["jj_commit_id"], assignment)
        self.assertIn("Read-only failure seal inputs", assignment)

        replay = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        refused = submit_action(self.store, self.initiative_id, replay)
        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            action_outcome(refused)["reason"],
            "salvage approval was already consumed",
        )
        self.assertEqual(len(calls), 1)

    def retry_reservation(self, *, action_id: str | None = None) -> dict:
        """The automatic retry's bookkeeping row, as the failure path leaves it."""
        node_attempts = [
            item for item in self.store.list_attempts_snapshot(self.initiative_id)
            if item["node_id"] == "implementation-a"
        ]
        reservation = model.validate_attempt({
            "contract": model.ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": "implementation-a",
            "task_id": str(uuid.uuid4()),
            "action_id": action_id,
            "ordinal": max((item["ordinal"] for item in node_attempts), default=0) + 1,
            "base": copy.deepcopy(self.plan["nodes"][0]["base"]),
            "state": "allocated",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": now_text(),
            "updated_at": now_text(),
        })
        self.store.save_attempt(self.initiative_id, reservation)
        return reservation

    def test_salvage_dispatch_supersedes_unbound_retry_reservation(self) -> None:
        failure, request_id, _approval = self.approved_salvage()
        reservation = self.retry_reservation()

        calls: list[list[str]] = []
        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.control_start(calls),
        ):
            result = submit_action(self.store, self.initiative_id, dispatch)
        self.assertEqual(result["state"], "completed", result["outcome"])

        retained = self.store.read_attempt(
            self.initiative_id, reservation["attempt_id"],
        )
        self.assertEqual(retained["state"], "cancelled")
        salvage_attempts = [
            item for item in self.store.list_attempts_snapshot(self.initiative_id)
            if item["base"]["policy"] == "scope-baseline"
        ]
        self.assertEqual(len(salvage_attempts), 1)
        self.assertEqual(
            salvage_attempts[0]["base"]["scope_origin"]["jj_commit_id"],
            failure["scope_origin"]["jj_commit_id"],
        )

    def test_salvage_dispatch_still_refuses_a_bound_reservation(self) -> None:
        _failure, request_id, _approval = self.approved_salvage()
        reservation = self.retry_reservation(action_id=str(uuid.uuid4()))

        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        refused = submit_action(self.store, self.initiative_id, dispatch)
        self.assertEqual(refused["state"], "refused")
        self.assertIn(
            "bound to a dispatch action", action_outcome(refused)["reason"],
        )
        retained = self.store.read_attempt(
            self.initiative_id, reservation["attempt_id"],
        )
        self.assertEqual(retained["state"], "allocated")

    def test_crash_after_consumption_reconciles_same_reserved_task_identity(self) -> None:
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        approve_salvage(self.store, self.initiative_id, request_id)
        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler._exact_base",
            side_effect=SimulatedDeath,
        ):
            with self.assertRaises(SimulatedDeath):
                submit_action(self.store, self.initiative_id, dispatch)
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(
            self.store.read_approval(self.initiative_id, request_id)["state"],
            "consumed",
        )
        calls = []

        def capture(argv, **_kwargs):
            calls.append(argv)
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            reconciled = reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--task-id") + 1], attempt["task_id"])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)

    def test_salvage_successor_cannot_launder_out_of_scope_failure_content(self) -> None:
        failure = self.seal("failure")
        _, request_id = self.request(failure)
        approve_salvage(self.store, self.initiative_id, request_id)

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            payload["task"]["jj"]["working_commit_id"] = "e" * 40
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {
                "node_id": "implementation-a", "salvage_request_id": request_id,
            },
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            submit_action(self.store, self.initiative_id, dispatch)
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        result = validate_result({
            "contract": model.RESULT_CONTRACT,
            "publication_id": str(uuid.uuid4()), "result_id": str(uuid.uuid4()),
            "payload_digest": "a" * 64, "supersedes_result_id": None,
            "initiative_id": self.initiative_id, "node_id": attempt["node_id"],
            "attempt_id": attempt["attempt_id"], "task_id": attempt["task_id"],
            "run_id": self.task["runs"][0]["run_id"], "claim_status": "completed",
            "summary": "candidate", "files_changed": ["docs/laundered.md"],
            "verification_attestations": [], "concerns": [], "follow_up": [],
            "published_at": now_text(),
        })
        self.store.save_result(self.initiative_id, result)
        reported = copy.deepcopy(attempt)
        reported.update({
            "state": "reported", "result_publication_id": result["publication_id"],
            "result_id": result["result_id"], "updated_at": now_text(),
        })
        self.store.save_attempt(
            self.initiative_id, reported, expected_digest=record_digest(attempt),
        )
        observed = {
            "state": "exited", "blocker": None, "evidence": [],
            "runs": [{
                "run_id": result["run_id"], "state": "exited", "blocker": None,
                "evidence": [
                    {"source": "tmux", "outcome": "missing", "detail": "tmux pane process exited with status 0", "state": None, "stale": False},
                    {"source": "jj", "outcome": "match", "detail": "match", "state": None, "stale": False},
                ],
            }],
        }
        jj = SealJj(
            self.task, failure["scope_origin"]["tree_digest"],
            (_entry("docs/laundered.md", "bad"),),
        )
        seal = prepare_and_publish_seal(
            self.store, self.initiative_id, attempt["attempt_id"], self.task,
            observed, jj=jj,
        )
        self.assertEqual(seal["outcome"], "failure")
        evidence = json.loads(self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )["summary"])
        self.assertEqual(evidence["scope_violations"], ["docs/laundered.md"])

    def test_repair_uses_success_seal_enforces_cap_and_stales_bound_records(self) -> None:
        success = self.seal("success")
        source = self.store.read_node(self.initiative_id, "implementation-a")
        evaluating = copy.deepcopy(source)
        evaluating["state"] = "evaluating"
        self.store.save_node(
            self.initiative_id, evaluating, expected_digest=record_digest(source),
        )
        succeeded = copy.deepcopy(evaluating)
        succeeded["state"] = "succeeded"
        self.store.save_node(
            self.initiative_id, succeeded, expected_digest=record_digest(evaluating),
        )
        target = self.store.read_node(self.initiative_id, "review-a")
        ready = copy.deepcopy(target)
        ready["state"] = "ready"
        self.store.save_node(
            self.initiative_id, ready, expected_digest=record_digest(target),
        )
        initiative = self.initiative()
        limited = copy.deepcopy(initiative)
        limited["limits"]["max_repair_cycles"] = 1
        limited.update({
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(limited, expected_digest=record_digest(initiative))

        review = contract_record(model.validate_review)
        review.update({
            "initiative_id": self.initiative_id,
            "node_id": "review-a", "state": "accepted-pass", "verdict": "pass",
            "findings": [],
        })
        review["target"].update({
            "seal_id": success["seal_id"],
            "repository_id": success["repository_id"],
            "jj_commit_id": success["jj_commit_id"],
            "base_seal_ids": success["base"]["seal_ids"],
            "diff_digest": success["diff_digest"],
            "active_plan_digest": self.plan["digest"],
        })
        self.store.save_review(self.initiative_id, review)
        verification = contract_record(model.validate_verification)
        verification.update({
            "initiative_id": self.initiative_id, "node_id": "verify-a",
            "active_plan_digest": self.plan["digest"], "state": "passed",
            "outcome": "passed", "seal_id": success["seal_id"],
            "repository_id": success["repository_id"],
        })
        self.store.save_verification(self.initiative_id, verification)
        bundle = contract_record(model.validate_bundle)
        bundle.update({
            "initiative_id": self.initiative_id,
            "active_plan_digest": self.plan["digest"],
            "state": "compatible", "outcome": "compatible",
        })
        bundle["members"][0].update({
            "repository_id": success["repository_id"],
            "seal_id": success["seal_id"],
            "jj_commit_id": success["jj_commit_id"],
            "tree_digest": success["tree_digest"],
            "diff_digest": success["diff_digest"],
            "review_id": review["review_id"],
            "verification_id": verification["verification_id"],
        })
        self.store.save_bundle(self.initiative_id, bundle)

        action = build_action_document(
            self.initiative(), "repair-node", {
                "node_id": "review-a", "seal_id": success["seal_id"],
            },
        )
        repaired = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(repaired["state"], "completed", repaired["outcome"])
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(attempt["base"]["seal_inputs"][0]["seal_id"], success["seal_id"])
        self.assertEqual(attempt["base"]["policy"], "upstream-seal")
        self.assertEqual(
            self.store.read_review(self.initiative_id, review["review_id"])["state"],
            "accepted-pass",
        )
        self.assertEqual(
            self.store.read_verification(
                self.initiative_id, verification["verification_id"],
            )["state"],
            "passed",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "succeeded",
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.assertEqual(readiness(self.store, self.initiative())["review-a"], "ready")

        verify = self.store.read_node(self.initiative_id, "verify-a")
        verify_ready = copy.deepcopy(verify)
        verify_ready["state"] = "ready"
        self.store.save_node(
            self.initiative_id, verify_ready, expected_digest=record_digest(verify),
        )
        capped = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "repair-node", {
                "node_id": "verify-a", "seal_id": success["seal_id"],
            },
        ))
        self.assertEqual(capped["state"], "refused")
        self.assertIn("max_repair_cycles", action_outcome(capped)["reason"])

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            payload["task"]["jj"]["working_commit_id"] = "e" * 40
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            dispatched = submit_action(
                self.store, self.initiative_id,
                build_action_document(
                    self.initiative(), "dispatch-node", {"node_id": "review-a"},
                ),
            )
        self.assertEqual(dispatched["state"], "completed", dispatched["outcome"])
        attempt = self.store.read_attempt(self.initiative_id, attempt["attempt_id"])
        result = validate_result({
            "contract": model.RESULT_CONTRACT,
            "publication_id": str(uuid.uuid4()),
            "result_id": str(uuid.uuid4()),
            "payload_digest": "b" * 64,
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": attempt["node_id"],
            "attempt_id": attempt["attempt_id"],
            "task_id": attempt["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": "completed",
            "summary": "repair complete",
            "files_changed": [],
            "verification_attestations": [],
            "concerns": [],
            "follow_up": [],
            "published_at": now_text(),
        })
        self.store.save_result(self.initiative_id, result)
        reported = copy.deepcopy(attempt)
        reported.update({
            "state": "reported",
            "result_publication_id": result["publication_id"],
            "result_id": result["result_id"],
            "updated_at": now_text(),
        })
        self.store.save_attempt(
            self.initiative_id, reported, expected_digest=record_digest(attempt),
        )

        repair_entries = ()

        class RepairJj:
            def inspect_workspace(inner_self, path, name, *, snapshot=False, require_empty=True):
                return WorkspaceIdentity(
                    name=name,
                    change_id=self.task["jj"]["change_id"],
                    commit_id=self.task["jj"]["working_commit_id"],
                    parent_commit_ids=(success["jj_commit_id"],),
                    description="repair",
                )

            def immutable_tree(inner_self, repository, commit_id):
                if commit_id == success["scope_origin"]["jj_commit_id"]:
                    return ImmutableTree(commit_id, success["scope_origin"]["tree_digest"], ())
                if commit_id == success["jj_commit_id"]:
                    return ImmutableTree(commit_id, success["tree_digest"], ())
                digest = __import__("hashlib").sha256(
                    json.dumps(repair_entries).encode()
                ).hexdigest()
                return ImmutableTree(commit_id, digest, repair_entries)

        observed = {
            "state": "exited", "blocker": None, "evidence": [],
            "runs": [{
                "run_id": self.task["runs"][0]["run_id"],
                "state": "exited", "blocker": None,
                "evidence": [
                    {"source": "process", "outcome": "missing", "detail": "process ended", "state": "exited", "stale": False},
                    {"source": "jj", "outcome": "match", "detail": "match", "state": None, "stale": False},
                ],
            }],
        }
        repair_seal = prepare_and_publish_seal(
            self.store, self.initiative_id, attempt["attempt_id"], self.task,
            observed, jj=RepairJj(),
        )
        self.assertEqual(repair_seal["outcome"], "success")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "superseded",
        )
        self.assertEqual(
            self.store.read_review(self.initiative_id, review["review_id"])["state"],
            "stale",
        )
        self.assertEqual(
            self.store.read_verification(
                self.initiative_id, verification["verification_id"],
            )["state"],
            "stale",
        )

    def test_generic_retry_resolves_original_plan_base_not_failed_inherited_base(self) -> None:
        inherited = self.seal("success")
        node = self.store.read_node(self.initiative_id, "implementation-a")
        at = now_text()
        failed = validate_attempt({
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node["node_id"],
            "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()),
            "ordinal": 1,
            "base": {
                "policy": "upstream-seal",
                "scope_origin": copy.deepcopy(inherited["scope_origin"]),
                "upstream_node_ids": [inherited["node_id"]],
                "seal_inputs": [{
                    "seal_id": inherited["seal_id"],
                    "outcome": "success", "read_only": False,
                    "scope_origin": copy.deepcopy(inherited["scope_origin"]),
                }],
            },
            "state": "sealed-failure",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": str(uuid.uuid4()),
            "created_at": at, "updated_at": at,
        })
        self.store.save_attempt(self.initiative_id, failed)
        for state in ("dispatching", "running"):
            changed = copy.deepcopy(node)
            changed["state"] = state
            self.store.save_node(
                self.initiative_id, changed, expected_digest=record_digest(node),
            )
            node = changed
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            _node, retry, reason = _failure_target(
                self.store, self.initiative_id, self.initiative(), self.plan,
                node, failed, [failed], datetime.now(timezone.utc),
            )
        self.assertIsNone(reason)
        self.assertIsNotNone(retry)
        self.assertEqual(retry["base"], self.plan["nodes"][0]["base"])
        self.assertNotEqual(retry["base"], failed["base"])

    def test_salvage_and_repair_failures_need_input_and_null_repair_retries_count(self) -> None:
        failure = self.seal("failure")
        node = self.store.read_node(self.initiative_id, "implementation-a")
        at = now_text()
        salvage = validate_attempt({
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node["node_id"],
            "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()),
            "ordinal": 1,
            "base": {
                "policy": "scope-baseline",
                "scope_origin": copy.deepcopy(failure["scope_origin"]),
                "upstream_node_ids": [],
                "seal_inputs": [{
                    "seal_id": failure["seal_id"], "outcome": "failure",
                    "read_only": True,
                    "scope_origin": copy.deepcopy(failure["scope_origin"]),
                }],
            },
            "state": "sealed-failure",
            "result_publication_id": None, "result_id": None,
            "seal_id": str(uuid.uuid4()),
            "created_at": at, "updated_at": at,
        })
        self.store.save_attempt(self.initiative_id, salvage)
        for state in ("dispatching", "running"):
            changed = copy.deepcopy(node)
            changed["state"] = state
            self.store.save_node(
                self.initiative_id, changed, expected_digest=record_digest(node),
            )
            node = changed
        current, retry, reason = _failure_target(
            self.store, self.initiative_id, self.initiative(), self.plan,
            node, salvage, [salvage], datetime.now(timezone.utc),
        )
        self.assertIsNone(retry)
        self.assertEqual(reason, "salvage-lineage-needs-input")
        self.assertEqual(current["state"], "needs-input")

        # A completed repair action is the lineage root. A retained historical
        # null-action retry with the same base must consume the second cycle.
        success = self.seal("success")
        source = self.store.read_node(self.initiative_id, "implementation-a")
        superseded = copy.deepcopy(source)
        superseded["state"] = "superseded"
        self.store.save_node(
            self.initiative_id, superseded, expected_digest=record_digest(source),
        )
        target = self.store.read_node(self.initiative_id, "review-a")
        ready = copy.deepcopy(target)
        ready["state"] = "ready"
        self.store.save_node(
            self.initiative_id, ready, expected_digest=record_digest(target),
        )
        repair_action = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "repair-node",
                {"node_id": "review-a", "seal_id": success["seal_id"]},
            ),
        )
        self.assertEqual(repair_action["state"], "completed", repair_action["outcome"])
        repair = next(
            item for item in self.store.list_attempts_snapshot(self.initiative_id)
            if item["attempt_id"] == action_outcome(repair_action)["attempt_id"]
        )
        current_attempt = repair
        for state in (
            "dispatching", "running", "abnormal-exit", "failure-seal-ready",
            "sealing", "sealed-failure",
        ):
            changed = copy.deepcopy(current_attempt)
            changed.update({"state": state, "updated_at": now_text()})
            if state in {"failure-seal-ready", "sealing", "sealed-failure"}:
                changed["seal_id"] = changed["seal_id"] or str(uuid.uuid4())
            self.store.save_attempt(
                self.initiative_id, changed,
                expected_digest=record_digest(current_attempt),
            )
            current_attempt = changed
        target = self.store.read_node(self.initiative_id, "review-a")
        for state in ("dispatching", "running"):
            changed = copy.deepcopy(target)
            changed["state"] = state
            self.store.save_node(
                self.initiative_id, changed, expected_digest=record_digest(target),
            )
            target = changed
        current, retry, reason = _failure_target(
            self.store, self.initiative_id, self.initiative(), self.plan,
            target, current_attempt, self.store.list_attempts_snapshot(self.initiative_id),
            datetime.now(timezone.utc),
        )
        self.assertIsNone(retry)
        self.assertEqual(reason, "repair-lineage-needs-input")
        self.assertEqual(current["state"], "needs-input")
        legacy = copy.deepcopy(repair)
        legacy.update({
            "attempt_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
            "action_id": None, "ordinal": repair["ordinal"] + 1,
            "state": "allocated", "seal_id": None,
            "created_at": now_text(), "updated_at": now_text(),
        })
        self.store.save_attempt(self.initiative_id, validate_attempt(legacy))
        verify = self.store.read_node(self.initiative_id, "verify-a")
        verify_ready = copy.deepcopy(verify)
        verify_ready["state"] = "ready"
        self.store.save_node(
            self.initiative_id, verify_ready, expected_digest=record_digest(verify),
        )
        capped = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "repair-node",
                {"node_id": "verify-a", "seal_id": success["seal_id"]},
            ),
        )
        self.assertEqual(capped["state"], "refused")
        self.assertIn("max_repair_cycles", action_outcome(capped)["reason"])

    def test_paused_seal_continuation_consumes_decision_once_and_attempt_budget(self) -> None:
        paused = self.seal("paused")
        node = self.store.read_node(self.initiative_id, "implementation-a")
        needs = copy.deepcopy(node)
        needs["state"] = "needs-input"
        self.store.save_node(
            self.initiative_id, needs, expected_digest=record_digest(node),
        )
        initiative = self.initiative()
        waiting = copy.deepcopy(initiative)
        waiting.update({
            "state": "needs-input", "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(waiting, expected_digest=record_digest(initiative))
        decision = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "decide", {
                "paused_seal_id": paused["seal_id"], "decision": "Continue with the retained candidate.",
            },
        ))
        continuation = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "continue-node", {
                "node_id": "implementation-a", "paused_seal_id": paused["seal_id"],
                "decision_action_id": decision["action_id"],
            },
        ))
        self.assertEqual(continuation["state"], "completed", continuation["outcome"])
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["base"]["seal_inputs"][0]["outcome"], "paused")
        self.assertEqual(self.initiative()["state"], "running")
        second = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "continue-node", {
                "node_id": "implementation-a", "paused_seal_id": paused["seal_id"],
                "decision_action_id": decision["action_id"],
            },
        ))
        self.assertEqual(second["state"], "refused")
        self.assertIn("already been continued", action_outcome(second)["reason"])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)

    def test_direct_continuation_retains_strict_historical_plan_defense(self) -> None:
        paused = self.seal("paused")
        node = self.store.read_node(self.initiative_id, "implementation-a")
        needs = copy.deepcopy(node)
        needs["state"] = "needs-input"
        self.store.save_node(
            self.initiative_id, needs, expected_digest=record_digest(node),
        )
        initiative = self.initiative()
        waiting = copy.deepcopy(initiative)
        waiting.update({
            "state": "needs-input",
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            waiting, expected_digest=record_digest(initiative),
        )
        decision = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "decide", {
                "paused_seal_id": paused["seal_id"],
                "decision": "Continue with the retained candidate.",
            }),
        )
        self.install_historical_active_plan()
        document = build_action_document(
            self.initiative(), "continue-node", {
                "node_id": "implementation-a",
                "paused_seal_id": paused["seal_id"],
                "decision_action_id": decision["action_id"],
            },
        )
        action, payload = _parse_document(document)
        before_attempts = self.store.list_attempts_snapshot(self.initiative_id)

        with self.assertRaises(ObservationOnlyPlanError), mock.patch.object(
            self.store, "save_attempt",
        ) as save_attempt:
            _continue_node(self.store, action, payload)

        save_attempt.assert_not_called()
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id), before_attempts,
        )


if __name__ == "__main__":
    unittest.main()
