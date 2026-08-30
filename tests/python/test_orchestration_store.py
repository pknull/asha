from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from lib.control.orchestration.config import load_config
from lib.control.orchestration import model
from lib.control.orchestration.model import (
    ATTEMPT_CONTRACT,
    EVIDENCE_CONTRACT,
    EVENT_CONTRACT,
    NODE_CONTRACT,
    RESULT_INGESTION_CONTRACT,
    record_digest,
)
from lib.control.orchestration.store import (
    InitiativeStore,
    ObservationOnlyPlanError,
    StoreCommittedError,
    StoreError,
)
from tests.python.test_orchestration_model import (
    DIGEST,
    HISTORICAL_PLAN_DIGEST,
    HISTORICAL_PLAN_FIXTURE,
    HISTORICAL_PLAN_RAW_SHA256,
    INITIATIVE_ID,
    NODE_ID,
    TIMESTAMP,
    initiative,
    node,
    plan,
)


def contract_record(validator) -> dict:
    from tests.python.test_orchestration_model import OrchestrationModelTests

    records = OrchestrationModelTests(methodName="runTest").contract_records()
    return copy.deepcopy(next(record for candidate, record in records if candidate is validator))


def event(sequence: int, event_id: str, recorded_at: str) -> dict:
    payload = {"source": f"test-{sequence}"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        "contract": EVENT_CONTRACT,
        "sequence": sequence,
        "event_id": event_id,
        "initiative_id": INITIATIVE_ID,
        "type": "initiative-created" if sequence == 1 else "plan-proposed",
        "actor_kind": "operator",
        "actor_id": "keeper",
        "subject_ids": [],
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "payload": payload,
        "recorded_at": recorded_at,
    }


def result_ingestion(ingestion_id: str, workspace_path: str) -> dict:
    return {
        "contract": RESULT_INGESTION_CONTRACT,
        "ingestion_id": ingestion_id,
        "initiative_id": INITIATIVE_ID,
        "node_id": NODE_ID,
        "attempt_id": "33333333-3333-4333-8333-333333333333",
        "task_id": "44444444-4444-4444-8444-444444444444",
        "run_id": "55555555-5555-4555-8555-555555555555",
        "active_plan_digest": DIGEST,
        "control_task_identity_digest": DIGEST,
        "staging_token_digest": DIGEST,
        "workspace_path": workspace_path,
        "workspace_name": "workspace",
        "change_id": "k" * 32,
        "outbox_path": ".asha/outbox/result.json",
        "state": "reserved",
        "candidate_digest": None,
        "publication_id": None,
        "result_id": None,
        "claimed_commit_id": None,
        "claimed_tree_digest": None,
        "commit_creator": None,
        "verification_evidence_ids": [],
        "ingester": None,
        "refusal": None,
        "created_at": TIMESTAMP,
        "updated_at": TIMESTAMP,
    }


class OrchestrationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.env = env
        for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
            Path(env[key]).mkdir(mode=0o700)
        self.config = load_config(env)
        self.store = InitiativeStore(self.config)
        self.record = initiative()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self) -> None:
        self.store.save_initiative(self.record)

    def install_historical_plan(self, raw: bytes | None = None) -> Path:
        self.create()
        path = self.config.initiatives_dir / INITIATIVE_ID / "plans" / "0001.json"
        path.write_bytes(HISTORICAL_PLAN_FIXTURE.read_bytes() if raw is None else raw)
        path.chmod(0o600)
        return path

    def test_increment_one_plan_observation_preserves_exact_bytes_and_digest(self) -> None:
        path = self.install_historical_plan()
        before = path.read_bytes()
        self.assertEqual(hashlib.sha256(before).hexdigest(), HISTORICAL_PLAN_RAW_SHA256)

        listed = self.store.list_plans_snapshot(INITIATIVE_ID)
        explicit = self.store.read_plan_snapshot(INITIATIVE_ID, 1)

        self.assertEqual(listed, [explicit])
        self.assertEqual(explicit["digest"], HISTORICAL_PLAN_DIGEST)
        self.assertEqual(
            set(explicit["declared_gates"][1]), {"kind", "node_id", "required"},
        )
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(
            ObservationOnlyPlanError,
            r"retained asha\.orchestration-plan\.v1 revision 1 is observation-only: "
            r"one or more historical verification gates lack immutable commands and "
            r"environment_policy; execution authority cannot be inferred",
        ):
            self.store.read_plan(INITIATIVE_ID, 1)
        self.assertEqual(path.read_bytes(), before)

    def test_increment_one_plan_cannot_be_saved_as_new_authority(self) -> None:
        self.create()
        retained = json.loads(HISTORICAL_PLAN_FIXTURE.read_bytes())
        with self.assertRaisesRegex(StoreError, "missing 2 required field"):
            self.store.save_plan(INITIATIVE_ID, retained)

    def test_historical_observation_wrong_digest_is_corrupt_not_observation_only(self) -> None:
        value = json.loads(HISTORICAL_PLAN_FIXTURE.read_bytes())
        value["digest"] = "0" * 64
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        self.install_historical_plan(raw)

        with self.assertRaisesRegex(StoreError, "stored plan digest does not match") as caught:
            self.store.read_plan_snapshot(INITIATIVE_ID, 1)
        self.assertNotIsInstance(caught.exception, ObservationOnlyPlanError)
        with self.assertRaisesRegex(StoreError, "stored plan digest does not match") as caught:
            self.store.read_plan(INITIATIVE_ID, 1)
        self.assertNotIsInstance(caught.exception, ObservationOnlyPlanError)

    def test_explicit_plan_observation_does_not_create_layout_or_sweep_residue(self) -> None:
        path = self.install_historical_plan()
        initiative_dir = path.parents[1]
        assignments = initiative_dir / "assignments"
        assignments.rmdir()
        residue = path.parent / ".0002.json.test.tmp"
        residue.write_bytes(b"retained residue")
        residue.chmod(0o600)

        self.store.read_plan_snapshot(INITIATIVE_ID, 1)

        self.assertFalse(assignments.exists())
        self.assertEqual(residue.read_bytes(), b"retained residue")

    def test_current_plan_snapshot_and_executable_read_remain_identical(self) -> None:
        self.create()
        path = self.store.save_plan(INITIATIVE_ID, plan())
        before = path.read_bytes()
        observed = self.store.read_plan_snapshot(INITIATIVE_ID, 1)
        executable = self.store.read_plan(INITIATIVE_ID, 1)
        self.assertEqual(observed, executable)
        self.assertEqual(path.read_bytes(), before)

    def test_layout_modes_and_lock_reentrancy(self) -> None:
        self.create()
        initiative_dir = self.config.initiatives_dir / INITIATIVE_ID
        self.assertEqual(stat.S_IMODE(initiative_dir.stat().st_mode), 0o700)
        for child in initiative_dir.iterdir():
            if child.is_dir():
                self.assertEqual(
                    stat.S_IMODE(child.stat().st_mode), 0o700, child.name
                )
        self.assertEqual(
            stat.S_IMODE((initiative_dir / "initiative.json").stat().st_mode), 0o600
        )
        with self.store.transaction_lock(INITIATIVE_ID):
            with self.store.transaction_lock(INITIATIVE_ID):
                self.assertEqual(self.store.peek(INITIATIVE_ID)["initiative_id"], INITIATIVE_ID)

    def test_assignment_directory_is_created_on_demand_for_legacy_initiative(self) -> None:
        self.create()
        assignments = self.config.initiatives_dir / INITIATIVE_ID / "assignments"
        os.rmdir(assignments)
        attempt_id = "33333333-3333-4333-8333-333333333333"
        path = self.store.write_assignment(INITIATIVE_ID, attempt_id, b"assignment\n")
        self.assertEqual(path.read_bytes(), b"assignment\n")
        self.assertEqual(stat.S_IMODE(assignments.stat().st_mode), 0o700)

    def test_first_locked_use_adds_increment_2b_layout_to_legacy_initiative(self) -> None:
        self.create()
        preparations = (
            self.config.initiatives_dir / INITIATIVE_ID / "seal-preparations"
        )
        os.rmdir(preparations)
        with self.store.transaction_lock(INITIATIVE_ID):
            pass
        self.assertTrue(preparations.is_dir())
        self.assertEqual(stat.S_IMODE(preparations.stat().st_mode), 0o700)

    def test_digest_guarded_update_and_conflict(self) -> None:
        self.create()
        current = self.store.read_initiative(INITIATIVE_ID)
        updated = copy.deepcopy(current)
        updated["state"] = "planning"
        updated["state_revision"] = 1
        updated["updated_at"] = "2026-08-17T16:00:01Z"
        self.store.save_initiative(updated, expected_digest=record_digest(current))
        with self.assertRaisesRegex(StoreError, "digest mismatch"):
            self.store.save_initiative(updated, expected_digest=record_digest(current))

    def test_create_initiative_requires_exact_draft_baseline(self) -> None:
        mutations = []
        for field, value in (
            ("last_event_sequence", 1),
            ("state", "planning"),
            ("state_revision", 1),
        ):
            record = initiative()
            record[field] = value
            mutations.append(record)
        active = initiative()
        active["active_plan"] = {
            "revision": 1,
            "digest": hashlib.sha256(
                b"Controller-observed evidence."
            ).hexdigest(),
            "approval_id": "33333333-3333-4333-8333-333333333333",
        }
        mutations.append(active)
        for record in mutations:
            with self.subTest(record=record), self.assertRaisesRegex(StoreError, "new initiative"):
                self.store.save_initiative(record)

    def test_immutable_plan_refuses_second_write(self) -> None:
        self.create()
        self.store.save_plan(INITIATIVE_ID, plan())
        with self.assertRaisesRegex(StoreError, "revision"):
            self.store.save_plan(INITIATIVE_ID, plan())

    def test_plan_requires_proposed_status_and_contiguous_revisions(self) -> None:
        self.create()
        approved = plan()
        approved["status"] = "approved"
        with self.assertRaisesRegex(StoreError, "status.*proposed"):
            self.store.save_plan(INITIATIVE_ID, approved)

        gap = plan()
        gap["revision"] = 2
        with self.assertRaisesRegex(StoreError, "revision.*exactly 1"):
            self.store.save_plan(INITIATIVE_ID, gap)

        self.store.save_plan(INITIATIVE_ID, plan())
        next_plan = plan()
        next_plan["revision"] = 2
        self.store.save_plan(INITIATIVE_ID, next_plan)

        skipped = plan()
        skipped["revision"] = 4
        with self.assertRaisesRegex(StoreError, "revision.*exactly 3"):
            self.store.save_plan(INITIATIVE_ID, skipped)

    def test_write_once_crash_residue_is_swept_by_next_write(self) -> None:
        self.create()
        real_unlink = os.unlink

        def refuse_temporary_unlink(path, *args, **kwargs):
            if str(path).startswith(".") and ".tmp." in str(path):
                raise OSError(errno.EIO, "simulated unlink crash window")
            return real_unlink(path, *args, **kwargs)

        with mock.patch(
            "lib.control.orchestration.store.os.unlink",
            side_effect=refuse_temporary_unlink,
        ):
            with self.assertRaises(StoreCommittedError):
                self.store.save_plan(INITIATIVE_ID, plan())

        plans_dir = self.config.initiatives_dir / INITIATIVE_ID / "plans"
        self.assertTrue(any(".tmp." in path.name for path in plans_dir.iterdir()))
        residue = next(path for path in plans_dir.iterdir() if ".tmp." in path.name)
        residue.rename(plans_dir / ".0001.json.tmp.crash-window")

        inventory = self.store.inventory(INITIATIVE_ID)
        self.assertEqual(inventory["plans"]["inodes"], 1)
        self.assertEqual(self.store.read_plan(INITIATIVE_ID, 1)["revision"], 1)
        self.assertFalse(any(".tmp." in path.name for path in plans_dir.iterdir()))

        with self.assertRaisesRegex(StoreError, "revision"):
            self.store.save_plan(INITIATIVE_ID, plan())

        next_plan = plan()
        next_plan["revision"] = 2
        self.store.save_plan(INITIATIVE_ID, next_plan)
        self.assertEqual(self.store.read_plan(INITIATIVE_ID, 2)["revision"], 2)

        evidence = {
            "contract": EVIDENCE_CONTRACT,
            "evidence_id": "33333333-3333-4333-8333-333333333333",
            "initiative_id": INITIATIVE_ID,
            "kind": "process-exit",
            "subject_id": NODE_ID,
            "digest": hashlib.sha256(
                b"Controller-observed evidence."
            ).hexdigest(),
            "summary": "Controller-observed evidence.",
            "recorded_at": TIMESTAMP,
        }
        with mock.patch(
            "lib.control.orchestration.store.os.unlink",
            side_effect=refuse_temporary_unlink,
        ):
            with self.assertRaises(StoreCommittedError):
                self.store.save_evidence(INITIATIVE_ID, evidence)

        self.assertEqual(
            self.store.read_evidence(INITIATIVE_ID, evidence["evidence_id"]),
            evidence,
        )
        self.assertEqual(self.store.inventory(INITIATIVE_ID)["evidence"]["inodes"], 1)

        first_event = event(
            1,
            "44444444-4444-4444-8444-444444444444",
            "2026-08-17T16:00:01Z",
        )
        with mock.patch(
            "lib.control.orchestration.store.os.unlink",
            side_effect=refuse_temporary_unlink,
        ):
            with self.assertRaises(StoreCommittedError):
                self.store.append_event(INITIATIVE_ID, first_event)

        self.assertEqual(self.store.list_events(INITIATIVE_ID), [first_event])
        events_dir = self.config.initiatives_dir / INITIATIVE_ID / "events"
        event_path = next(path for path in events_dir.iterdir() if not path.name.startswith("."))
        verify_residue = events_dir / f".{event_path.name}.tmp.verify-crash"
        os.link(event_path, verify_residue)
        with self.assertRaisesRegex(StoreError, "event sequence disagrees"):
            self.store.verify_events(INITIATIVE_ID)
        self.assertFalse(verify_residue.exists())

    def test_mutable_node_and_attempt_snapshots(self) -> None:
        self.create()
        value = node()
        self.store.save_node(INITIATIVE_ID, value)
        current = self.store.read_node(INITIATIVE_ID, NODE_ID)
        updated = copy.deepcopy(current)
        updated["state"] = "approved"
        self.store.save_node(
            INITIATIVE_ID, updated, expected_digest=record_digest(current)
        )
        attempt = {
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": "33333333-3333-4333-8333-333333333333",
            "initiative_id": INITIATIVE_ID,
            "node_id": NODE_ID,
            "task_id": "44444444-4444-4444-8444-444444444444",
            "action_id": "55555555-5555-4555-8555-555555555555",
            "ordinal": 1,
            "base": value["base"],
            "state": "allocated",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
        }
        self.store.save_attempt(INITIATIVE_ID, attempt)
        self.assertEqual(
            self.store.read_attempt(INITIATIVE_ID, attempt["attempt_id"])["state"],
            "allocated",
        )

    def test_mutable_records_enforce_transitions_immutable_fields_and_terminality(self) -> None:
        self.create()
        value = node()
        self.store.save_node(INITIATIVE_ID, value)
        current = self.store.read_node(INITIATIVE_ID, NODE_ID)

        illegal = copy.deepcopy(current)
        illegal["state"] = "running"
        with self.assertRaisesRegex(StoreError, "illegal record transition"):
            self.store.save_node(
                INITIATIVE_ID, illegal, expected_digest=record_digest(current)
            )

        changed = copy.deepcopy(current)
        changed["state"] = "approved"
        changed["goal"] = "Changed after proposal."
        with self.assertRaisesRegex(StoreError, "immutable record field"):
            self.store.save_node(
                INITIATIVE_ID, changed, expected_digest=record_digest(current)
            )

        approval = contract_record(model.validate_approval)
        self.store.save_approval(INITIATIVE_ID, approval)
        current_approval = self.store.read_approval(INITIATIVE_ID, approval["request_id"])
        rejected = copy.deepcopy(current_approval)
        rejected["state"] = "rejected"
        self.store.save_approval(
            INITIATIVE_ID,
            rejected,
            expected_digest=record_digest(current_approval),
        )
        with self.assertRaisesRegex(StoreError, "write-once terminal"):
            self.store.save_approval(
                INITIATIVE_ID,
                rejected,
                expected_digest=record_digest(rejected),
            )

        action = contract_record(model.validate_action)
        self.store.save_action(INITIATIVE_ID, action)
        current_action = self.store.read_action(INITIATIVE_ID, action["action_id"])
        refused = copy.deepcopy(current_action)
        refused.update({"state": "refused", "outcome": "operator refused"})
        self.store.save_action(
            INITIATIVE_ID,
            refused,
            expected_digest=record_digest(current_action),
        )
        with self.assertRaisesRegex(StoreError, "write-once terminal"):
            self.store.save_action(
                INITIATIVE_ID,
                refused,
                expected_digest=record_digest(refused),
            )

    def test_review_verification_and_bundle_are_mutable_until_terminal(self) -> None:
        self.create()

        review = contract_record(model.validate_review)
        assigned = {
            field: review[field] for field in ("attempt_id", "task_id", "run_id")
        }
        review.update({
            "attempt_id": None,
            "task_id": None,
            "run_id": None,
            "state": "pending",
            "verdict": None,
        })
        self.store.save_review(INITIATIVE_ID, review)
        current_review = self.store.read_review(INITIATIVE_ID, review["review_id"])
        running = copy.deepcopy(current_review)
        running.update(assigned)
        running["state"] = "running"
        running["updated_at"] = "2026-08-17T16:00:01Z"
        self.store.save_review(
            INITIATIVE_ID,
            running,
            expected_digest=record_digest(current_review),
        )
        current_review = self.store.read_review(INITIATIVE_ID, review["review_id"])
        tampered_review = copy.deepcopy(current_review)
        tampered_review["state"] = "submitted"
        tampered_review["target"]["diff_digest"] = "0" * 64
        with self.assertRaisesRegex(StoreError, "immutable record field"):
            self.store.save_review(
                INITIATIVE_ID,
                tampered_review,
                expected_digest=record_digest(current_review),
            )
        reassigned_review = copy.deepcopy(current_review)
        reassigned_review["state"] = "submitted"
        reassigned_review["task_id"] = "10101010-1010-4010-8010-101010101010"
        with self.assertRaisesRegex(StoreError, "immutable record field"):
            self.store.save_review(
                INITIATIVE_ID,
                reassigned_review,
                expected_digest=record_digest(current_review),
            )
        submitted = copy.deepcopy(current_review)
        submitted["state"] = "submitted"
        submitted["updated_at"] = "2026-08-17T16:00:02Z"
        self.store.save_review(
            INITIATIVE_ID,
            submitted,
            expected_digest=record_digest(current_review),
        )
        current_review = self.store.read_review(INITIATIVE_ID, review["review_id"])
        accepted = copy.deepcopy(current_review)
        accepted.update({
            "state": "accepted-pass",
            "verdict": "pass",
            "updated_at": "2026-08-17T16:00:03Z",
        })
        self.store.save_review(
            INITIATIVE_ID,
            accepted,
            expected_digest=record_digest(current_review),
        )
        rewritten_review = copy.deepcopy(accepted)
        rewritten_review["updated_at"] = "2026-08-17T16:00:03.500000Z"
        with self.assertRaisesRegex(StoreError, "terminal review evidence"):
            self.store.save_review(
                INITIATIVE_ID,
                rewritten_review,
                expected_digest=record_digest(accepted),
            )
        stale_review = copy.deepcopy(accepted)
        stale_review.update({
            "state": "stale", "verdict": None, "findings": [],
            "updated_at": "2026-08-17T16:00:04Z",
        })
        self.store.save_review(
            INITIATIVE_ID, stale_review, expected_digest=record_digest(accepted),
        )
        with self.assertRaisesRegex(StoreError, "write-once terminal"):
            self.store.save_review(
                INITIATIVE_ID,
                stale_review,
                expected_digest=record_digest(stale_review),
            )

        verification = contract_record(model.validate_verification)
        verification.update({
            "state": "pending",
            "commands": [],
            "evidence_ids": [],
            "outcome": None,
        })
        self.store.save_verification(INITIATIVE_ID, verification)
        current_verification = self.store.read_verification(
            INITIATIVE_ID, verification["verification_id"]
        )
        dispatching = copy.deepcopy(current_verification)
        dispatching["state"] = "dispatching"
        dispatching["updated_at"] = "2026-08-17T16:00:01Z"
        self.store.save_verification(
            INITIATIVE_ID,
            dispatching,
            expected_digest=record_digest(current_verification),
        )
        current_verification = self.store.read_verification(
            INITIATIVE_ID, verification["verification_id"]
        )
        running_verification = copy.deepcopy(current_verification)
        running_verification["state"] = "running"
        running_verification["updated_at"] = "2026-08-17T16:00:02Z"
        self.store.save_verification(
            INITIATIVE_ID,
            running_verification,
            expected_digest=record_digest(current_verification),
        )
        current_verification = self.store.read_verification(
            INITIATIVE_ID, verification["verification_id"]
        )
        passed = copy.deepcopy(current_verification)
        passed.update({
            "state": "passed",
            "outcome": "passed",
            "updated_at": "2026-08-17T16:00:03Z",
        })
        self.store.save_verification(
            INITIATIVE_ID,
            passed,
            expected_digest=record_digest(current_verification),
        )
        rewritten_verification = copy.deepcopy(passed)
        rewritten_verification["updated_at"] = "2026-08-17T16:00:03.500000Z"
        with self.assertRaisesRegex(StoreError, "terminal verification evidence"):
            self.store.save_verification(
                INITIATIVE_ID,
                rewritten_verification,
                expected_digest=record_digest(passed),
            )
        stale_verification = copy.deepcopy(passed)
        stale_verification.update({
            "state": "stale", "outcome": None,
            "updated_at": "2026-08-17T16:00:04Z",
        })
        self.store.save_verification(
            INITIATIVE_ID, stale_verification,
            expected_digest=record_digest(passed),
        )
        with self.assertRaisesRegex(StoreError, "write-once terminal"):
            self.store.save_verification(
                INITIATIVE_ID,
                stale_verification,
                expected_digest=record_digest(stale_verification),
            )

        bundle = contract_record(model.validate_bundle)
        bundle.update({"state": "binding", "outcome": None, "bound_at": None})
        self.store.save_bundle(INITIATIVE_ID, bundle)
        current_bundle = self.store.read_bundle(INITIATIVE_ID, bundle["bundle_id"])
        compatible = copy.deepcopy(current_bundle)
        compatible.update({
            "state": "compatible",
            "outcome": "compatible",
            "bound_at": TIMESTAMP,
        })
        self.store.save_bundle(
            INITIATIVE_ID,
            compatible,
            expected_digest=record_digest(current_bundle),
        )
        with self.assertRaisesRegex(StoreError, "write-once terminal"):
            self.store.save_bundle(
                INITIATIVE_ID,
                compatible,
                expected_digest=record_digest(compatible),
            )

    def test_event_sequence_gaps_and_duplicates_refuse(self) -> None:
        self.create()
        first = event(
            1,
            "33333333-3333-4333-8333-333333333333",
            "2026-08-17T16:00:01Z",
        )
        self.store.append_event(INITIATIVE_ID, first)
        with self.assertRaisesRegex(StoreError, "event sequence"):
            self.store.append_event(INITIATIVE_ID, first)
        gap = copy.deepcopy(first)
        gap["sequence"] = 3
        gap["event_id"] = "44444444-4444-4444-8444-444444444444"
        with self.assertRaisesRegex(StoreError, "event sequence"):
            self.store.append_event(INITIATIVE_ID, gap)

    def test_append_event_checks_tail_only_and_verify_events_checks_full_chain(self) -> None:
        self.create()
        first = event(
            1,
            "33333333-3333-4333-8333-333333333333",
            "2026-08-17T16:00:01Z",
        )
        second = event(
            2,
            "44444444-4444-4444-8444-444444444444",
            "2026-08-17T16:00:02Z",
        )
        self.store.append_event(INITIATIVE_ID, first)
        with mock.patch.object(
            self.store,
            "_event_records",
            side_effect=AssertionError("append_event must not scan the full journal"),
        ):
            self.store.append_event(INITIATIVE_ID, second)
        self.assertEqual(len(self.store.verify_events(INITIATIVE_ID)), 2)

        snapshot = self.store.read_initiative(INITIATIVE_ID)
        snapshot["last_event_sequence"] = 3
        snapshot["state_revision"] += 1
        snapshot["updated_at"] = "2026-08-17T16:00:03Z"
        self.store.save_initiative(
            snapshot,
            expected_digest=record_digest(self.store.read_initiative(INITIATIVE_ID)),
        )
        with self.assertRaisesRegex(StoreError, "event sequence disagrees"):
            self.store.verify_events(INITIATIVE_ID)

    def test_record_reads_refuse_symlinks_bad_modes_and_oversize_records(self) -> None:
        self.create()
        self.store.save_plan(INITIATIVE_ID, plan())
        plan_path = self.config.initiatives_dir / INITIATIVE_ID / "plans" / "0001.json"
        plan_path.chmod(0o644)
        with self.assertRaisesRegex(StoreError, "mode"):
            self.store.read_plan(INITIATIVE_ID, 1)

        result_id = "33333333-3333-4333-8333-333333333333"
        result_path = self.config.initiatives_dir / INITIATIVE_ID / "results" / f"{result_id}.json"
        outside = self.root / "outside.json"
        outside.write_text("{}")
        outside.chmod(0o600)
        result_path.symlink_to(outside)
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.read_result(INITIATIVE_ID, result_id)

        oversized = node()
        paths = [f"path-{index:03d}/" + ("x" * 480) for index in range(512)]
        oversized["advisory_path_ownership"] = paths
        oversized["hard_write_scope"] = paths
        with self.assertRaisesRegex(StoreError, "record exceeds"):
            self.store.save_node(INITIATIVE_ID, oversized)

    def test_subprocess_lock_excludes_parent_until_release(self) -> None:
        self.create()
        root = Path(__file__).resolve().parents[2]
        child_env = os.environ.copy()
        child_env.update(self.env)
        child_env["PYTHONPATH"] = str(root)
        script = f"""
import time
from lib.control.orchestration.config import load_config
from lib.control.orchestration.store import InitiativeStore
store = InitiativeStore(load_config())
with store.transaction_lock({INITIATIVE_ID!r}):
    print('locked', flush=True)
    time.sleep(30)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=root,
            env=child_env,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")

            def acquire() -> bool:
                with self.store.transaction_lock(INITIATIVE_ID):
                    return True

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(acquire)
                with self.assertRaises(FutureTimeout):
                    future.result(timeout=0.25)
                child.terminate()
                child.wait(timeout=5)
                self.assertTrue(future.result(timeout=5))
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()

    def test_inventory_thresholds_count_files_without_following_links(self) -> None:
        self.config = replace(self.config, max_retained_bytes_before_pause=1)
        self.store = InitiativeStore(self.config)
        self.create()
        evidence = {
            "contract": EVIDENCE_CONTRACT,
            "evidence_id": "33333333-3333-4333-8333-333333333333",
            "initiative_id": INITIATIVE_ID,
            "kind": "process-exit",
            "subject_id": NODE_ID,
            "digest": hashlib.sha256(
                b"Controller-observed evidence."
            ).hexdigest(),
            "summary": "Controller-observed evidence.",
            "recorded_at": TIMESTAMP,
        }
        self.store.save_evidence(INITIATIVE_ID, evidence)
        result = self.store.inventory(INITIATIVE_ID)
        self.assertGreater(result["initiative"]["bytes"], 0)
        self.assertEqual(result["evidence"]["inodes"], 1)
        self.assertTrue(result["pause_recommended"])

        bad = self.config.initiatives_dir / INITIATIVE_ID / "results" / "bad.json"
        bad.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.inventory(INITIATIVE_ID)

    def test_snapshot_readers_reject_filename_mismatch_gap_and_symlink(self) -> None:
        self.create()
        self.store.save_node(INITIATIVE_ID, node())
        nodes_dir = self.config.initiatives_dir / INITIATIVE_ID / "nodes"
        (nodes_dir / f"{NODE_ID}.json").rename(nodes_dir / "different-node.json")
        with self.assertRaisesRegex(StoreError, "does not match its filename"):
            self.store.list_nodes_snapshot(INITIATIVE_ID)

        self.store.save_plan(INITIATIVE_ID, plan())
        second = plan()
        second["revision"] = 2
        self.store.save_plan(INITIATIVE_ID, second)
        plans_dir = self.config.initiatives_dir / INITIATIVE_ID / "plans"
        (plans_dir / "0001.json").unlink()
        with self.assertRaisesRegex(StoreError, "gap"):
            self.store.list_plans_snapshot(INITIATIVE_ID)

        attempts_dir = self.config.initiatives_dir / INITIATIVE_ID / "attempts"
        outside = self.root / "outside-attempt.json"
        outside.write_text("{}")
        outside.chmod(0o600)
        attempt_id = "33333333-3333-4333-8333-333333333333"
        (attempts_dir / f"{attempt_id}.json").symlink_to(outside)
        with self.assertRaisesRegex(StoreError, "symlink"):
            self.store.list_attempts_snapshot(INITIATIVE_ID)

    def test_ingestion_survey_names_unreadable_records_and_repairs_nothing(self) -> None:
        self.create()
        live_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        clobbered_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        unreadable_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        self.store.save_result_ingestion(
            INITIATIVE_ID,
            result_ingestion(live_id, str(self.root.resolve() / "workspace")),
        )
        directory = self.config.initiatives_dir / INITIATIVE_ID / "result-ingestions"
        live = directory / f"{live_id}.json"
        # The observed corruption: a verbatim copy of a live record landed
        # under another record's filename.
        clobbered = directory / f"{clobbered_id}.json"
        clobbered.write_bytes(live.read_bytes())
        clobbered.chmod(0o600)
        unreadable = directory / f"{unreadable_id}.json"
        unreadable.write_text("{ not json")
        unreadable.chmod(0o600)

        with self.assertRaisesRegex(StoreError, "does not match its filename"):
            self.store.list_result_ingestions_snapshot(INITIATIVE_ID)

        problems: list[dict[str, str]] = []
        records = self.store.list_result_ingestions_snapshot(
            INITIATIVE_ID, problems=problems,
        )
        self.assertEqual([record["ingestion_id"] for record in records], [live_id])
        self.assertEqual(
            [problem["name"] for problem in problems],
            [f"{clobbered_id}.json", f"{unreadable_id}.json"],
        )
        self.assertEqual({problem["directory"] for problem in problems}, {"result-ingestions"})
        self.assertIn("does not match its filename", problems[0]["reason"])
        self.assertIn(INITIATIVE_ID, problems[0]["reason"])
        self.assertIn(str(clobbered), problems[0]["reason"])
        self.assertIn("field ingestion_id", problems[0]["reason"])
        self.assertIn(f"filename stem='{clobbered_id}'", problems[0]["reason"])
        self.assertIn(f"record value='{live_id}'", problems[0]["reason"])
        with self.assertRaises(StoreError) as mismatch:
            self.store.read_result_ingestion(INITIATIVE_ID, clobbered_id)
        exact_detail = str(mismatch.exception)
        self.assertIn(INITIATIVE_ID, exact_detail)
        self.assertIn(str(clobbered), exact_detail)
        self.assertIn(f"filename stem='{clobbered_id}'", exact_detail)
        self.assertIn(f"record value='{live_id}'", exact_detail)
        # Surveying is a read: nothing is renamed, repaired or removed, because
        # the clobbered payload belongs to the live record and adopting it
        # under this filename would mint a duplicate of a real record.
        self.assertEqual(
            sorted(item.name for item in directory.iterdir()),
            sorted([live.name, clobbered.name, unreadable.name]),
        )
        self.assertEqual(clobbered.read_bytes(), live.read_bytes())
        # Sibling readers of the same directory keep failing closed.
        with self.assertRaisesRegex(StoreError, "does not match its filename"):
            self.store.list_result_ingestions_snapshot(INITIATIVE_ID)

    def test_snapshot_inventory_counts_and_preserves_hidden_residue(self) -> None:
        self.create()
        residue = (
            self.config.initiatives_dir / INITIATIVE_ID / "evidence"
            / ".record.json.tmp.interrupted"
        )
        residue.write_text("residue")
        residue.chmod(0o600)
        snapshot = self.store.inventory(INITIATIVE_ID, locked=False)
        self.assertEqual(snapshot["evidence"]["inodes"], 1)
        self.assertTrue(residue.exists())
        self.assertEqual(self.store.record_counts_snapshot(INITIATIVE_ID)["evidence"], 0)
        self.assertTrue(residue.exists())

    def test_symlink_and_duplicate_json_records_refuse_and_list_skips_corrupt(self) -> None:
        self.create()
        path = self.config.initiatives_dir / INITIATIVE_ID / "initiative.json"
        path.write_text('{"contract":"x","contract":"y"}')
        path.chmod(0o600)
        with self.assertRaisesRegex(StoreError, "duplicate JSON key"):
            self.store.peek(INITIATIVE_ID)
        self.assertEqual(self.store.list_initiatives(), [])
        self.assertEqual(len(self.store.skipped), 1)


if __name__ == "__main__":
    unittest.main()
