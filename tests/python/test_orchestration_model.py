from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid

from lib.control.orchestration import model


INITIATIVE_ID = "11111111-1111-4111-8111-111111111111"
REPOSITORY_ID = "22222222-2222-4222-8222-222222222222"
NODE_ID = "implementation-a"
DIGEST = "a" * 64
TIMESTAMP = "2026-08-17T16:00:00Z"


def repository() -> dict:
    return {
        "repository_id": REPOSITORY_ID,
        "project_id": "memory-project",
        "root": "/tmp/repository",
        "control_repository_id": "control-repository",
        "initial_identity_digest": DIGEST,
    }


def limits() -> dict:
    return {
        "max_parallel": 2,
        "max_total_tasks": 8,
        "max_attempts_per_node": 2,
        "max_repair_cycles": 2,
        "max_retained_bytes_before_pause": 1024,
        "max_retained_inodes_before_pause": 100,
        "deadline": None,
    }


def initiative() -> dict:
    return {
        "contract": model.INITIATIVE_CONTRACT,
        "initiative_id": INITIATIVE_ID,
        "slug": "orchestration-test",
        "label": "Orchestration test",
        "state": "draft",
        "objective": "Implement a bounded test change.",
        "acceptance_criteria": ["All declared checks pass."],
        "scope": {"kind": "repository", "repository": repository()},
        "active_plan": None,
        "limits": limits(),
        "coordinator": None,
        "state_revision": 0,
        "forbidden_action_classes": list(model.FORBIDDEN_ACTION_CLASSES),
        "last_event_sequence": 0,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    }


def base(policy: str = "approved-baseline") -> dict:
    return {
        "policy": policy,
        "scope_origin": {"jj_commit_id": "b" * 40, "tree_digest": "c" * 64},
        "upstream_node_ids": [],
        "seal_inputs": [],
    }


def node() -> dict:
    return {
        "contract": model.NODE_CONTRACT,
        "node_id": NODE_ID,
        "type": "work",
        "goal": "Implement the requested bounded change.",
        "role": "implementer",
        "workflow": "none",
        "harness": "codex",
        "repository_id": REPOSITORY_ID,
        "base": base(),
        "dependencies": [],
        "advisory_path_ownership": ["lib/control/orchestration"],
        "hard_write_scope": ["lib/control/orchestration"],
        "acceptance": "The implementation passes its tests.",
        "terminal_candidate": True,
        "state": "proposed",
    }


def plan() -> dict:
    return {
        "contract": model.PLAN_CONTRACT,
        "initiative_id": INITIATIVE_ID,
        "revision": 1,
        "digest": None,
        "status": "proposed",
        "repositories": [repository()],
        "limits": limits(),
        "declared_gates": [
            {"kind": "review", "node_id": "review-a", "required": True},
            {"kind": "verification", "node_id": "verify-a", "required": True},
        ],
        "nested_workflow_policy": {"workflow": "none", "single_writer": False},
        "acceptance_conditions": ["The terminal bundle passes review and verification."],
        "action_classes": ["task-start", "review", "verification"],
        "nodes": [node()],
    }


