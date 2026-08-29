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

from lib.control.jj import ImmutableTree, JjAdapter, JjError, WorkspaceIdentity
from lib.control.reconcile import Evidence, LiveAdapters
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.cli import task_main
from lib.control.orchestration.coordinator import CoordinatorError
from lib.control.orchestration.ingestion import (
    IngestionRefused,
    IngestionUnavailable,
    _save_verification_evidence,
    ingest_result,
    reserve_result_ingestion,
    result_ingestion_id,
    stage_result,
    verify_controller_snapshot,
)
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.model import validate_result_ingestion
from lib.control.orchestration.results import ResultRefused, publish_result
from lib.control.orchestration.seals import prepare_and_publish_seal
from lib.control.orchestration.reconcile import reconcile_live
from lib.control.store import StoreError, TaskStore
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
        self.staging_token = "a5" * 32
        self.launch_environment = None

        def capture(argv, **kwargs):
            self.launch_environment = kwargs.get("env")
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
        ), mock.patch(
            "secrets.token_hex", return_value=self.staging_token,
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
            "ASHA_CONTROL_RESULT_TOKEN": self.staging_token,
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

    @staticmethod
    def _exact_snapshot_jj(commit_id: str, tree_digest: str):
        class ExactSnapshotJj:
            def inspect_workspace(
                inner_self, path, name, *, snapshot=False, require_empty=True,
            ):
                del inner_self, path, snapshot, require_empty
                return WorkspaceIdentity(
                    name=name, change_id="7" * 32, commit_id="6" * 40,
                    parent_commit_ids=(commit_id,), description="verification",
                )

            def immutable_tree(inner_self, repository, observed_commit_id):
                del inner_self, repository
                return ImmutableTree(observed_commit_id, tree_digest, ())

        return ExactSnapshotJj()

    @staticmethod
    def _terminal_observed(task: dict) -> dict:
        return {
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

    def _controller_verification_refusal(
        self, output: bytes, child_status: int,
    ) -> str:
        commit_id = "9" * 40
        tree_digest = "8" * 64

        captured: dict[str, Path] = {}

        def contained(_bubblewrap, _environment, _argv, **kwargs):
            captured["output_path"] = kwargs["output_path"]
            return ["contained-verification"]

        def execute(_argv, **_kwargs):
            output_id = captured["output_path"].stem
            self.store.finalize_reserved_output(
                self.initiative_id, output_id, output,
            )
            return 0, {
                "returncode": child_status,
                "invocation_error": None,
                "timed_out": False,
                "output_truncated": False,
                "output_original_bytes": len(output),
                "output_digest": hashlib.sha256(output).hexdigest(),
            }

        body = self.body(verification_attestations=[{
            "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            "cwd": ".", "exit_code": 0, "output_digest": "5" * 64,
        }])
        materialization = {
            "workspace_name": "controller-verification",
            "workspace_path": str(self.workspace),
        }
        with mock.patch(
            "lib.control.orchestration.ingestion.prepare_materialization",
            return_value=materialization,
        ), mock.patch(
            "lib.control.orchestration.ingestion.tracked_workspace_status",
            return_value=(True, [], False),
        ), mock.patch(
            "lib.control.orchestration.ingestion._bubblewrap_program",
            return_value=Path("/usr/bin/bwrap"),
        ), mock.patch(
            "lib.control.orchestration.ingestion._contained_argv",
            side_effect=contained,
        ), mock.patch(
            "lib.control.orchestration.ingestion._capture_truncated",
            side_effect=execute,
        ):
            with self.assertRaises(IngestionRefused) as refused:
                verify_controller_snapshot(
                    self.store, self.ingestion, self.task, body,
                    commit_id, tree_digest,
                    jj=self._exact_snapshot_jj(commit_id, tree_digest),
                )
        return str(refused.exception)

    def test_failing_controller_rerun_refusal_contains_output_tail(self) -> None:
        refusal = self._controller_verification_refusal(
            ("é" * 1500).encode() + b"controller-output\x00tail", 7,
        )
        self.assertIn("command failure", refusal)
        self.assertIn("controller-output?tail", refusal)
        self.assertTrue(all(character.isprintable() for character in refusal))
        self.assertLessEqual(len(refusal.encode("utf-8")), 2048)

    def test_worker_stages_reserved_candidate_without_authoritative_write(self) -> None:
        before = self.store.list_results_snapshot(self.initiative_id)
        receipt = self.stage()
        self.assertEqual(receipt["phase"], "staged")
        self.assertEqual(receipt["identity_proof"], "pane")
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
            staging_token_digest=self.ingestion["staging_token_digest"],
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
            staging_token_digest=self.ingestion["staging_token_digest"],
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["result_id"], accepted["result_id"])

    def test_reservation_staging_token_digest_is_immutable_even_from_null(self) -> None:
        changed = copy.deepcopy(self.ingestion)
        changed["staging_token_digest"] = "f" * 64
        with self.assertRaisesRegex(StoreError, "staging_token_digest"):
            self.store.save_result_ingestion(
                self.initiative_id, changed,
                expected_digest=record_digest(self.ingestion),
            )

        legacy = copy.deepcopy(self.ingestion)
        legacy["staging_token_digest"] = None
        record_path = (
            self.config.initiatives_dir / self.initiative_id / "result-ingestions"
            / f"{self.ingestion['ingestion_id']}.json"
        )
        record_path.write_text(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n")
        rebound = copy.deepcopy(legacy)
        rebound["staging_token_digest"] = "e" * 64
        retained = self.store.list_result_ingestions_snapshot(
            self.initiative_id,
        )[0]
        with self.assertRaisesRegex(StoreError, "staging_token_digest"):
            self.store.save_result_ingestion(
                self.initiative_id, rebound,
                expected_digest=record_digest(retained),
            )

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

    def _sandbox_arguments(self) -> tuple[dict[str, str], list[str], Path]:
        result_file = self.workspace / ".asha" / "input-result.json"
        result_file.parent.mkdir(mode=0o700, exist_ok=True)
        result_file.write_text(json.dumps(self.body()))
        result_file.chmod(0o600)
        environment = {
            **os.environ, **self.managed,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "RESULT_FILE": str(result_file),
            "TMUX_PANE": "%9",
        }
        common = [
            "bwrap", "--die-with-parent", "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--ro-bind", str(self.root), str(self.root),
            "--bind", str(self.workspace), str(self.workspace),
            "--unshare-pid", "--proc", "/proc",
            "--dev-bind", "/dev", "/dev", "--",
            sys.executable, "-c",
        ]
        return environment, common, result_file

    def _sandbox_stage(self, token: str | None) -> subprocess.CompletedProcess[str]:
        environment, common, _result_file = self._sandbox_arguments()
        if token is None:
            environment.pop("ASHA_CONTROL_RESULT_TOKEN", None)
        else:
            environment["ASHA_CONTROL_RESULT_TOKEN"] = token
        stage_program = """
import json, os
from pathlib import Path
from lib.control.orchestration.config import load_config
from lib.control.orchestration.ingestion import stage_result
from lib.control.orchestration.results import read_client_file
body = read_client_file(Path(os.environ['RESULT_FILE']))
try:
    receipt = stage_result(
        load_config(os.environ), body, os.environ, caller_pid=os.getpid(),
    )
except BaseException as exc:
    print(type(exc).__name__ + ': ' + str(exc))
    raise SystemExit(31)
print(json.dumps(receipt, sort_keys=True))
"""
        return subprocess.run(
            [*common, stage_program], env=environment,
            capture_output=True, text=True, check=False,
        )

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
    def test_workspace_sandbox_reproduces_control_state_erofs_then_token_stages(self) -> None:
        environment, common, result_file = self._sandbox_arguments()
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
        direct = subprocess.run(
            [*common, direct_program], env=environment,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(direct.returncode, 30, direct.stdout + direct.stderr)
        self.assertIn("Read-only file system", direct.stdout)
        self.assertEqual(self.store.list_results_snapshot(self.initiative_id), [])
        staged = self._sandbox_stage(self.staging_token)
        self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
        receipt = json.loads(staged.stdout)
        self.assertEqual(receipt["phase"], "staged")
        self.assertEqual(receipt["identity_proof"], "token")
        self.assertEqual(self.ingest()["phase"], "completed")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
    def test_workspace_sandbox_wrong_token_refuses_without_candidate(self) -> None:
        staged = self._sandbox_stage("f0" * 32)
        self.assertEqual(staged.returncode, 31, staged.stdout + staged.stderr)
        self.assertIn("pane proof unreachable:", staged.stdout)
        self.assertIn(
            "token proof failed: staging token is invalid", staged.stdout,
        )
        self.assertFalse(Path(self.managed["ASHA_CONTROL_RESULT_OUTBOX"]).exists())

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
    def test_workspace_sandbox_absent_token_names_both_failed_proofs(self) -> None:
        staged = self._sandbox_stage(None)
        self.assertEqual(staged.returncode, 31, staged.stdout + staged.stderr)
        self.assertIn("pane proof unreachable:", staged.stdout)
        self.assertIn(
            "token proof failed: staging token is absent", staged.stdout,
        )
        self.assertFalse(Path(self.managed["ASHA_CONTROL_RESULT_OUTBOX"]).exists())

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap required")
    def test_workspace_sandbox_legacy_null_digest_refuses_token(self) -> None:
        legacy = copy.deepcopy(self.ingestion)
        legacy["staging_token_digest"] = None
        record_path = (
            self.config.initiatives_dir / self.initiative_id / "result-ingestions"
            / f"{self.ingestion['ingestion_id']}.json"
        )
        record_path.write_text(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n")
        staged = self._sandbox_stage(self.staging_token)
        self.assertEqual(staged.returncode, 31, staged.stdout + staged.stderr)
        self.assertIn("pane proof unreachable:", staged.stdout)
        self.assertIn(
            "token proof failed: reservation has no staging token digest",
            staged.stdout,
        )
        self.assertFalse(Path(self.managed["ASHA_CONTROL_RESULT_OUTBOX"]).exists())

    def test_legacy_reservation_without_digest_reads_as_null(self) -> None:
        legacy = copy.deepcopy(self.ingestion)
        legacy.pop("staging_token_digest", None)
        self.assertIsNone(validate_result_ingestion(legacy)["staging_token_digest"])

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
            "TMUX_PANE": "%9",
        }
        fake_tmux = mock.Mock()
        with self.assertRaisesRegex(IngestionRefused, "impersonate"):
            stage_result(
                self.config, self.body(), forged, caller_pid=os.getpid(),
                tmux=fake_tmux,
            )
        fake_tmux.pane_facts.assert_not_called()

    def test_live_pane_proof_remains_primary_and_is_named_in_receipt(self) -> None:
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
            return_value=True,
        ):
            receipt = stage_result(
                self.config, self.body(), env, caller_pid=os.getpid(),
                tmux=fake_tmux,
            )
        self.assertEqual(receipt["identity_proof"], "pane")
        fake_tmux.pane_facts.assert_called_once_with("%9")

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

    def test_environment_class_rerun_publishes_and_seals_with_durable_gap(self) -> None:
        attestations = [{
            "argv": [sys.executable, "-c", "raise SystemExit(127)"],
            "cwd": ".", "exit_code": 0, "output_digest": "5" * 64,
            "finished_at": now_text(), "summary": "worker passed",
        }, {
            "argv": [sys.executable, "-c", "raise AssertionError('must not run')"],
            "cwd": ".", "exit_code": 0, "output_digest": "4" * 64,
            "finished_at": now_text(), "summary": "worker passed again",
        }]
        self.stage(self.body(verification_attestations=attestations))

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

        captured: dict[str, Path] = {}
        rerun_argv: list[list[str]] = []

        def contained(_bubblewrap, _environment, argv, **kwargs):
            rerun_argv.append(argv)
            captured["output_path"] = kwargs["output_path"]
            return ["contained-verification"]

        def execute(_argv, **_kwargs):
            output = b"\n--- stderr ---\n"
            output_id = captured["output_path"].stem
            self.store.finalize_reserved_output(
                self.initiative_id, output_id, output,
            )
            return 0, {
                "returncode": 127,
                "invocation_error": None,
                "timed_out": False,
                "output_truncated": False,
                "output_original_bytes": len(output),
                "output_digest": hashlib.sha256(output).hexdigest(),
            }

        def real_verifier(store, ingestion, task, body, commit, tree, **_kwargs):
            return verify_controller_snapshot(
                store, ingestion, task, body, commit, tree,
                jj=self._exact_snapshot_jj(commit, tree),
            )

        materialization = {
            "workspace_name": "controller-verification",
            "workspace_path": str(self.workspace),
        }
        with mock.patch(
            "lib.control.orchestration.ingestion.prepare_materialization",
            return_value=materialization,
        ), mock.patch(
            "lib.control.orchestration.ingestion.tracked_workspace_status",
            return_value=(True, [], False),
        ), mock.patch(
            "lib.control.orchestration.ingestion._bubblewrap_program",
            return_value=Path("/usr/bin/bwrap"),
        ), mock.patch(
            "lib.control.orchestration.ingestion._contained_argv",
            side_effect=contained,
        ), mock.patch(
            "lib.control.orchestration.ingestion._capture_truncated",
            side_effect=execute,
        ), mock.patch(
            "lib.control.orchestration.ingestion.verify_controller_snapshot",
            side_effect=real_verifier,
        ):
            observed = reconcile_live(
                self.store, self.initiative_id,
                control_store=TaskStore(self.config.control),
                adapters_factory=lambda _task: TerminalAdapters(self.jj),
            )

        self.assertEqual(rerun_argv, [attestations[0]["argv"]])
        result = self.store.list_results_snapshot(self.initiative_id)[0]
        self.assertEqual(result["claim_status"], "completed")
        gap_records = []
        for evidence_id in result["commit_provenance"]["verification_evidence_ids"]:
            evidence = self.store.read_evidence(self.initiative_id, evidence_id)
            detail = json.loads(evidence["summary"])
            if detail.get("kind") == "snapshot-verification-environment-gap":
                gap_records.append(detail)
        self.assertEqual(len(gap_records), 1)
        gap = gap_records[0]
        self.assertEqual(gap["claimed_commit_id"], "d" * 40)
        self.assertEqual(gap["claimed_tree_digest"], "e" * 64)
        self.assertEqual(gap["failure_kind"], "invocation/environment")
        self.assertEqual(gap["status"], "unreproducible-environment")
        self.assertEqual(gap["argv"], attestations[0]["argv"])
        self.assertEqual(gap["cwd"], attestations[0]["cwd"])
        self.assertIn("output_tail", gap)
        self.assertTrue(all(character.isprintable() for character in gap["output_tail"]))
        self.assertLessEqual(len(gap["output_tail"].encode("utf-8")), 2048)
        self.assertEqual(len(observed["seals"]), 1)
        seal = observed["seals"][0]
        self.assertEqual(seal["outcome"], "success")
        seal_detail = json.loads(self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )["summary"])
        self.assertTrue(seal_detail["commit_provenance_verified"])
        self.assertTrue(seal_detail["verification_environment_degraded"])
        output = StringIO()
        with redirect_stdout(output):
            status = task_main(["seal", self.task["task_id"], "--json"], env=self.env)
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue())["verification"], "environment-degraded",
        )

    def test_tampered_environment_gap_fails_exact_provenance(self) -> None:
        self.stage()

        def tampered_verifier(store, ingestion, _task, _body, commit, _tree, **_kwargs):
            return [_save_verification_evidence(
                store, self.initiative_id, ingestion["ingestion_id"], {
                    "kind": "snapshot-verification-environment-gap",
                    "claimed_commit_id": commit,
                    "claimed_tree_digest": "0" * 64,
                    "argv": [sys.executable, "-m", "unittest"],
                    "cwd": ".",
                    "failure_kind": "invocation/environment",
                    "output_tail": "<empty>",
                    "status": "unreproducible-environment",
                },
            )]

        accepted = ingest_result(
            self.store, self.initiative_id, self.ingestion["ingestion_id"],
            control_store=TaskStore(self.config.control), jj=self.jj,
            terminal_reconciliation={"state": "exited"},
            verifier=tampered_verifier,
        )
        self.assertEqual(accepted["phase"], "completed")
        task = TaskStore(self.config.control).peek(self.task["task_id"])
        seal = prepare_and_publish_seal(
            self.store, self.initiative_id, self.attempt["attempt_id"], task,
            self._terminal_observed(task), jj=self.jj,
        )
        self.assertEqual(seal["outcome"], "failure")
        seal_detail = json.loads(self.store.read_evidence(
            self.initiative_id, seal["process_evidence_id"],
        )["summary"])
        self.assertFalse(seal_detail["commit_provenance_verified"])

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


    @staticmethod
    def _enoent_jj(task: dict) -> SnapshotJj:
        class EnoentJj(SnapshotJj):
            def inspect_workspace(self, *_args, **_kwargs):
                raise JjError(
                    "command invocation failed: "
                    "[Errno 2] No such file or directory: 'jj'"
                )

        return EnoentJj(task)

    def test_environment_failure_leaves_ingestion_retryable_then_seals(self) -> None:
        self.stage()
        with self.assertRaisesRegex(IngestionUnavailable, "command invocation failed"):
            ingest_result(
                self.store, self.initiative_id, self.ingestion["ingestion_id"],
                control_store=TaskStore(self.config.control),
                jj=self._enoent_jj(self.task),
                terminal_reconciliation={"state": "exited"},
                verifier=self.verifier,
            )
        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        self.assertNotIn(retained["state"], {"completed", "refused"})

        self.ingest()
        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        self.assertEqual(retained["state"], "completed")

    def test_snapshot_materialization_environment_failure_is_not_a_refusal(self) -> None:
        with mock.patch(
            "lib.control.orchestration.ingestion.prepare_materialization",
            side_effect=JjError(
                "command invocation failed: "
                "[Errno 2] No such file or directory: 'jj'"
            ),
        ):
            with self.assertRaisesRegex(
                IngestionUnavailable, "controller snapshot materialization failed",
            ):
                verify_controller_snapshot(
                    self.store, self.ingestion, self.task, self.body(),
                    "a" * 40, "b" * 64, jj=self.jj,
                )

    def test_live_pass_retries_after_environment_failure_and_then_seals(self) -> None:
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

        with mock.patch(
            "lib.control.orchestration.ingestion.verify_controller_snapshot",
            side_effect=self.verifier,
        ):
            first = reconcile_live(
                self.store, self.initiative_id,
                control_store=TaskStore(self.config.control),
                adapters_factory=lambda _task: TerminalAdapters(
                    self._enoent_jj(self.task),
                ),
            )
            retained = self.store.read_result_ingestion(
                self.initiative_id, self.ingestion["ingestion_id"],
            )
            self.assertNotIn(retained["state"], {"completed", "refused"})
            self.assertEqual(first["seals"], [])

            second = reconcile_live(
                self.store, self.initiative_id,
                control_store=TaskStore(self.config.control),
                adapters_factory=lambda _task: TerminalAdapters(self.jj),
            )
        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        self.assertEqual(retained["state"], "completed")
        self.assertEqual(len(second["seals"]), 1)
        self.assertEqual(second["seals"][0]["outcome"], "success")


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
