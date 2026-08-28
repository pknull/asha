from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from lib.control.jj import ImmutableTree, JjError, WorkspaceIdentity
from lib.control.prune import PruneRecordStore
from lib.control.cli import main as control_main
from lib.control.store import StoreError
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.model import record_digest, validate_result
from lib.control.orchestration.seals import (
    SealError,
    _process_kind,
    prepare_and_publish_seal,
    reconcile_seal_drift,
)
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.test_orchestration_graph import valid_plan


class SimulatedDeath(BaseException):
    pass


def _entry(path: str, marker: str) -> tuple[str, str, str]:
    content = marker.encode()
    return path, "100644", hashlib.sha256(content).hexdigest()


class SealJj:
    def __init__(self, task: dict, origin_digest: str, entries: tuple, *, parent=None):
        self.task = task
        self.origin_digest = origin_digest
        self.entries = entries
        self.parent = parent or task["jj"]["base_commit_id"]
        self.current_commit = task["jj"]["working_commit_id"]

    def inspect_workspace(self, path, name, *, snapshot=False, require_empty=True):
        return WorkspaceIdentity(
            name=name,
            change_id=self.task["jj"]["change_id"],
            commit_id=self.current_commit,
            parent_commit_ids=(self.parent,),
            description="test",
        )

    def immutable_tree(self, repository, commit_id):
        origin = self.task["jj"]["base_commit_id"]
        if commit_id == origin:
            return ImmutableTree(commit_id, self.origin_digest, ())
        digest = hashlib.sha256(json.dumps(self.entries).encode()).hexdigest()
        return ImmutableTree(commit_id, digest, self.entries)


class OrchestrationSealTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        original = valid_plan

        def wider_plan():
            plan = original()
            node = plan["nodes"][0]
            node["hard_write_scope"] = ["lib"]
            node["advisory_path_ownership"] = ["lib/control/orchestration"]
            return plan

        with mock.patch(
            "tests.python.orchestration_execution_fixtures.valid_plan",
            side_effect=wider_plan,
        ):
            super().setUp()
        self._dispatch()

    def _dispatch(self) -> None:
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            payload["task"]["jj"]["working_commit_id"] = "d" * 40
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            submitted = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(submitted["state"], "completed")
        self.attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.node = self.store.read_node(self.initiative_id, self.attempt["node_id"])
        self.origin_digest = self.attempt["base"]["scope_origin"]["tree_digest"]

    def _accept(
        self, status: str = "completed", *, files: list[str] | None = None,
    ) -> dict:
        attempt = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        result_id = str(uuid.uuid4())
        result = validate_result({
            "contract": "asha.orchestration-result.v1",
            "publication_id": str(uuid.uuid4()),
            "result_id": result_id,
            "payload_digest": "a" * 64,
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": attempt["node_id"],
            "attempt_id": attempt["attempt_id"],
            "task_id": attempt["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": status,
            "summary": status,
            "files_changed": files or ["lib/control/orchestration/change.py"],
            "verification_attestations": [],
            "concerns": [],
            "follow_up": [],
            "published_at": now_text(),
        })
        self.store.save_result(self.initiative_id, result)
        changed = copy.deepcopy(attempt)
        changed.update({
            "state": "reported",
            "result_publication_id": result["publication_id"],
            "result_id": result_id,
            "updated_at": now_text(),
        })
        self.store.save_attempt(
            self.initiative_id, changed, expected_digest=record_digest(attempt),
        )
        return result

    def _missing(self) -> None:
        attempt = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        changed = copy.deepcopy(attempt)
        changed.update({"state": "result-missing", "updated_at": now_text()})
        self.store.save_attempt(
            self.initiative_id, changed, expected_digest=record_digest(attempt),
        )

    def observed(self, *, normal: bool = True, jj_match: bool = True) -> dict:
        status = 0 if normal else 1
        state = "exited" if normal else "failed"
        evidence = [
            {"source": "tmux", "outcome": "missing", "detail": f"tmux pane process exited with status {status}", "state": None, "stale": False},
            {"source": "process", "outcome": "missing", "detail": "process absent", "state": None, "stale": False},
            {"source": "jj", "outcome": "match" if jj_match else "mismatch", "detail": "jj identity", "state": None, "stale": False},
        ]
        return {
            "contract": "asha.control-reconciliation.v1",
            "task_id": self.task["task_id"],
            "state": state,
            "blocker": None,
            "evidence": [],
            "runs": [{
                "contract": "asha.control-run-reconciliation.v1",
                "run_id": self.task["runs"][0]["run_id"],
                "state": state,
                "blocker": None,
                "evidence": evidence,
            }],
        }

    def seal(self, entries: tuple, *, observed=None, jj=None, phase_hook=None):
        adapter = jj or SealJj(self.task, self.origin_digest, entries)
        return prepare_and_publish_seal(
            self.store, self.initiative_id, self.attempt["attempt_id"],
            self.task, observed or self.observed(), jj=adapter,
            phase_hook=phase_hook,
        )

    def evidence(self, seal: dict) -> dict:
        record = self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )
        return json.loads(record["summary"])

    def test_preparing_completed_claim_and_all_four_conditions_produce_success(self) -> None:
        self._accept("completed")
        seal = self.seal((_entry("lib/control/orchestration/change.py", "x"),))
        self.assertEqual(seal["outcome"], "success")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["state"],
            "sealed-success",
        )
        self.assertEqual(self.store.read_node(self.initiative_id, self.node["node_id"])["state"], "succeeded")
        facts = self.evidence(seal)
        self.assertTrue(facts["accepted_completed_claim"])
        self.assertTrue(facts["normal_zero_exit"])
        self.assertTrue(facts["clean_identity"])
        self.assertTrue(facts["hard_scope_valid"])
        self.assertFalse(facts["verification_environment_degraded"])

    def test_success_refused_when_claim_process_identity_or_hard_scope_fails(self) -> None:
        self._accept("failed")
        seal = self.seal((_entry("lib/control/orchestration/change.py", "x"),))
        self.assertEqual(seal["outcome"], "failure")

    def test_completed_claim_followed_by_nonzero_exit_is_failure_never_success(self) -> None:
        self._accept("completed")
        seal = self.seal(
            (_entry("lib/control/orchestration/change.py", "x"),),
            observed=self.observed(normal=False),
        )
        self.assertEqual(seal["outcome"], "failure")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["state"],
            "sealed-failure",
        )
        self.assertFalse(self.evidence(seal)["normal_zero_exit"])

    def test_clean_identity_is_a_required_success_condition(self) -> None:
        self._accept("completed")
        adapter = SealJj(
            self.task, self.origin_digest,
            (_entry("lib/control/orchestration/change.py", "x"),),
            parent="e" * 40,
        )
        seal = self.seal(adapter.entries, jj=adapter)
        self.assertEqual(seal["outcome"], "failure")
        self.assertFalse(self.evidence(seal)["clean_identity"])

    def test_blocked_claim_prepares_and_publishes_paused_outcome(self) -> None:
        self._accept("needs-decision")
        seal = self.seal((_entry("lib/control/orchestration/change.py", "x"),))
        self.assertEqual(seal["outcome"], "paused")
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.store.read_node(self.initiative_id, self.node["node_id"])["state"], "needs-input")

    def test_result_missing_prepares_failure_seal(self) -> None:
        self._missing()
        seal = self.seal((_entry("lib/control/orchestration/partial.py", "x"),))
        self.assertEqual(seal["outcome"], "failure")
        self.assertIsNone(seal["result_id"])

    def test_hard_scope_violation_is_failure_and_records_offending_path(self) -> None:
        self._accept("completed")
        seal = self.seal((_entry("docs/outside.md", "x"),))
        self.assertEqual(seal["outcome"], "failure")
        facts = self.evidence(seal)
        self.assertEqual(facts["scope_violations"], ["docs/outside.md"])
        event = next(
            item for item in self.store.list_events_snapshot(self.initiative_id)
            if item["type"] == "seal-published"
        )
        self.assertEqual(event["payload"]["scope_violations"], ["docs/outside.md"])

    def test_advisory_divergence_is_separate_evidence_not_failure(self) -> None:
        self._accept("completed", files=["lib/other.py"])
        seal = self.seal((_entry("lib/other.py", "x"),))
        self.assertEqual(seal["outcome"], "success")
        facts = self.evidence(seal)
        self.assertEqual(facts["scope_violations"], [])
        self.assertEqual(facts["advisory_divergence"], ["lib/other.py"])

    def test_completed_claimed_path_missing_from_sealed_diff_is_failure(self) -> None:
        self._accept("completed")
        seal = self.seal(())
        self.assertEqual(seal["outcome"], "failure")
        self.assertEqual(
            self.evidence(seal)["claimed-but-unsealed"],
            ["lib/control/orchestration/change.py"],
        )
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        self.assertEqual(len(attempts), 2)
        retry = next(item for item in attempts if item["attempt_id"] != self.attempt["attempt_id"])
        self.assertEqual(retry["state"], "allocated")
        self.assertEqual(
            self.store.read_node(self.initiative_id, self.node["node_id"])["state"],
            "ready",
        )

    def test_process_kind_prefers_structured_facts_and_fails_closed(self) -> None:
        structured = {
            "state": "exited", "runs": [{"evidence": [{
                "source": "process", "outcome": "missing",
                "detail": "process ended", "state": "exited", "stale": False,
            }]}],
        }
        kind, facts = _process_kind(structured)
        self.assertEqual((kind, facts["exit_status"]), ("normal", 0))
        unknown = {
            "state": None, "runs": [{"evidence": [{
                "source": "process", "outcome": "missing",
                "detail": "unknown terminal prose", "state": None, "stale": False,
            }]}],
        }
        with self.assertRaisesRegex(SealError, "structured exit fact"):
            _process_kind(unknown)

    def test_path_heavy_failure_bounds_event_and_retains_full_evidence(self) -> None:
        paths = [
            f"docs/generated/section-{index:04d}/content-file.md"
            for index in range(500)
        ]
        self._accept("completed", files=[paths[0]])
        seal = self.seal(tuple(_entry(path, "x") for path in paths))
        self.assertEqual(seal["outcome"], "failure")
        event = next(
            item for item in self.store.list_events_snapshot(self.initiative_id)
            if item["type"] == "seal-published"
        )
        self.assertEqual(len(event["payload"]["scope_violations"]), 33)
        self.assertEqual(
            event["payload"]["scope_violations"][-1], "truncated: 468 more",
        )
        facts = self.evidence(seal)
        self.assertEqual(facts["scope_violations"], paths)
        self.assertEqual(self.seal(tuple(_entry(path, "x") for path in paths)), seal)

    def test_seal_caps_six_hundred_changed_paths_and_binds_full_list(self) -> None:
        paths = [
            f"lib/control/orchestration/gen/f{index:04d}.py"
            for index in range(600)
        ]
        self._accept("completed", files=[paths[0]])
        seal = self.seal(tuple(_entry(path, "x") for path in paths))
        self.assertEqual(seal["outcome"], "success")
        self.assertEqual(len(seal["changed_paths"]), 512)
        self.assertEqual(len(seal["cumulative_changed_paths"]), 512)
        self.assertEqual(seal["changed_paths_truncated"], 88)
        self.assertEqual(seal["cumulative_changed_paths_truncated"], 88)
        canonical = json.dumps(
            paths, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        self.assertEqual(
            seal["changed_paths_digest"], hashlib.sha256(canonical).hexdigest(),
        )
        self.assertEqual(
            seal["cumulative_changed_paths_digest"],
            hashlib.sha256(canonical).hexdigest(),
        )
        evidence = self.evidence(seal)
        self.assertEqual(evidence["changed_paths"], paths[:512])
        self.assertEqual(evidence["changed_paths_truncated"], 88)
        self.assertEqual(
            evidence["changed_paths_digest"], hashlib.sha256(canonical).hexdigest(),
        )

    def test_three_thousand_paths_publish_with_bounded_evidence(self) -> None:
        paths = [
            f"lib/control/orchestration/generated/f{index:04d}.py"
            for index in range(3000)
        ]
        self._accept("completed", files=[paths[0]])
        seal = self.seal(tuple(_entry(path, "x") for path in paths))
        self.assertEqual(seal["outcome"], "success")
        self.assertEqual(seal["changed_paths_truncated"], 2488)
        self.assertEqual(seal["cumulative_changed_paths_truncated"], 2488)
        evidence_record = self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )
        self.assertLessEqual(len(evidence_record["summary"].encode()), 128 * 1024)
        evidence = json.loads(evidence_record["summary"])
        canonical = json.dumps(
            paths, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        for field in (
            "changed_paths", "cumulative_changed_paths", "advisory_divergence",
            "scope_violations", "claimed-but-unsealed",
        ):
            self.assertIn(f"{field}_truncated", evidence)
            self.assertIn(f"{field}_digest", evidence)
        self.assertGreater(evidence["changed_paths_truncated"], 0)
        self.assertEqual(evidence["changed_paths_digest"], digest)
        self.assertEqual(evidence["cumulative_changed_paths_digest"], digest)
        self.assertEqual(self.seal(tuple(_entry(path, "x") for path in paths)), seal)

    def test_restart_after_preparing_and_after_publish_is_effect_once_and_write_once(self) -> None:
        self._accept("completed")
        entries = (_entry("lib/control/orchestration/change.py", "x"),)
        for target in ("preparing", "published"):
            def die(phase, _record, expected=target):
                if phase == expected:
                    raise SimulatedDeath(expected)

            with self.assertRaises(SimulatedDeath):
                self.seal(entries, phase_hook=die)
            if target == "preparing":
                self.assertEqual(self.store.list_seals_snapshot(self.initiative_id), [])
            else:
                break
        seal = self.seal(entries)
        path = self.config.initiatives_dir / self.initiative_id / "seals" / f"{seal['seal_id']}.json"
        before = path.read_bytes()
        replay = self.seal(entries)
        self.assertEqual(replay, seal)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.store.list_seals_snapshot(self.initiative_id)), 1)

    def test_restart_repairs_missing_seal_preparing_event(self) -> None:
        self._accept("completed")
        entries = (_entry("lib/control/orchestration/change.py", "x"),)
        with mock.patch(
            "lib.control.orchestration.seals.append_event",
            side_effect=StoreError("event write failed"),
        ):
            with self.assertRaises(StoreError):
                self.seal(entries)
        seal = self.seal(entries)
        preparing = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "seal-preparing"
            and seal["seal_id"] in event["subject_ids"]
        ]
        self.assertEqual(len(preparing), 1)

    def test_later_commit_drift_records_event_pauses_and_never_rewrites_seal(self) -> None:
        self._accept("completed")
        adapter = SealJj(
            self.task, self.origin_digest,
            (_entry("lib/control/orchestration/change.py", "x"),),
        )
        seal = self.seal(adapter.entries, jj=adapter)
        seal_path = self.config.initiatives_dir / self.initiative_id / "seals" / f"{seal['seal_id']}.json"
        before = seal_path.read_bytes()
        adapter.current_commit = "e" * 40
        control = mock.Mock()
        control.peek.return_value = self.task
        first = reconcile_seal_drift(
            self.store, self.initiative_id, control_store=control, jj=adapter,
        )
        second = reconcile_seal_drift(
            self.store, self.initiative_id, control_store=control, jj=adapter,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(seal_path.read_bytes(), before)

    def test_pruned_archived_workspace_is_not_seal_drift(self) -> None:
        self._accept("completed")
        adapter = SealJj(
            self.task, self.origin_digest,
            (_entry("lib/control/orchestration/change.py", "x"),),
        )
        seal = self.seal(adapter.entries, jj=adapter)
        pruned = copy.deepcopy(self.task)
        pruned["lifecycle"] = "archived"
        pruned["jj"]["workspace_path"] = str(self.root / "reclaimed" / "gone")
        control = mock.Mock()
        control.peek.return_value = pruned
        adapter.current_commit = "e" * 40  # would count as drift if inspected
        PruneRecordStore(self.config.control).write(
            pruned["task_id"], {"workspace_removed": True},
        )
        findings = reconcile_seal_drift(
            self.store, self.initiative_id, control_store=control, jj=adapter,
        )
        self.assertEqual(findings, [])
        self.assertEqual(self.initiative()["state"], "running")
        self.assertFalse(any(
            event["type"] == "seal-drift-detected"
            and seal["seal_id"] in event["subject_ids"]
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))
        # An archived task whose directory is gone WITHOUT a prune record
        # (moved aside, deleted by hand) is still drift.
        PruneRecordStore(self.config.control).path(pruned["task_id"]).unlink()
        with mock.patch.object(
            adapter, "inspect_workspace", side_effect=JjError("workspace missing"),
        ):
            findings = reconcile_seal_drift(
                self.store, self.initiative_id, control_store=control, jj=adapter,
            )
        self.assertEqual(len(findings), 1)
        self.assertIn("unavailable", findings[0]["reason"])

    def test_control_task_seal_cli_inspects_by_task_and_attempt(self) -> None:
        self._accept("completed")
        seal = self.seal((_entry("lib/control/orchestration/change.py", "x"),))
        for identity in (seal["task_id"], seal["attempt_id"]):
            output = StringIO()
            with redirect_stdout(output):
                status = control_main(
                    ["task", "seal", identity, "--json"], env=self.env,
                )
            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["seal"]["seal_id"], seal["seal_id"])
            self.assertEqual(payload["verification"], "reproduced")


if __name__ == "__main__":
    unittest.main()
