from __future__ import annotations

import copy
import json
import os
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from dataclasses import replace
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
    locate_task_binding,
    locate_task_binding_now,
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
        asha_home = os.path.realpath(self.env["ASHA_HOME"])
        while os.path.realpath(current) != asha_home:
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

    def test_publication_waits_for_a_link_written_after_worker_launch(self) -> None:
        link = self.store.read_link(self.initiative_id, self.attempt["attempt_id"])
        link_path = (
            self.store.config.initiatives_dir / self.initiative_id
            / "links" / f"{self.attempt['attempt_id']}.json"
        )
        link_path.unlink()
        self.store.config = replace(self.store.config, link_grace_seconds=3)
        errors: list[BaseException] = []

        def delayed_link() -> None:
            try:
                time.sleep(1)
                self.store.save_link(self.initiative_id, link)
            except BaseException as exc:
                errors.append(exc)

        writer = threading.Thread(target=delayed_link)
        writer.start()
        started = time.monotonic()
        try:
            receipt = self.publish(self.body())
        finally:
            writer.join(3)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertGreaterEqual(time.monotonic() - started, 0.75)
        self.assertEqual(receipt["phase"], "completed")

    def test_missing_link_refusal_says_report_may_be_retried(self) -> None:
        link_path = (
            self.store.config.initiatives_dir / self.initiative_id
            / "links" / f"{self.attempt['attempt_id']}.json"
        )
        link_path.unlink()
        self.store.config = replace(self.store.config, link_grace_seconds=1)
        with mock.patch(
            "lib.control.orchestration.results.time.monotonic",
            side_effect=[0.0, 1.0],
        ):
            with self.assertRaisesRegex(
                ResultRefused, "not yet linked.*report may be retried",
            ):
                locate_task_binding(self.store.config, self.task["task_id"])

    def test_nonblocking_binding_lookup_returns_owned_or_none_without_grace_wait(self) -> None:
        binding = locate_task_binding_now(
            self.store.config, self.task["task_id"],
            control_store=TaskStore(self.config.control),
        )
        self.assertEqual(binding[1]["initiative_id"], self.initiative_id)
        self.assertEqual(binding[2]["control_task_id"], self.task["task_id"])

        missing = str(uuid.uuid4())
        started = time.monotonic()
        self.assertIsNone(locate_task_binding_now(self.store.config, missing))
        self.assertLess(time.monotonic() - started, 0.25)

    def test_nonblocking_binding_lookup_refuses_skipped_and_identity_drift(self) -> None:
        link = self.store.read_link(self.initiative_id, self.attempt["attempt_id"])
        changed = copy.deepcopy(self.task)
        changed["label"] = "foreign replacement"
        with self.assertRaisesRegex(ResultRefused, "identity"):
            locate_task_binding_now(
                self.store.config, self.task["task_id"],
                control_store=mock.Mock(peek=mock.Mock(return_value=changed)),
            )
        self.assertIsNotNone(link)

        fake = mock.Mock()
        fake.list_initiatives.return_value = []
        fake.skipped = [{"name": "bad", "reason": "malformed"}]
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore", return_value=fake,
        ):
            with self.assertRaisesRegex(ResultRefused, "unreadable"):
                locate_task_binding_now(self.store.config, str(uuid.uuid4()))

        duplicate_store = mock.Mock()
        duplicate_store.skipped = []
        duplicate_store.list_initiatives.return_value = [
            {"initiative_id": self.initiative_id},
        ]
        duplicate_store.list_attempts_snapshot.return_value = [self.attempt]
        duplicate_store.list_links_snapshot.return_value = [link, copy.deepcopy(link)]
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore",
            return_value=duplicate_store,
        ):
            with self.assertRaisesRegex(ResultRefused, "more than one"):
                locate_task_binding_now(self.store.config, self.task["task_id"])

        interrupted = mock.Mock()
        interrupted.skipped = []
        interrupted.list_initiatives.return_value = [
            {"initiative_id": self.initiative_id},
        ]
        interrupted.list_links_snapshot.return_value = []
        interrupted.list_attempts_snapshot.return_value = [self.attempt]
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore",
            return_value=interrupted,
        ):
            with self.assertRaisesRegex(ResultRefused, "link is missing"):
                locate_task_binding_now(self.store.config, self.task["task_id"])

    def test_nonblocking_binding_lookup_binds_the_observed_reservation_to_its_link(self) -> None:
        link = self.store.read_link(self.initiative_id, self.attempt["attempt_id"])
        node = self.store.read_node(self.initiative_id, self.attempt["node_id"])
        later_attempt = copy.deepcopy(self.attempt)
        later_attempt["attempt_id"] = str(uuid.uuid4())
        later_link = copy.deepcopy(link)
        later_link["attempt_id"] = later_attempt["attempt_id"]

        interleaved = mock.Mock()
        interleaved.skipped = []
        interleaved.list_initiatives.return_value = [
            {"initiative_id": self.initiative_id},
        ]
        interleaved.list_attempts_snapshot.return_value = [self.attempt]
        interleaved.list_links_snapshot.return_value = [later_link]
        interleaved.read_attempt.return_value = later_attempt
        interleaved.read_node.return_value = node
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore",
            return_value=interleaved,
        ):
            with self.assertRaisesRegex(
                ResultRefused, "reservation.*link.*disagree",
            ):
                locate_task_binding_now(
                    self.store.config, self.task["task_id"],
                    control_store=TaskStore(self.config.control),
                )

        later_initiative_id = str(uuid.uuid4())
        initiative_interleaved = mock.Mock()
        initiative_interleaved.skipped = []
        initiative_interleaved.list_initiatives.return_value = [
            {"initiative_id": self.initiative_id},
            {"initiative_id": later_initiative_id},
        ]
        initiative_interleaved.list_attempts_snapshot.side_effect = (
            lambda initiative_id: (
                [self.attempt] if initiative_id == self.initiative_id else []
            )
        )
        cross_initiative_link = copy.deepcopy(later_link)
        cross_initiative_link["initiative_id"] = later_initiative_id
        initiative_interleaved.list_links_snapshot.side_effect = (
            lambda initiative_id: (
                [cross_initiative_link]
                if initiative_id == later_initiative_id else []
            )
        )
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore",
            return_value=initiative_interleaved,
        ):
            with self.assertRaisesRegex(
                ResultRefused, "reservation.*link.*disagree",
            ):
                locate_task_binding_now(
                    self.store.config, self.task["task_id"],
                    control_store=TaskStore(self.config.control),
                )

        corresponding = mock.Mock()
        corresponding.skipped = []
        corresponding.list_initiatives.return_value = [
            {"initiative_id": self.initiative_id},
        ]
        corresponding.list_attempts_snapshot.return_value = [later_attempt]
        corresponding.list_links_snapshot.return_value = [later_link]
        corresponding.read_attempt.return_value = later_attempt
        corresponding.read_node.return_value = node
        with mock.patch(
            "lib.control.orchestration.results.InitiativeStore",
            return_value=corresponding,
        ):
            binding = locate_task_binding_now(
                self.store.config, self.task["task_id"],
                control_store=TaskStore(self.config.control),
            )

        self.assertEqual(binding[2]["attempt_id"], later_attempt["attempt_id"])

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
