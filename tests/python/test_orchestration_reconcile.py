from __future__ import annotations

import json
import unittest
import uuid
from unittest import mock

from lib.control.reconcile import Evidence, UnavailableAdapters
from lib.control.store import StoreError, task_digest
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.reconcile import (
    _staged_result_candidate, reconcile_live, reconcile_nodes,
)
from lib.control.orchestration.links import control_task_identity_digest
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_control_config_model import task_record


class OrchestrationReconcileTests(unittest.TestCase):
    def test_unlinked_node_is_reported_without_control_reads(self) -> None:
        store = mock.Mock()
        store.list_links_snapshot.return_value = []
        store.list_attempts_snapshot.return_value = []
        control = mock.Mock()
        result = reconcile_nodes("11111111-1111-4111-8111-111111111111", [{"node_id": "work"}], store=store, control_store=control)
        self.assertEqual(result[0]["control_state"], "unlinked")
        control.peek.assert_not_called()

    def test_matching_mismatching_and_missing_control_records_are_observations_only(self) -> None:
        task = task_record(
            task_id="22222222-2222-4222-8222-222222222222",
            repository_root="/tmp/source", workspace_path="/tmp/workspace",
        )
        link = {
            "node_id": "work", "attempt_id": "33333333-3333-4333-8333-333333333333",
            "control_task_id": task["task_id"],
            "control_task_identity_digest": control_task_identity_digest(task),
            "control_task_record_digest": task_digest(task),
        }
        store = mock.Mock()
        store.list_links_snapshot.return_value = [link]
        store.list_attempts_snapshot.return_value = []
        store.config.control = mock.Mock()
        control = mock.Mock()
        control.peek.return_value = task
        matched = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertTrue(matched["digest_match"])
        changed = dict(link)
        changed["control_task_identity_digest"] = "0" * 64
        store.list_links_snapshot.return_value = [changed]
        stale = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertEqual(stale["control_state"], "stale")
        control.peek.side_effect = StoreError("task not found")
        missing = reconcile_nodes(
            "11111111-1111-4111-8111-111111111111", [{"node_id": "work"}],
            store=store, control_store=control, adapters_factory=lambda _: UnavailableAdapters(),
        )[0]
        self.assertEqual(missing["control_state"], "stale")


class WorkingAdapters:
    def tmux(self, task, run):
        return Evidence("tmux", "match", "owned pane matched")

    def process(self, task, run):
        return Evidence("process", "match", "process identity matched")

    def jj(self, task):
        return Evidence("jj", "match", "workspace identity matched")

    def event(self, task, run):
        return Evidence("event", "match", "semantic event", state="working")


class OrchestrationReconcileCorruptRecordTests(ExecutionFixture, unittest.TestCase):
    """One unreadable subrecord must not wedge the whole reconciliation pass."""

    def dispatch_one(self):
        tasks = []

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            tasks.append(payload["task"])
            return 0, json.dumps(payload).encode(), b""

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            submit_action(self.store, self.initiative_id, document)
        return tasks[0]

    def clobber_one_ingestion_record(self) -> str:
        """Copy a live record over a second filename, as `json.tool` once did."""
        directory = (
            self.config.initiatives_dir / self.initiative_id / "result-ingestions"
        )
        live = next(iter(sorted(directory.glob("*.json"))))
        clobbered = directory / f"{uuid.uuid4()}.json"
        clobbered.write_bytes(live.read_bytes())
        clobbered.chmod(0o600)
        return clobbered.name

    def reconcile(self, task):
        control = mock.Mock()
        control.peek.return_value = task
        control.list.return_value = [task]
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            return reconcile_live(
                self.store, self.initiative_id, control_store=control,
                adapters_factory=lambda _task: WorkingAdapters(),
            )

    def test_corrupt_ingestion_record_is_named_and_the_pass_still_completes(self) -> None:
        task = self.dispatch_one()
        attempt_id = self.store.list_attempts_snapshot(self.initiative_id)[0]["attempt_id"]
        name = self.clobber_one_ingestion_record()

        result = self.reconcile(task)

        # The rest of the pass ran: the live attempt was still observed.
        self.assertEqual(
            [item["attempt_id"] for item in result["observations"]], [attempt_id],
        )
        self.assertEqual(
            [item["control_state"] for item in result["observations"]], ["working"],
        )
        # And the corruption is named rather than swallowed.
        corrupt = [
            probe for probe in result["probes"]
            if probe["name"] == "result-ingestion-records"
        ]
        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0]["outcome"], "corrupt")
        self.assertIn(name, corrupt[0]["detail"])
        self.assertIn("does not match its filename", corrupt[0]["detail"])
        # Reconciliation reports; it never repairs storage by itself.
        directory = (
            self.config.initiatives_dir / self.initiative_id / "result-ingestions"
        )
        self.assertTrue((directory / name).is_file())

    def test_clean_storage_reports_no_corruption_probe(self) -> None:
        task = self.dispatch_one()

        result = self.reconcile(task)

        self.assertEqual(
            [probe["name"] for probe in result["probes"]], ["control-task-list"],
        )

    def test_unnamed_ingestion_storage_failure_is_still_fatal(self) -> None:
        task = self.dispatch_one()
        with mock.patch(
            "lib.control.orchestration.reconcile.ingest_pending_results",
            side_effect=StoreError("initiative transaction lock is unavailable"),
        ):
            with self.assertRaisesRegex(StoreError, "transaction lock is unavailable"):
                self.reconcile(task)

    def test_unreadable_survey_reports_the_original_ingestion_failure(self) -> None:
        task = self.dispatch_one()
        with mock.patch(
            "lib.control.orchestration.reconcile.ingest_pending_results",
            side_effect=StoreError("initiative storage directory is missing"),
        ), mock.patch.object(
            type(self.store), "list_result_ingestions_snapshot",
            side_effect=StoreError("cannot list result-ingestions records"),
        ):
            # A survey that cannot run either names nothing, so the failure the
            # operator sees stays the one reconciliation actually hit.
            with self.assertRaisesRegex(StoreError, "storage directory is missing"):
                self.reconcile(task)

    def test_staged_candidate_fails_closed_on_an_unreadable_record(self) -> None:
        self.dispatch_one()
        attempt_id = self.store.list_attempts_snapshot(self.initiative_id)[0]["attempt_id"]
        other_attempt = "33333333-3333-4333-8333-333333333333"
        # No outbox file exists, so a readable store answers "nothing staged".
        self.assertFalse(
            _staged_result_candidate(self.store, self.initiative_id, attempt_id),
        )
        self.clobber_one_ingestion_record()
        # An unreadable record cannot be attributed to an attempt, so staging
        # is ambiguous for every attempt and the answer that preserves a
        # possible result wins over the one that expires it.
        self.assertTrue(
            _staged_result_candidate(self.store, self.initiative_id, attempt_id),
        )
        self.assertTrue(
            _staged_result_candidate(self.store, self.initiative_id, other_attempt),
        )


if __name__ == "__main__":
    unittest.main()
