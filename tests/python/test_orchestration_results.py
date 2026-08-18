from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from lib.control.jj import WorkspaceIdentity
from lib.control.cli import main as control_main
from lib.control.reconcile import Evidence
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.results import (
    ResultError,
    ResultRefused,
    publish_result,
    reconcile_publications,
)
from lib.control.orchestration.reconcile import reconcile_live
from lib.control.orchestration.store import InitiativeStore
from lib.control.store import StoreError, TaskStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text


class SimulatedDeath(BaseException):
    pass


class OwnershipJj:
    def __init__(self, task: dict):
        self.task = task

    def inspect_workspace(self, path, name, *, snapshot=False, require_empty=True):
        jj = self.task["jj"]
        return WorkspaceIdentity(
            name=name,
            change_id=jj["change_id"],
            commit_id=jj["working_commit_id"],
            parent_commit_ids=(jj["base_commit_id"],),
            description="test",
        )


class OrchestrationResultPublicationTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._dispatch()

    def _dispatch(self) -> None:
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            workspace = self.config.control.workspace_root / payload["task"]["task_id"]
            payload["task"]["jj"]["workspace_path"] = str(workspace)
            payload["workspace"]["path"] = str(workspace)
            self.task = payload["task"]
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
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.repo.chmod(0o700)
        self.workspace = Path(self.task["jj"]["workspace_path"])
        (self.workspace / "lib/control/orchestration").mkdir(parents=True)
        current = self.workspace
        data_home = os.path.realpath(self.env["XDG_DATA_HOME"])
        while os.path.realpath(current) != data_home:
            current.chmod(0o700)
            current = current.parent
        (self.workspace / "lib/control/orchestration/result.py").write_text("changed\n")
        TaskStore(self.config.control).save(self.task)
        self.jj = OwnershipJj(self.task)
        self.managed = {
            **self.env,
            "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task["task_id"],
            "ASHA_CONTROL_RUN_ID": self.task["runs"][0]["run_id"],
        }

    def body(
        self,
        *,
        publication_id: str | None = None,
        supersedes: str | None = None,
        files: list[str] | None = None,
        status: str = "completed",
        summary: str = "done",
    ) -> dict:
        return {
            "contract": "asha.orchestration-result.v1",
            "publication_id": publication_id or str(uuid.uuid4()),
            "supersedes_result_id": supersedes,
            "initiative_id": self.initiative_id,
            "node_id": self.attempt["node_id"],
            "attempt_id": self.attempt["attempt_id"],
            "task_id": self.task["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": status,
            "summary": summary,
            "files_changed": files if files is not None else ["lib/control/orchestration/result.py"],
            "verification_attestations": [],
            "concerns": [],
            "follow_up": [],
            "published_at": now_text(),
        }

    def publish(self, body: dict, **kwargs):
        return publish_result(
            self.store, body, self.managed, jj=self.jj, **kwargs,
        )

    def test_publication_restarts_from_every_durable_phase_and_replays_once(self) -> None:
        current = None
        for phase in ("reserved", "validating", "persisting", "completed"):
            body = self.body(supersedes=current, summary=f"death after {phase}")

            def die(observed, _record, target=phase):
                if observed == target:
                    raise SimulatedDeath(target)

            with self.assertRaises(SimulatedDeath):
                self.publish(body, phase_hook=die)
            receipt = self.publish(body)
            self.assertEqual(receipt["phase"], "completed")
            current = receipt["result_id"]
            before = len(self.store.list_results_snapshot(self.initiative_id))
            replay = self.publish(copy.deepcopy(body))
            self.assertEqual(replay, receipt)
            self.assertEqual(
                len(self.store.list_results_snapshot(self.initiative_id)), before,
            )
        self.assertEqual(len(self.store.list_results_snapshot(self.initiative_id)), 4)
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["result_id"],
            current,
        )

    def test_same_publication_id_with_changed_bytes_is_refused(self) -> None:
        body = self.body()
        self.publish(body)
        changed = copy.deepcopy(body)
        changed["summary"] = "different canonical bytes"
        with self.assertRaisesRegex(ResultRefused, "different canonical bytes"):
            self.publish(changed)
        self.assertEqual(len(self.store.list_results_snapshot(self.initiative_id)), 1)

    def test_semantic_refusal_occurs_after_durable_reservation(self) -> None:
        body = self.body(summary="x" * 10000)

        def die(phase, _record):
            if phase == "reserved":
                raise SimulatedDeath

        with self.assertRaises(SimulatedDeath):
            self.publish(body, phase_hook=die)
        self.assertEqual(
            self.store.read_result_publication(
                self.initiative_id, body["publication_id"],
            )["state"],
            "reserved",
        )
        receipt = self.publish(body)
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("summary", receipt["refusal"])

    def test_completed_phase_repairs_crash_between_attempt_binding_and_event(self) -> None:
        body = self.body()
        with mock.patch(
            "lib.control.orchestration.results.append_event", side_effect=SimulatedDeath,
        ):
            with self.assertRaises(SimulatedDeath):
                self.publish(body)
        journal = self.store.read_result_publication(
            self.initiative_id, body["publication_id"],
        )
        self.assertEqual(journal["state"], "completed")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["result_id"],
            journal["result_id"],
        )
        receipt = self.publish(body)
        self.assertEqual(receipt["phase"], "completed")
        self.assertTrue(any(
            event["type"] == "result-published"
            and receipt["result_id"] in event["subject_ids"]
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))

    def test_worker_is_not_acknowledged_until_attempt_and_event_are_durable(self) -> None:
        body = self.body()
        with mock.patch(
            "lib.control.orchestration.results.append_event",
            side_effect=StoreError("event write failed"),
        ):
            with self.assertRaisesRegex(ResultError, "not durably accepted"):
                self.publish(body)
        receipt = self.publish(body)
        self.assertEqual(receipt["phase"], "completed")

    def test_conflicting_preallocated_result_becomes_indeterminate_and_pauses(self) -> None:
        body = self.body()

        def die(phase, _record):
            if phase == "persisting":
                raise SimulatedDeath

        with self.assertRaises(SimulatedDeath):
            self.publish(body, phase_hook=die)
        journal = self.store.read_result_publication(
            self.initiative_id, body["publication_id"],
        )
        conflicting = copy.deepcopy(body)
        conflicting.update({
            "result_id": journal["result_id"],
            "payload_digest": journal["payload_digest"],
            "summary": "conflicting bytes at reserved identity",
        })
        self.store.save_result(self.initiative_id, conflicting)
        records = reconcile_publications(
            self.store, self.initiative_id,
            control_store=TaskStore(self.config.control), jj=self.jj,
        )
        self.assertEqual(records[0]["state"], "indeterminate")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["state"],
            "indeterminate",
        )
        self.assertEqual(self.initiative()["state"], "paused")
        control = mock.Mock()
        control.peek.return_value = self.task
        control.list.return_value = [self.task]
        adapters = mock.Mock()
        adapters.tmux.return_value = Evidence("tmux", "match", "owned")
        adapters.process.return_value = Evidence("process", "match", "live")
        adapters.jj.return_value = Evidence("jj", "match", "owned")
        adapters.event.return_value = Evidence("event", "missing", "none")
        observed = reconcile_live(
            self.store, self.initiative_id, control_store=control,
            adapters_factory=lambda _task: adapters,
        )
        self.assertEqual(
            observed["observations"][0]["control_state"],
            "publication-indeterminate",
        )
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["state"],
            "indeterminate",
        )

    def test_managed_environment_and_task_run_binding_are_mandatory(self) -> None:
        body = self.body()
        with self.assertRaisesRegex(ResultRefused, "ASHA_CONTROL_MANAGED"):
            publish_result(self.store, body, self.env, jj=self.jj)
        wrong = {**self.managed, "ASHA_CONTROL_RUN_ID": str(uuid.uuid4())}
        with self.assertRaisesRegex(ResultRefused, "primary run"):
            publish_result(self.store, body, wrong, jj=self.jj)
        self.assertEqual(self.store.list_result_publications_snapshot(self.initiative_id), [])

    def test_path_validation_refuses_noncanonical_and_symlink_traversal(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "linked").symlink_to(outside, target_is_directory=True)
        symlink_body = self.body(files=["linked/escaped.py"])
        receipt = self.publish(symlink_body)
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("symlink", receipt["refusal"])

        lexical = self.body(files=["../escaped.py"])
        receipt = self.publish(lexical)
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("canonical relative", receipt["refusal"])

    def test_files_changed_refuses_existing_directory_claims(self) -> None:
        directory = self.workspace / "lib/control/orchestration/generated"
        directory.mkdir()
        receipt = self.publish(self.body(
            files=["lib/control/orchestration/generated"],
        ))
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("must name files, not directories", receipt["refusal"])
        self.assertEqual(
            self.store.read_attempt(
                self.initiative_id, self.attempt["attempt_id"],
            )["state"],
            "running",
        )

    def test_files_changed_allows_a_deleted_file_path(self) -> None:
        receipt = self.publish(self.body(
            files=["lib/control/orchestration/deleted.py"],
        ))
        self.assertEqual(receipt["phase"], "completed")

    def test_only_current_result_can_be_superseded_before_sealing(self) -> None:
        first = self.publish(self.body())
        correction = self.publish(self.body(
            supersedes=first["result_id"], summary="corrected",
        ))
        self.assertEqual(correction["phase"], "completed")
        wrong = self.publish(self.body(
            supersedes=first["result_id"], summary="stale correction",
        ))
        self.assertEqual(wrong["phase"], "refused")
        self.assertIn("current accepted result", wrong["refusal"])
        attempt = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        self.assertEqual(attempt["result_id"], correction["result_id"])

    def test_superseded_completed_publication_reconciles_and_replays_as_durable(self) -> None:
        body = self.body(summary="A")
        first = self.publish(body)
        second = self.publish(self.body(
            supersedes=first["result_id"], summary="B",
        ))
        self.assertEqual(
            reconcile_publications(
                self.store, self.initiative_id,
                control_store=TaskStore(self.config.control), jj=self.jj,
            ),
            [],
        )
        cold_jj = mock.Mock()
        self.assertEqual(
            publish_result(
                self.store, copy.deepcopy(body), self.managed, jj=cold_jj,
            ),
            first,
        )
        cold_jj.inspect_workspace.assert_not_called()
        attempt = self.store.read_attempt(
            self.initiative_id, self.attempt["attempt_id"],
        )
        self.assertEqual(attempt["result_id"], second["result_id"])
        self.assertEqual(attempt["state"], "reported")
        self.assertEqual(self.initiative()["state"], "running")

    def test_control_task_report_and_result_cli_routes_publish_and_inspect(self) -> None:
        body = self.body()
        path = self.root / "RESULT.json"
        path.write_text(json.dumps(body))
        output = StringIO()
        with mock.patch(
            "lib.control.orchestration.results.JjAdapter", return_value=self.jj,
        ), redirect_stdout(output):
            status = control_main(
                ["task", "report", "--file", str(path), "--json"],
                env=self.managed,
            )
        self.assertEqual(status, 0, output.getvalue())
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["phase"], "completed")
        output = StringIO()
        with redirect_stdout(output):
            status = control_main(
                ["task", "result", self.task["task_id"], "--json"],
                env=self.env,
            )
        self.assertEqual(status, 0)
        inspected = json.loads(output.getvalue())
        self.assertEqual([item["result_id"] for item in inspected["results"]], [receipt["result_id"]])


if __name__ == "__main__":
    unittest.main()
