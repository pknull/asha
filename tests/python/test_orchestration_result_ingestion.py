from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from lib.control.jj import ImmutableTree, JjAdapter, WorkspaceIdentity
from lib.control.reconcile import Evidence, LiveAdapters
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.cli import task_main
from lib.control.orchestration.coordinator import CoordinatorError
from lib.control.orchestration.ingestion import (
    IngestionRefused,
    _save_verification_evidence,
    ingest_result,
    reserve_result_ingestion,
    result_ingestion_id,
    stage_result,
)
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.results import ResultRefused, publish_result
from lib.control.orchestration.seals import prepare_and_publish_seal
from lib.control.orchestration.reconcile import reconcile_live
from lib.control.store import TaskStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text


class SnapshotJj:
    def __init__(self, task: dict):
        self.task = task
        self.snapshotted = False
        self.base = task["jj"]["base_commit_id"]
        self.before = task["jj"]["working_commit_id"]
        self.after = "d" * 40
        self.base_tree = ImmutableTree(
            commit_id=self.base, digest="c" * 64, entries=(),
        )
        self.final_tree = ImmutableTree(
            commit_id=self.after, digest="e" * 64,
            entries=(("lib/control/orchestration/result.py", "100644", "f" * 40),),
        )

    def inspect_workspace(
        self, path, name, *, snapshot=False, require_empty=True,
        exclude_control_transport=False,
    ) -> WorkspaceIdentity:
        if snapshot:
            if not exclude_control_transport:
                raise AssertionError("controller snapshot did not exclude its transport")
            self.snapshotted = True
        commit = self.after if self.snapshotted else self.before
        return WorkspaceIdentity(
            name=name,
            change_id=self.task["jj"]["change_id"],
            commit_id=commit,
            parent_commit_ids=(self.base,),
            description="test",
        )

    def immutable_tree(self, _repository, commit_id):
        if commit_id == self.after:
            return self.final_tree
        if commit_id == self.base:
            return self.base_tree
        raise AssertionError(f"unexpected commit {commit_id}")


class ResultIngestionTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            workspace = self.config.control.workspace_root / payload["task"]["task_id"]
            payload["task"]["jj"]["workspace_path"] = str(workspace)
            payload["task"]["jj"]["base_commit_id"] = "b" * 40
            payload["workspace"]["path"] = str(workspace)
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
        self.repo.chmod(0o700)
        self.workspace = Path(self.task["jj"]["workspace_path"])
        (self.workspace / "lib/control/orchestration").mkdir(parents=True)
        (self.workspace / "lib/control/orchestration/result.py").write_text("changed\n")
        current = self.workspace
        home = Path(self.env["ASHA_HOME"])
        while current != home:
            current.chmod(0o700)
            current = current.parent
        TaskStore(self.config.control).save(self.task)
        self.jj = SnapshotJj(self.task)
        self.ingestion = self.store.read_result_ingestion(
            self.initiative_id, result_ingestion_id(self.attempt["attempt_id"]),
        )
        self.managed = {
            **self.env,
            "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task["task_id"],
            "ASHA_CONTROL_RUN_ID": self.task["runs"][0]["run_id"],
            "ASHA_CONTROL_RESULT_INGESTION_ID": self.ingestion["ingestion_id"],
            "ASHA_CONTROL_RESULT_OUTBOX": str(
                self.workspace.joinpath(*self.ingestion["outbox_path"].split("/"))
            ),
        }

    def body(self, **changes) -> dict:
        value = {
            "contract": "asha.orchestration-result.v1",
            "publication_id": str(uuid.uuid4()),
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": self.attempt["node_id"],
            "attempt_id": self.attempt["attempt_id"],
            "task_id": self.task["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": "completed",
            "summary": "staged safely",
            "files_changed": ["lib/control/orchestration/result.py"],
            "verification_attestations": [],
            "concerns": [],
            "follow_up": [],
            "published_at": now_text(),
        }
        value.update(changes)
        return value

    def stage(self, body=None):
        return stage_result(self.config, body or self.body(), self.managed)

    def verifier(self, store, ingestion, _task, _body, commit, tree, **_kwargs):
        return [_save_verification_evidence(
            store, self.initiative_id, ingestion["ingestion_id"], {
                "kind": "snapshot-integrity",
                "claimed_commit_id": commit,
                "claimed_tree_digest": tree,
                "status": "verified",
            },
        )]

    def ingest(self):
        return ingest_result(
            self.store, self.initiative_id, self.ingestion["ingestion_id"],
            control_store=TaskStore(self.config.control), jj=self.jj,
            terminal_reconciliation={"state": "exited"},
            verifier=self.verifier,
        )

    def test_worker_stages_reserved_candidate_without_authoritative_write(self) -> None:
        before = self.store.list_results_snapshot(self.initiative_id)
        receipt = self.stage()
        self.assertEqual(receipt["phase"], "staged")
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), before)
        self.assertEqual(
            self.store.read_result_ingestion(
                self.initiative_id, self.ingestion["ingestion_id"],
            )["state"],
            "reserved",
        )
        candidate = Path(receipt["outbox_path"])
        self.assertTrue(candidate.is_file())
        self.assertEqual(candidate.stat().st_mode & 0o777, 0o600)

    def test_reservation_replay_accepts_the_retained_state_machine_record(self) -> None:
        replayed = reserve_result_ingestion(
            self.store, self.initiative(),
            self.store.read_node(self.initiative_id, self.attempt["node_id"]),
            self.attempt,
            self.store.read_link(self.initiative_id, self.attempt["attempt_id"]),
            self.task,
        )
        self.assertEqual(replayed, self.ingestion)
        self.stage()
        accepted = self.ingest()
        completed = reserve_result_ingestion(
            self.store, self.initiative(),
            self.store.read_node(self.initiative_id, self.attempt["node_id"]),
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"]),
            self.store.read_link(self.initiative_id, self.attempt["attempt_id"]),
            self.task,
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["result_id"], accepted["result_id"])

    def test_reserved_worker_cannot_bypass_outbox_with_direct_publication(self) -> None:
        direct_env = {
            key: value for key, value in self.managed.items()
            if not key.startswith("ASHA_CONTROL_RESULT_")
        }
        with mock.patch(
            "lib.control.orchestration.results.caller_descends_from", return_value=True,
        ), self.assertRaisesRegex(ResultRefused, "direct authoritative publication"):
            publish_result(
                self.store, self.body(), direct_env, jj=self.jj,
                caller_pid=os.getpid(),
            )
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), [])

    def test_controller_cli_routes_the_unique_reservation_to_ingestion(self) -> None:
        completed = {
            "contract": "asha.orchestration-result-ingestion-receipt.v1",
            "ingestion_id": self.ingestion["ingestion_id"],
            "publication_id": str(uuid.uuid4()),
            "result_id": str(uuid.uuid4()),
            "phase": "completed",
            "refusal": None,
        }
        output = StringIO()
        with mock.patch(
            "lib.control.orchestration.cli.ingest_result", return_value=completed,
        ) as ingest, mock.patch(
            "lib.control.orchestration.cli.refuse_coordinator_pane",
        ) as refuse_coordinator, redirect_stdout(output):
            returncode = task_main(
                ["ingest", self.ingestion["ingestion_id"], "--json"],
                env=self.env,
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(output.getvalue()), completed)
        self.assertEqual(
            ingest.call_args.kwargs["ingester"],
            {
                "actor_kind": "controller", "actor_id": "task-ingest-cli",
                "coordinator_generation": None,
            },
        )
        refuse_coordinator.assert_called_once()

    def test_coordinator_ingest_refusal_is_a_typed_cli_error(self) -> None:
        error = StringIO()
        with mock.patch(
            "lib.control.orchestration.cli.require_live_coordinator",
            side_effect=CoordinatorError("no live coordinator generation"),
        ), redirect_stderr(error):
            returncode = task_main(
                ["ingest", self.ingestion["ingestion_id"], "--json"],
                env={
                    **self.env,
                    "ASHA_ORCHESTRATION_COORDINATOR_ID": str(uuid.uuid4()),
                },
            )
        self.assertEqual(returncode, 2)
        self.assertIn("no live coordinator generation", error.getvalue())

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
    def test_workspace_sandbox_reproduces_control_state_erofs_then_stages(self) -> None:
        result_file = self.workspace / ".asha" / "input-result.json"
        result_file.parent.mkdir(mode=0o700, exist_ok=True)
        result_file.write_text(json.dumps(self.body()))
        result_file.chmod(0o600)
        environment = {
            **os.environ, **self.managed,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        direct_program = """
import json, os
from pathlib import Path
from lib.control.orchestration.config import load_config
from lib.control.orchestration.results import publish_result, read_client_file
from lib.control.orchestration.store import InitiativeStore
body = read_client_file(Path(os.environ['RESULT_FILE']))
try:
    publish_result(InitiativeStore(load_config(os.environ)), body, os.environ)
except BaseException as exc:
    print(type(exc).__name__ + ': ' + str(exc))
    raise SystemExit(30 if 'Read-only file system' in str(exc) else 31)
raise SystemExit(0)
"""
        common = [
            "bwrap", "--die-with-parent", "--ro-bind", "/", "/",
            "--bind", str(self.workspace), str(self.workspace),
            "--proc", "/proc", "--dev-bind", "/dev", "/dev", "--",
            sys.executable, "-c",
        ]
        direct = subprocess.run(
            [*common, direct_program], env={**environment, "RESULT_FILE": str(result_file)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(direct.returncode, 30, direct.stdout + direct.stderr)
        self.assertIn("Read-only file system", direct.stdout)
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), [])
        stage_program = """
import json, os
from pathlib import Path
from lib.control.orchestration.config import load_config
from lib.control.orchestration.ingestion import stage_result
from lib.control.orchestration.results import read_client_file
body = read_client_file(Path(os.environ['RESULT_FILE']))
print(json.dumps(stage_result(load_config(os.environ), body, os.environ), sort_keys=True))
"""
        staged = subprocess.run(
            [*common, stage_program], env={**environment, "RESULT_FILE": str(result_file)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        self.assertEqual(json.loads(staged.stdout)["phase"], "staged")
        self.assertEqual(self.ingest()["phase"], "completed")

    def test_unreserved_foreign_and_forged_coordinator_staging_are_refused(self) -> None:
        with self.assertRaisesRegex(IngestionRefused, "no controller reservation"):
            ingest_result(
                self.store, self.initiative_id, str(uuid.uuid4()),
                terminal_reconciliation={"state": "exited"}, jj=self.jj,
            )
        foreign = {
            **self.managed,
            "ASHA_CONTROL_RESULT_INGESTION_ID": str(uuid.uuid4()),
        }
        with self.assertRaisesRegex(IngestionRefused, "foreign"):
            stage_result(self.config, self.body(), foreign)
        forged = {
            **self.managed,
            "ASHA_ORCHESTRATION_COORDINATOR_ID": str(uuid.uuid4()),
        }
        with self.assertRaisesRegex(IngestionRefused, "impersonate"):
            stage_result(self.config, self.body(), forged)

    def test_forged_managed_environment_from_another_process_is_refused(self) -> None:
        fake_tmux = mock.Mock()
        fake_tmux.pane_facts.return_value = SimpleNamespace(
            dead=False, pane_pid=12345, session="owned-session",
        )
        fake_tmux.session_option.side_effect = lambda _session, option: {
            "@asha_managed": "1", "@asha_task_id": self.task["task_id"],
        }[option]
        fake_tmux.pane_option.side_effect = lambda _pane, option: {
            "@asha_run_id": self.task["runs"][0]["run_id"],
            "@asha_result_ingestion": self.ingestion["ingestion_id"],
            "@asha_result_outbox_digest": hashlib.sha256(
                self.managed["ASHA_CONTROL_RESULT_OUTBOX"].encode()
            ).hexdigest(),
        }[option]
        env = {**self.managed, "TMUX_PANE": "%9"}
        with mock.patch(
            "lib.control.orchestration.ingestion.caller_descends_from",
            return_value=False,
        ), self.assertRaisesRegex(IngestionRefused, "does not descend"):
            stage_result(
                self.config, self.body(), env, caller_pid=os.getpid(),
                tmux=fake_tmux,
            )

    def test_staging_exact_replay_is_idempotent_and_changed_bytes_refuse(self) -> None:
        body = self.body()
        first = self.stage(body)
        second = self.stage(copy.deepcopy(body))
        self.assertEqual(second, first)
        changed = copy.deepcopy(body)
        changed["summary"] = "different"
        with self.assertRaisesRegex(IngestionRefused, "different canonical bytes"):
            self.stage(changed)

    def test_controller_snapshots_verifies_publishes_and_replays(self) -> None:
        self.stage()
        receipt = self.ingest()
        self.assertEqual(receipt["phase"], "completed")
        self.assertTrue(self.jj.snapshotted)
        result = self.store.read_result(self.initiative_id, receipt["result_id"])
        self.assertEqual(
            result["publication_provenance"]["method"], "controller-ingestion",
        )
        self.assertEqual(result["publication_provenance"]["producer_run_id"], result["run_id"])
        self.assertEqual(result["claimed_commit_id"], "d" * 40)
        self.assertEqual(result["commit_provenance"]["creator"], "controller")
        self.assertTrue(result["commit_provenance"]["verification_evidence_ids"])
        self.assertEqual(self.ingest(), receipt)

    def test_concurrent_ingestion_replay_is_single_flight_and_idempotent(self) -> None:
        self.stage()

        class BlockingSnapshotJj(SnapshotJj):
            def __init__(inner_self, task):
                super().__init__(task)
                inner_self.entered = threading.Event()
                inner_self.release = threading.Event()
                inner_self.snapshot_calls = 0
                inner_self._first_probe = True
                inner_self._probe_lock = threading.Lock()

            def inspect_workspace(inner_self, *args, **kwargs):
                with inner_self._probe_lock:
                    block = inner_self._first_probe
                    inner_self._first_probe = False
                    if kwargs.get("snapshot", False):
                        inner_self.snapshot_calls += 1
                if block:
                    inner_self.entered.set()
                    if not inner_self.release.wait(3):
                        raise AssertionError("timed out holding the first ingestion")
                return super().inspect_workspace(*args, **kwargs)

        jj = BlockingSnapshotJj(self.task)

        def ingest():
            return ingest_result(
                self.store, self.initiative_id, self.ingestion["ingestion_id"],
                control_store=TaskStore(self.config.control), jj=jj,
                terminal_reconciliation={"state": "exited"},
                verifier=self.verifier,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(ingest)
            self.assertTrue(jj.entered.wait(3))
            replay = executor.submit(ingest)
            try:
                with self.assertRaises(FutureTimeout):
                    replay.result(timeout=0.25)
            finally:
                jj.release.set()
            first_receipt = first.result(timeout=3)
            replay_receipt = replay.result(timeout=3)

        self.assertEqual(replay_receipt, first_receipt)
        self.assertEqual(first_receipt["phase"], "completed")
        self.assertEqual(jj.snapshot_calls, 1)

    def test_live_supervisor_ingests_then_reaches_ordinary_seal_pipeline(self) -> None:
        self.stage()

        class TerminalAdapters(LiveAdapters):
            def __init__(inner_self, jj):
                super().__init__(config=None, tmux=mock.Mock(), jj=jj)

            def tmux(inner_self, _task, _run):
                return Evidence("tmux", "missing", "owned pane exited")

            def process(inner_self, _task, _run):
                return Evidence(
                    "process", "missing", "tmux pane process exited with status 0",
                    state="exited",
                )

            def jj(inner_self, _task):
                return Evidence("jj", "match", "owned")

            def event(inner_self, _task, _run):
                return Evidence("event", "missing", "none")

        adapters = TerminalAdapters(self.jj)
        with mock.patch(
            "lib.control.orchestration.ingestion.verify_controller_snapshot",
            side_effect=self.verifier,
        ):
            observed = reconcile_live(
                self.store, self.initiative_id,
                control_store=TaskStore(self.config.control),
                adapters_factory=lambda _task: adapters,
            )
        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        self.assertEqual(retained["state"], "completed")
        self.assertEqual(len(observed["seals"]), 1)
        self.assertEqual(observed["seals"][0]["outcome"], "success")

    def test_modified_completed_candidate_and_stale_plan_fail_closed(self) -> None:
        self.stage()
        receipt = self.ingest()
        path = Path(self.managed["ASHA_CONTROL_RESULT_OUTBOX"])
        candidate = json.loads(path.read_text())
        candidate["body"]["summary"] = "tampered"
        path.write_text(json.dumps(candidate))
        path.chmod(0o600)
        with self.assertRaisesRegex(IngestionRefused, "modified"):
            self.ingest()
        self.assertEqual(
            self.store.read_result(self.initiative_id, receipt["result_id"])["summary"],
            "staged safely",
        )

    def test_scope_refusal_is_precise_and_persists_no_result(self) -> None:
        self.stage(self.body(files_changed=["outside.txt"]))
        receipt = self.ingest()
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("hard scope", receipt["refusal"])
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), [])

    def test_conflicting_prior_publication_is_retained_as_precise_refusal(self) -> None:
        self.stage()
        with mock.patch(
            "lib.control.orchestration.ingestion.publish_bound_result",
            side_effect=ResultRefused("publication ID already has different bytes"),
        ):
            receipt = self.ingest()
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("publication ID already has different bytes", receipt["refusal"])
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), [])

    def test_active_plan_digest_drift_refuses_before_snapshot(self) -> None:
        self.stage()
        initiative = self.initiative()
        stale = copy.deepcopy(initiative)
        stale["active_plan"]["digest"] = "f" * 64
        stale["state_revision"] += 1
        stale["updated_at"] = now_text()
        self.store.save_initiative(
            stale, expected_digest=record_digest(initiative),
        )
        receipt = self.ingest()
        self.assertEqual(receipt["phase"], "refused")
        self.assertIn("stale result ingestion binding", receipt["refusal"])
        self.assertFalse(self.jj.snapshotted)

    def test_seal_binds_controller_commit_provenance(self) -> None:
        self.stage()
        accepted = self.ingest()
        task = TaskStore(self.config.control).peek(self.task["task_id"])
        observed = {
            "state": "exited", "blocker": None,
            "runs": [{
                "run_id": task["runs"][0]["run_id"], "state": "exited",
                "evidence": [
                    {"source": "process", "outcome": "missing", "state": "exited",
                     "detail": "tmux pane process exited with status 0"},
                    {"source": "jj", "outcome": "match", "detail": "owned"},
                ],
            }],
        }
        seal = prepare_and_publish_seal(
            self.store, self.initiative_id, self.attempt["attempt_id"], task,
            observed, jj=self.jj,
        )
        self.assertEqual(seal["result_id"], accepted["result_id"])
        self.assertEqual(seal["outcome"], "success")
        self.assertEqual(seal["commit_provenance"]["creator"], "controller")
        self.assertEqual(self.ingest(), accepted)

    def test_failed_verification_still_attributes_failure_artifact_to_controller(self) -> None:
        self.stage()

        def fail_verification(*_args, **_kwargs):
            raise IngestionRefused("independent verification failed")

        refused = ingest_result(
            self.store, self.initiative_id, self.ingestion["ingestion_id"],
            control_store=TaskStore(self.config.control), jj=self.jj,
            terminal_reconciliation={"state": "exited"},
            verifier=fail_verification,
        )
        self.assertEqual(refused["phase"], "refused")
        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        self.assertEqual(retained["commit_creator"], "controller")
        self.assertTrue(retained["verification_evidence_ids"])
        attempt = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        missing = copy.deepcopy(attempt)
        missing.update({"state": "result-missing", "updated_at": now_text()})
        self.store.save_attempt(
            self.initiative_id, missing, expected_digest=record_digest(attempt),
        )
        task = TaskStore(self.config.control).peek(self.task["task_id"])
        observed = {
            "state": "exited", "blocker": None,
            "runs": [{
                "run_id": task["runs"][0]["run_id"], "state": "exited",
                "evidence": [
                    {"source": "process", "outcome": "missing", "state": "exited",
                     "detail": "tmux pane process exited with status 0"},
                    {"source": "jj", "outcome": "match", "detail": "owned"},
                ],
            }],
        }
        seal = prepare_and_publish_seal(
            self.store, self.initiative_id, self.attempt["attempt_id"], task,
            observed, jj=self.jj,
        )
        self.assertEqual(seal["outcome"], "failure")
        self.assertEqual(seal["commit_provenance"]["creator"], "controller")
        seal_evidence = self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )
        self.assertFalse(
            json.loads(seal_evidence["summary"])["commit_provenance_verified"]
        )