class OrchestrationModelTests(unittest.TestCase):
    def contract_records(self) -> list[tuple[object, dict]]:
        attempt_id = "33333333-3333-4333-8333-333333333333"
        task_id = "44444444-4444-4444-8444-444444444444"
        run_id = "55555555-5555-4555-8555-555555555555"
        action_id = "66666666-6666-4666-8666-666666666666"
        publication_id = "77777777-7777-4777-8777-777777777777"
        result_id = "88888888-8888-4888-8888-888888888888"
        seal_id = "99999999-9999-4999-8999-999999999999"
        evidence_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        review_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        verification_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        body = {"source": "model-test"}
        body_digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        attempt = {
            "contract": model.ATTEMPT_CONTRACT,
            "attempt_id": attempt_id,
            "initiative_id": INITIATIVE_ID,
            "node_id": NODE_ID,
            "task_id": task_id,
            "action_id": action_id,
            "ordinal": 1,
            "base": base(),
            "state": "allocated",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        publication = {
            "contract": model.RESULT_PUBLICATION_CONTRACT,
            "publication_id": publication_id,
            "result_id": result_id,
            "payload_digest": DIGEST,
            "initiative_id": INITIATIVE_ID,
            "node_id": NODE_ID,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "run_id": run_id,
            "state": "reserved",
            "body_digest": DIGEST,
            "receipt_sequence": 1,
            "attempt_revision": 0,
            "refusal": None,
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        result = {
            "contract": model.RESULT_CONTRACT,
            "publication_id": publication_id,
            "result_id": result_id,
            "payload_digest": DIGEST,
            "supersedes_result_id": None,
            "initiative_id": INITIATIVE_ID,
            "node_id": NODE_ID,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "run_id": run_id,
            "claim_status": "completed",
            "summary": "The bounded implementation completed.",
            "files_changed": ["lib/example.py"],
            "verification_attestations": [{
                "argv": ["python3", "-m", "unittest"],
                "cwd": ".",
                "exit_code": 0,
                "finished_at": TIMESTAMP,
                "output_digest": DIGEST,
                "summary": "Worker-reported checks passed.",
            }],
            "concerns": [],
            "follow_up": [],
            "published_at": TIMESTAMP,
        }
        seal = {
            "contract": model.SEAL_CONTRACT,
            "seal_id": seal_id,
            "initiative_id": INITIATIVE_ID,
            "node_id": NODE_ID,
            "attempt_id": attempt_id,
            "task_id": task_id,
            "run_id": run_id,
            "outcome": "success",
            "repository_id": REPOSITORY_ID,
            "scope_origin": {"jj_commit_id": "b" * 40, "tree_digest": "c" * 64},
            "base": {
                "kind": "repository-baseline",
                "jj_commit_id": "b" * 40,
                "tree_digest": "c" * 64,
                "seal_ids": [],
            },
            "read_only_failure_seal_ids": [],
            "jj_commit_id": "d" * 40,
            "tree_digest": "e" * 64,
            "diff_digest": "f" * 64,
            "cumulative_diff_digest": "0" * 64,
            "changed_paths": ["lib/example.py"],
            "cumulative_changed_paths": ["lib/example.py"],
            "result_id": result_id,
            "process_evidence_id": evidence_id,
            "sealed_at": TIMESTAMP,
        }
        review = {
            "contract": model.REVIEW_CONTRACT,
            "review_id": review_id,
            "initiative_id": INITIATIVE_ID,
            "node_id": "review-a",
            "attempt_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "task_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "run_id": "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "state": "accepted-pass",
            "target": {
                "seal_id": seal_id,
                "active_plan_digest": DIGEST,
                "specification_digest": "b" * 64,
                "repository_id": REPOSITORY_ID,
                "jj_commit_id": "d" * 40,
                "diff_digest": "f" * 64,
            },
            "verdict": "pass",
            "findings": [],
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        verification = {
            "contract": model.VERIFICATION_CONTRACT,
            "verification_id": verification_id,
            "initiative_id": INITIATIVE_ID,
            "node_id": "verify-a",
            "bundle_digest": DIGEST,
            "active_plan_digest": "b" * 64,
            "state": "passed",
            "commands": [{
                "argv": ["python3", "-m", "unittest"],
                "cwd": ".",
                "environment_policy_id": "hermetic-test",
                "started_at": TIMESTAMP,
                "finished_at": TIMESTAMP,
                "exit_code": 0,
                "signal": None,
                "timed_out": False,
                "output_digest": DIGEST,
                "pre_identity_digest": "b" * 64,
                "post_identity_digest": "b" * 64,
            }],
            "evidence_ids": [evidence_id],
            "outcome": "passed",
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        approval = {
            "contract": model.APPROVAL_CONTRACT,
            "request_id": "12121212-1212-4212-8212-121212121212",
            "initiative_id": INITIATIVE_ID,
            "action_class": "task-start",
            "binding_digest": DIGEST,
            "active_plan_digest": "b" * 64,
            "expected_state_revision": 0,
            "actor_kind": "operator",
            "actor_id": "keeper",
            "state": "requested",
            "expires_at": "2026-08-18T16:00:00Z",
            "rationale": None,
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        action = {
            "contract": model.ACTION_CONTRACT,
            "action_id": action_id,
            "initiative_id": INITIATIVE_ID,
            "actor_kind": "operator",
            "actor_id": "keeper",
            "action_class": "task-start",
            "payload_digest": DIGEST,
            "active_plan_digest": "b" * 64,
            "expected_state_revision": 0,
            "state": "received",
            "outcome": None,
            "received_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        event = {
            "contract": model.EVENT_CONTRACT,
            "sequence": 1,
            "event_id": "13131313-1313-4313-8313-131313131313",
            "initiative_id": INITIATIVE_ID,
            "type": "initiative-created",
            "actor_kind": "operator",
            "actor_id": "keeper",
            "subject_ids": [],
            "payload_digest": body_digest,
            "payload": body,
            "recorded_at": TIMESTAMP,
        }
        link = {
            "contract": model.LINK_CONTRACT,
            "initiative_id": INITIATIVE_ID,
            "active_plan_digest": DIGEST,
            "node_id": NODE_ID,
            "attempt_id": attempt_id,
            "action_id": action_id,
            "actor_kind": "operator",
            "actor_id": "keeper",
            "expected_initiative_revision": 0,
            "control_task_id": task_id,
            "control_task_identity_digest": "a" * 64,
            "control_task_record_digest": "b" * 64,
        }
        evidence = {
            "contract": model.EVIDENCE_CONTRACT,
            "evidence_id": evidence_id,
            "initiative_id": INITIATIVE_ID,
            "kind": "process-exit",
            "subject_id": NODE_ID,
            "digest": DIGEST,
            "summary": "Controller-observed normal exit.",
            "recorded_at": TIMESTAMP,
        }
        bundle = {
            "contract": model.BUNDLE_CONTRACT,
            "bundle_id": "14141414-1414-4414-8414-141414141414",
            "initiative_id": INITIATIVE_ID,
            "aggregate_spec_digest": DIGEST,
            "active_plan_digest": "b" * 64,
            "state": "compatible",
            "members": [{
                "repository_id": REPOSITORY_ID,
                "seal_id": seal_id,
                "jj_commit_id": "d" * 40,
                "tree_digest": "e" * 64,
                "diff_digest": "f" * 64,
                "materialization_id": "15151515-1515-4515-8515-151515151515",
                "review_id": review_id,
                "verification_id": verification_id,
            }],
            "controller_evidence_ids": [evidence_id],
            "outcome": "compatible",
            "bound_at": TIMESTAMP,
        }
        return [
            (model.validate_attempt, attempt),
            (model.validate_result_publication, publication),
            (model.validate_result, result),
            (model.validate_seal, seal),
            (model.validate_review, review),
            (model.validate_verification, verification),
            (model.validate_approval, approval),
            (model.validate_action, action),
            (model.validate_event, event),
            (model.validate_link, link),
            (model.validate_evidence, evidence),
            (model.validate_bundle, bundle),
        ]

    def test_primary_records_validate_and_digest_canonically(self) -> None:
        self.assertEqual(model.validate_initiative(initiative())["coordinator"], None)
        validated_plan = model.validate_plan_record(plan())
        self.assertEqual(validated_plan["revision"], 1)
        self.assertEqual(model.validate_node(node())["node_id"], NODE_ID)
        self.assertRegex(model.record_digest(initiative()), r"^[0-9a-f]{64}$")
        self.assertEqual(model.plan_digest(plan()), model.plan_digest(copy.deepcopy(plan())))
        approved = copy.deepcopy(plan())
        approved["status"] = "approved"
        self.assertEqual(
            model.plan_digest(plan()),
            model.plan_digest(approved),
            "plan approval binds content, not lifecycle status",
        )

    def test_hostile_common_fields_refuse(self) -> None:
        mutations = []
        extra = initiative()
        extra["extra"] = True
        mutations.append(extra)
        future = initiative()
        future["contract"] = "asha.orchestration-initiative.v2"
        mutations.append(future)
        noncanonical = initiative()
        noncanonical["initiative_id"] = "{" + INITIATIVE_ID + "}"
        mutations.append(noncanonical)
        controls = initiative()
        controls["objective"] = "bad\u202etext"
        mutations.append(controls)
        surrogate = initiative()
        surrogate["objective"] = "bad\ud800text"
        mutations.append(surrogate)
        oversized = initiative()
        oversized["objective"] = "x" * (model.MAX_OBJECTIVE_BYTES + 1)
        mutations.append(oversized)
        coordinator = initiative()
        coordinator["coordinator"] = {"id": str(uuid.uuid4())}
        mutations.append(coordinator)
        wrong_type = initiative()
        wrong_type["state_revision"] = "0"
        mutations.append(wrong_type)
        for record in mutations:
            with self.subTest(record=record), self.assertRaises(model.ModelError):
                model.validate_initiative(record)

    def test_canonical_path_digest_timestamp_and_relative_paths_refuse(self) -> None:
        bad_repo = repository()
        bad_repo["root"] = "/tmp/../tmp/repository"
        with self.assertRaises(model.ModelError):
            model.validate_repository_scope(bad_repo)
        bad_repo = repository()
        bad_repo["initial_identity_digest"] = "A" * 64
        with self.assertRaises(model.ModelError):
            model.validate_repository_scope(bad_repo)
        bad = initiative()
        bad["created_at"] = "2026-08-17T16:00:00+00:00"
        with self.assertRaises(model.ModelError):
            model.validate_initiative(bad)
        bad_node = node()
        bad_node["hard_write_scope"] = ["../escape"]
        with self.assertRaises(model.ModelError):
            model.validate_node(bad_node)

    def test_transition_machines_match_independent_literal_oracle(self) -> None:
        # This oracle is transcribed from the proposal.  It must never be built
        # by iterating over the production transition tables it tests.
        states = {
            "initiative": (
                "draft", "planning", "awaiting-plan-approval", "approved",
                "running", "needs-input", "paused", "ready-for-integration",
                "partial", "failed", "cancelled", "archived",
            ),
            "coordinator": (
                "absent", "starting", "active", "waiting", "needs-input",
                "stopping", "exited", "failed", "stale", "fenced",
            ),
            "node": (
                "proposed", "approved", "blocked", "ready", "dispatching",
                "running", "evaluating", "needs-input", "succeeded", "failed",
                "cancelled", "superseded", "stale",
            ),
            "attempt": (
                "allocated", "dispatching", "running", "reported", "awaiting-exit",
                "success-seal-ready", "failure-seal-ready", "paused-seal-ready",
                "sealing", "sealed-success", "sealed-failure", "sealed-paused",
                "readonly-ready", "completed-readonly", "result-missing",
                "launch-failed", "abnormal-exit", "failed-no-artifact",
                "indeterminate", "cancelled", "stale",
            ),
            "result-publication": (
                "reserved", "validating", "persisting", "completed", "refused",
                "indeterminate",
            ),
            "result": ("accepted",),
            "seal": (
                "preparing", "sealed-success", "sealed-failure", "sealed-paused",
                "indeterminate",
            ),
            "review": (
                "pending", "running", "submitted", "accepted-pass",
                "accepted-findings", "failed", "indeterminate", "stale",
            ),
            "verification": (
                "pending", "dispatching", "running", "passed", "failed",
                "indeterminate", "stale",
            ),
            "bundle": ("binding", "compatible", "incompatible", "indeterminate"),
            "approval": (
                "requested", "approved", "rejected", "expired", "cancelled",
                "consumed", "revoked-before-use",
            ),
            "action": (
                "received", "validated", "dispatching", "completed", "refused",
                "indeterminate",
            ),
        }
        must_accept = {
            "initiative": [
                ("draft", "planning"), ("planning", "awaiting-plan-approval"),
                ("awaiting-plan-approval", "planning"),
                ("awaiting-plan-approval", "approved"), ("approved", "running"),
                ("running", "needs-input"), ("running", "paused"),
                ("needs-input", "running"), ("paused", "running"),
                ("running", "ready-for-integration"), ("running", "partial"),
                ("running", "failed"), ("draft", "cancelled"),
                ("planning", "cancelled"), ("awaiting-plan-approval", "cancelled"),
                ("approved", "cancelled"), ("running", "cancelled"),
                ("needs-input", "cancelled"), ("paused", "cancelled"),
                ("ready-for-integration", "archived"), ("partial", "archived"),
                ("failed", "archived"), ("cancelled", "archived"),
            ],
            "coordinator": [
                ("absent", "starting"), ("starting", "active"),
                ("active", "waiting"), ("waiting", "active"),
                ("active", "needs-input"), ("waiting", "needs-input"),
                ("needs-input", "active"), ("starting", "stopping"),
                ("active", "stopping"), ("waiting", "stopping"),
                ("needs-input", "stopping"), ("stopping", "exited"),
                ("starting", "exited"), ("active", "exited"),
                ("waiting", "exited"), ("needs-input", "exited"),
                ("starting", "failed"), ("active", "failed"),
                ("waiting", "failed"), ("needs-input", "failed"),
                ("stopping", "failed"), ("starting", "stale"),
                ("active", "stale"), ("waiting", "stale"),
                ("needs-input", "stale"), ("stopping", "stale"),
                ("starting", "fenced"), ("active", "fenced"),
                ("waiting", "fenced"), ("needs-input", "fenced"),
                ("stopping", "fenced"), ("stale", "fenced"),
            ],
            "node": [
                ("proposed", "approved"), ("approved", "blocked"),
                ("approved", "ready"), ("blocked", "ready"),
                ("ready", "dispatching"), ("ready", "evaluating"),
                ("ready", "needs-input"), ("dispatching", "running"),
                ("dispatching", "evaluating"), ("running", "evaluating"),
                ("needs-input", "evaluating"), ("needs-input", "ready"),
                ("evaluating", "succeeded"), ("evaluating", "ready"),
                ("evaluating", "failed"), ("dispatching", "needs-input"),
                ("running", "needs-input"), ("evaluating", "needs-input"),
                ("proposed", "cancelled"), ("approved", "cancelled"),
                ("blocked", "cancelled"), ("ready", "cancelled"),
                ("dispatching", "cancelled"), ("running", "cancelled"),
                ("evaluating", "cancelled"), ("needs-input", "cancelled"),
                ("proposed", "superseded"), ("approved", "superseded"),
                ("blocked", "superseded"), ("ready", "superseded"),
                ("dispatching", "superseded"), ("running", "superseded"),
                ("evaluating", "superseded"), ("needs-input", "superseded"),
                ("succeeded", "superseded"), ("dispatching", "stale"),
                ("running", "stale"), ("evaluating", "stale"),
                ("needs-input", "stale"),
            ],
            "attempt": [
                ("allocated", "dispatching"), ("dispatching", "running"),
                ("dispatching", "launch-failed"), ("running", "reported"),
                ("reported", "awaiting-exit"),
                ("awaiting-exit", "success-seal-ready"),
                ("awaiting-exit", "paused-seal-ready"),
                ("awaiting-exit", "failure-seal-ready"),
                ("awaiting-exit", "readonly-ready"),
                ("abnormal-exit", "failure-seal-ready"),
                ("result-missing", "failure-seal-ready"),
                ("abnormal-exit", "failed-no-artifact"),
                ("result-missing", "failed-no-artifact"),
                ("success-seal-ready", "sealing"),
                ("failure-seal-ready", "sealing"),
                ("paused-seal-ready", "sealing"),
                ("sealing", "sealed-success"), ("sealing", "sealed-failure"),
                ("sealing", "sealed-paused"),
                ("readonly-ready", "completed-readonly"),
                ("running", "result-missing"),
                ("dispatching", "abnormal-exit"),
                ("running", "abnormal-exit"), ("reported", "abnormal-exit"),
                ("awaiting-exit", "abnormal-exit"),
                ("allocated", "indeterminate"),
                ("dispatching", "indeterminate"),
                ("running", "indeterminate"),
                ("reported", "indeterminate"),
                ("awaiting-exit", "indeterminate"),
                ("success-seal-ready", "indeterminate"),
                ("failure-seal-ready", "indeterminate"),
                ("paused-seal-ready", "indeterminate"),
                ("sealing", "indeterminate"),
                ("readonly-ready", "indeterminate"),
                ("result-missing", "indeterminate"),
                ("abnormal-exit", "indeterminate"),
                ("indeterminate", "allocated"),
                ("indeterminate", "dispatching"),
                ("indeterminate", "running"),
                ("indeterminate", "reported"),
                ("indeterminate", "awaiting-exit"),
                ("indeterminate", "success-seal-ready"),
                ("indeterminate", "failure-seal-ready"),
                ("indeterminate", "paused-seal-ready"),
                ("indeterminate", "sealing"),
                ("indeterminate", "readonly-ready"),
                ("indeterminate", "result-missing"),
                ("indeterminate", "abnormal-exit"),
                ("indeterminate", "launch-failed"),
                ("allocated", "cancelled"), ("dispatching", "cancelled"),
                ("running", "cancelled"), ("reported", "cancelled"),
                ("awaiting-exit", "cancelled"),
                ("success-seal-ready", "cancelled"),
                ("failure-seal-ready", "cancelled"),
                ("paused-seal-ready", "cancelled"), ("sealing", "cancelled"),
                ("readonly-ready", "cancelled"),
                ("result-missing", "cancelled"),
                ("abnormal-exit", "cancelled"),
                ("indeterminate", "cancelled"),
                ("dispatching", "stale"), ("running", "stale"),
                ("reported", "stale"), ("awaiting-exit", "stale"),
                ("success-seal-ready", "stale"),
                ("failure-seal-ready", "stale"),
                ("paused-seal-ready", "stale"), ("sealing", "stale"),
            ],
            "result-publication": [
                ("reserved", "validating"), ("validating", "persisting"),
                ("persisting", "completed"), ("reserved", "refused"),
                ("validating", "refused"), ("reserved", "indeterminate"),
                ("validating", "indeterminate"), ("persisting", "indeterminate"),
                ("indeterminate", "reserved"), ("indeterminate", "validating"),
                ("indeterminate", "persisting"), ("indeterminate", "completed"),
                ("indeterminate", "refused"),
            ],
            "result": [],
            "seal": [
                ("preparing", "sealed-success"),
                ("preparing", "sealed-failure"),
                ("preparing", "sealed-paused"),
                ("preparing", "indeterminate"),
            ],
            "review": [
                ("pending", "running"), ("running", "submitted"),
                ("submitted", "accepted-pass"),
                ("submitted", "accepted-findings"),
                ("pending", "failed"), ("pending", "indeterminate"),
                ("pending", "stale"), ("running", "failed"),
                ("running", "indeterminate"), ("running", "stale"),
                ("submitted", "failed"), ("submitted", "indeterminate"),
                ("submitted", "stale"),
            ],
            "verification": [
                ("pending", "dispatching"), ("dispatching", "running"),
                ("running", "passed"), ("running", "failed"),
                ("pending", "indeterminate"), ("pending", "stale"),
                ("dispatching", "indeterminate"), ("dispatching", "stale"),
                ("running", "indeterminate"), ("running", "stale"),
            ],
            "bundle": [
                ("binding", "compatible"), ("binding", "incompatible"),
                ("binding", "indeterminate"),
                ("indeterminate", "compatible"),
                ("indeterminate", "incompatible"),
                ("indeterminate", "binding"),
            ],
            "approval": [
                ("requested", "approved"), ("requested", "rejected"),
                ("requested", "expired"), ("requested", "cancelled"),
                ("approved", "consumed"), ("approved", "expired"),
                ("approved", "revoked-before-use"),
            ],
            "action": [
                ("received", "validated"), ("validated", "dispatching"),
                ("dispatching", "completed"), ("validated", "completed"),
                ("received", "refused"), ("validated", "refused"),
                ("dispatching", "indeterminate"),
                ("indeterminate", "completed"), ("indeterminate", "refused"),
            ],
        }
        for name, expected_states in states.items():
            oracle = {state: set() for state in expected_states}
            for source, target in must_accept[name]:
                oracle[source].add(target)
                with self.subTest(machine=name, source=source, target=target):
                    model.require_transition(name, source, target)
            self.assertEqual(
                {state: set(targets) for state, targets in model.MACHINES[name].items()},
                oracle,
                f"{name} transitions differ from the independent proposal oracle",
            )

        # One literal refusal for every state, plus the cold review's
        # representative non-self refusals.
        must_refuse = {
            "initiative": [
                ("draft", "draft"), ("planning", "planning"),
                ("awaiting-plan-approval", "awaiting-plan-approval"),
                ("approved", "approved"), ("running", "running"),
                ("needs-input", "needs-input"), ("paused", "paused"),
                ("ready-for-integration", "ready-for-integration"),
                ("partial", "partial"), ("failed", "failed"),
                ("cancelled", "cancelled"), ("archived", "archived"),
            ],
            "coordinator": [
                ("absent", "absent"), ("starting", "starting"),
                ("active", "active"), ("waiting", "waiting"),
                ("needs-input", "needs-input"), ("stopping", "stopping"),
                ("exited", "exited"), ("failed", "failed"),
                ("stale", "stale"), ("fenced", "fenced"),
            ],
            "node": [
                ("proposed", "proposed"), ("approved", "approved"),
                ("blocked", "blocked"), ("ready", "ready"),
                ("dispatching", "dispatching"), ("running", "running"),
                ("evaluating", "evaluating"), ("needs-input", "needs-input"),
                ("succeeded", "succeeded"), ("failed", "failed"),
                ("cancelled", "cancelled"), ("superseded", "superseded"),
                ("stale", "stale"),
            ],
            "attempt": [
                ("allocated", "allocated"), ("dispatching", "dispatching"),
                ("running", "running"), ("reported", "reported"),
                ("awaiting-exit", "awaiting-exit"),
                ("success-seal-ready", "success-seal-ready"),
                ("failure-seal-ready", "failure-seal-ready"),
                ("paused-seal-ready", "paused-seal-ready"),
                ("sealing", "sealing"), ("sealed-success", "sealed-success"),
                ("sealed-failure", "sealed-failure"),
                ("sealed-paused", "sealed-paused"),
                ("readonly-ready", "readonly-ready"),
                ("completed-readonly", "completed-readonly"),
                ("result-missing", "result-missing"),
                ("launch-failed", "launch-failed"),
                ("abnormal-exit", "abnormal-exit"),
                ("failed-no-artifact", "failed-no-artifact"),
                ("indeterminate", "indeterminate"),
                ("cancelled", "cancelled"), ("stale", "stale"),
            ],
            "result-publication": [
                ("reserved", "reserved"), ("validating", "validating"),
                ("persisting", "persisting"), ("completed", "completed"),
                ("refused", "refused"), ("indeterminate", "indeterminate"),
            ],
            "result": [("accepted", "accepted")],
            "seal": [
                ("preparing", "preparing"),
                ("sealed-success", "sealed-success"),
                ("sealed-failure", "sealed-failure"),
                ("sealed-paused", "sealed-paused"),
                ("indeterminate", "indeterminate"),
            ],
            "review": [
                ("pending", "pending"), ("running", "running"),
                ("submitted", "submitted"),
                ("accepted-pass", "accepted-pass"),
                ("accepted-findings", "accepted-findings"),
                ("failed", "failed"), ("indeterminate", "indeterminate"),
                ("stale", "stale"),
            ],
            "verification": [
                ("pending", "pending"), ("dispatching", "dispatching"),
                ("running", "running"), ("passed", "passed"),
                ("failed", "failed"), ("indeterminate", "indeterminate"),
                ("stale", "stale"),
            ],
            "bundle": [
                ("binding", "binding"), ("compatible", "compatible"),
                ("incompatible", "incompatible"),
                ("indeterminate", "indeterminate"),
            ],
            "approval": [
                ("requested", "requested"), ("approved", "approved"),
                ("rejected", "rejected"), ("expired", "expired"),
                ("cancelled", "cancelled"), ("consumed", "consumed"),
                ("revoked-before-use", "revoked-before-use"),
            ],
            "action": [
                ("received", "received"), ("validated", "validated"),
                ("dispatching", "dispatching"), ("completed", "completed"),
                ("refused", "refused"), ("indeterminate", "indeterminate"),
            ],
        }
        for name, refusals in must_refuse.items():
            self.assertEqual(len(refusals), len(states[name]))
            for source, target in refusals:
                with self.subTest(machine=name, source=source, target=target):
                    with self.assertRaises(model.ModelError):
                        model.require_transition(name, source, target)
        representative_refusals = [
            ("initiative", "running", "archived"),
            ("attempt", "allocated", "running"),
            ("attempt", "sealed-success", "allocated"),
        ]
        for name, source, target in representative_refusals:
            with self.subTest(machine=name, source=source, target=target):
                with self.assertRaises(model.ModelError):
                    model.require_transition(name, source, target)

    def test_action_coordinator_fields_are_absent_not_nullable(self) -> None:
        action = {
            "contract": model.ACTION_CONTRACT,
            "action_id": str(uuid.uuid4()),
            "initiative_id": INITIATIVE_ID,
            "actor_kind": "operator",
            "actor_id": "keeper",
            "action_class": "task-start",
            "payload_digest": DIGEST,
            "active_plan_digest": DIGEST,
            "expected_state_revision": 0,
            "state": "received",
            "outcome": None,
            "received_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        model.validate_action(action)
        action["coordinator_generation"] = None
        with self.assertRaises(model.ModelError):
            model.validate_action(action)

        action.pop("coordinator_generation")
        action["actor_kind"] = "coordinator"
        with self.assertRaises(model.ModelError):
            model.validate_action(action)

        approval = next(
            record for validator, record in self.contract_records()
            if validator is model.validate_approval
        )
        approval["actor_kind"] = "coordinator"
        with self.assertRaises(model.ModelError):
            model.validate_approval(approval)

    def test_review_execution_ids_are_nullable_only_while_pending(self) -> None:
        review = next(
            record for validator, record in self.contract_records()
            if validator is model.validate_review
        )
        pending = copy.deepcopy(review)
        pending.update({
            "attempt_id": None,
            "task_id": None,
            "run_id": None,
            "state": "pending",
            "verdict": None,
        })
        model.validate_review(pending)
        pending["state"] = "running"
        with self.assertRaises(model.ModelError):
            model.validate_review(pending)

    def test_attempt_action_id_is_nullable_only_for_allocation(self) -> None:
        attempt = next(
            record for validator, record in self.contract_records()
            if validator is model.validate_attempt
        )
        attempt["action_id"] = None
        model.validate_attempt(attempt)
        attempt["state"] = "dispatching"
        with self.assertRaisesRegex(model.ModelError, "allocated attempt"):
            model.validate_attempt(attempt)

    def test_every_record_contract_validates_and_rejects_extra_or_future_fields(self) -> None:
        for validator, record in self.contract_records():
            with self.subTest(contract=record["contract"]):
                self.assertEqual(validator(record)["contract"], record["contract"])
                self.assertRegex(model.record_digest(record), r"^[0-9a-f]{64}$")
                extra = copy.deepcopy(record)
                extra["unexpected"] = True
                with self.assertRaises(model.ModelError):
                    validator(extra)
                future = copy.deepcopy(record)
                future["contract"] = record["contract"].replace(".v1", ".v2")
                with self.assertRaises(model.ModelError):
                    validator(future)


if __name__ == "__main__":
    unittest.main()