@unittest.skipUnless(shutil.which("bwrap") and shutil.which("jj"), "bwrap and jj required")
class ColocatedObjectStoreErofsTests(unittest.TestCase):
    def test_workspace_write_sandbox_cannot_snapshot_but_controller_can(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            workspace = root / "workspace"
            source.mkdir()
            subprocess.run(
                ["jj", "git", "init", "--colocate", str(source)],
                check=True, capture_output=True,
            )
            (source / "base.txt").write_text("base\n")
            subprocess.run(["jj", "-R", str(source), "status"], check=True, capture_output=True)
            subprocess.run(
                ["jj", "-R", str(source), "workspace", "add", "--name", "erofs-test", str(workspace)],
                check=True, capture_output=True,
            )
            (workspace / "changed.txt").write_text("changed\n")
            outbox = workspace / ".asha/outbox"
            outbox.mkdir(parents=True)
            (outbox / "candidate.json").write_text("private transport\n")
            sandboxed = subprocess.run([
                "bwrap", "--die-with-parent", "--ro-bind", "/", "/",
                "--bind", str(workspace), str(workspace), "--proc", "/proc",
                "--dev-bind", "/dev", "/dev", "--",
                "jj", "-R", str(workspace), "status",
            ], capture_output=True, text=True, check=False)
            self.assertNotEqual(sandboxed.returncode, 0)
            self.assertRegex(
                sandboxed.stderr.lower(), r"read-only file system|could not write object",
            )
            adapter = JjAdapter()
            identity = adapter.inspect_workspace(
                workspace, "erofs-test", snapshot=True, require_empty=False,
                exclude_control_transport=True,
            )
            tree = adapter.immutable_tree(workspace, identity.commit_id)
            paths = {entry[0] for entry in tree.entries}
            self.assertIn("changed.txt", paths)
            self.assertNotIn(".asha/outbox/candidate.json", paths)
            self.assertRegex(identity.commit_id, r"^[0-9a-f]{40,64}$")


if __name__ == "__main__":
    unittest.main()
