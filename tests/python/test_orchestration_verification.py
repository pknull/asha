from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.orchestration import actions as action_module
from lib.control.jj import (
    ImmutableTree, MaterializationPlan, RepositoryFacts, WorkspaceIdentity,
)
from lib.control.prepare import plan_materialization
from lib.control.orchestration.actions import (
    _parse_document, action_outcome, build_action_document, reconcile_actions,
    submit_action,
)
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.links import build_link
from lib.control.orchestration.results import publish_result
from lib.control.orchestration.store import ObservationOnlyPlanError, StoreError
from lib.control.orchestration.verification import (
    candidate_bundle_digest, command_denial, prepare_verification_intent,
    prevalidate_verification, run_verification,
)
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import (
    advance_node,
    save_accepted_review,
    save_candidate,
)
from tests.python.test_orchestration_graph import valid_plan
from tests.python.test_control_config_model import task_record


class VerificationJj:
    def __init__(
        self, source: Path, candidate: dict, *, mutate: bool = False,
        fail_on_inspection: int | None = None,
    ):
        self.source = source
        self.candidate = candidate
        self.mutate = mutate
        self.fail_on_inspection = fail_on_inspection
        self.inspections = 0

    def preflight(self, source):
        return RepositoryFacts(root=self.source, git_root=self.source / ".git")

    def materialization_plan(self, git_root, commit_id, *, exact_root):
        return MaterializationPlan(commit_id, "0" * 64, (), 0, 0, 0)

    def inspect_workspace(self, path, name, *, snapshot=False, require_empty=True):
        self.inspections += 1
        if self.inspections == self.fail_on_inspection:
            raise OSError(f"inspection {self.inspections} failed")
        changed = self.mutate and self.inspections >= 2
        return WorkspaceIdentity(
            name=name,
            change_id="k" * 32,
            commit_id=("1" if changed else "f") * 40,
            parent_commit_ids=(self.candidate["jj_commit_id"],),
            description="controller materialization",
        )

    def immutable_tree(self, repository, commit_id):
        digest = "1" * 64 if commit_id == "1" * 40 else self.candidate["tree_digest"]
        return ImmutableTree(commit_id, digest, ())


class OrchestrationVerificationTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        method = self._testMethodName
        if "denied" in method:
            command = ["gh", "api", "repos/example/project"]
        elif "timeout_large_output" in method:
            command = [
                sys.executable, "-c",
                "import sys,time; sys.stdout.write('z'*(2*1024*1024)); "
                "sys.stdout.flush(); time.sleep(5)",
            ]
        elif "timeout" in method:
            command = [sys.executable, "-c", "import time; time.sleep(5)"]
        elif "explicit_exit_143" in method:
            command = [sys.executable, "-c", "raise SystemExit(143)"]
        elif "signal" in method:
            command = [sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"]
        elif "descendant" in method:
            command = [
                sys.executable, "-c",
                "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
                "\"import time; time.sleep(.6); open('late-marker','w').write('late')\"],"
                "start_new_session=True)",
            ]
        elif "devnull" in method:
            command = [
                sys.executable, "-c",
                "import subprocess,sys; subprocess.check_call([sys.executable,'-c',"
                "'open(\\\"/dev/null\\\",\\\"wb\\\").write(b\\\"ok\\\")'],"
                "stdout=subprocess.DEVNULL)",
            ]
        elif "nonzero" in method:
            command = [sys.executable, "-c", "raise SystemExit(7)"]
        elif "large_output" in method:
            command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))"]
        elif "mutation" in method:
            command = [sys.executable, "-c", "print('mutating identity test')"]
        else:
            command = [
                sys.executable, "-c",
                "import os; assert set(os.environ) <= {'PATH','HOME','LANG'}; print('verified')",
            ]
        original = valid_plan

        def plan_for_command():
            value = original()
            specification = value["declared_gates"][1]["commands"][0]
            specification["argv"] = command
            specification["timeout_seconds"] = 1 if "timeout" in method else 30
            return value

        with mock.patch(
            "tests.python.orchestration_execution_fixtures.valid_plan",
            side_effect=plan_for_command,
        ):
            super().setUp()
        self.candidate = save_candidate(self)
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        advance_node(self, "review-a", ["ready", "evaluating", "succeeded"])
        advance_node(self, "verify-a", ["ready"])
        self.review = save_accepted_review(self, self.candidate)
        self.materialization = self.root / "fresh-materialization"

    def _materializer(self, config, source, base_commit_id, name, *, jj):
        target_plan = plan_materialization(config, source, name, jj=jj)
        target = Path(target_plan["workspace_path"])
        workspace_name = target_plan["workspace_name"]
        self.materialization = target
        self.materialization.mkdir(mode=0o700, parents=True)
        self.assertEqual(base_commit_id, self.candidate["jj_commit_id"])
        return {
            "workspace_name": workspace_name,
            "workspace_path": str(self.materialization),
            "change_id": "k" * 32,
            "working_commit_id": "f" * 40,
        }

    def _intent_plan_patch(self):
        def planned(_config, _source, name, **_kwargs):
            return {
                "workspace_name": f"unit-{name}",
                "workspace_path": str((self.root / "unit-intents" / name).resolve()),
            }

        return mock.patch(
            "lib.control.orchestration.verification.plan_materialization",
            side_effect=planned,
        )

    def _run(self, *, mutate=False, fail_on_inspection=None, non_tracked=None):
        adapter = VerificationJj(
            self.repo, self.candidate, mutate=mutate,
            fail_on_inspection=fail_on_inspection,
        )
        with mock.patch(
            "lib.control.orchestration.verification.tracked_workspace_status",
            return_value=(not mutate, list(non_tracked or []), False),
        ):
            record = run_verification(
                self.store, self.initiative_id, "verify-a", jj=adapter,
                environment={
                    "PATH": os.environ["PATH"], "HOME": str(self.root / "home"),
                    "LANG": "C.UTF-8", "SECRET_SHOULD_NOT_LEAK": "forbidden",
                },
                materializer=self._materializer,
            )
        return record

    def test_direct_verification_boundaries_refuse_historical_plan_before_effects(self) -> None:
        self.install_historical_active_plan()
        before = self.store.list_verifications_snapshot(self.initiative_id)

        with self.assertRaises(ObservationOnlyPlanError):
            prevalidate_verification(
                self.store, self.initiative_id, "verify-a",
            )
        with self.assertRaises(ObservationOnlyPlanError), mock.patch(
            "lib.control.orchestration.verification.prepare_materialization",
        ) as materialize, mock.patch(
            "lib.control.orchestration.verification._capture_truncated",
        ) as execute:
            run_verification(
                self.store, self.initiative_id, "verify-a",
                materializer=materialize,
            )

        materialize.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(
            self.store.list_verifications_snapshot(self.initiative_id), before,
        )

    def test_fresh_materialization_records_equal_pre_post_identity_and_passes(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "passed")
        command = record["commands"][0]
        self.assertEqual(command["exit_code"], 0)
        self.assertEqual(command["pre_identity_digest"], command["post_identity_digest"])
        self.assertEqual(command["pre_tree_digest"], self.candidate["tree_digest"])
        self.assertEqual(command["post_tree_digest"], self.candidate["tree_digest"])
        output_path = Path(command["output_path"])
        self.assertTrue(output_path.is_file())
        self.assertEqual(
            hashlib.sha256(output_path.read_bytes()).hexdigest(),
            command["output_digest"],
        )
        self.assertRegex(
            command["process_identity"],
            r"^pidns:[0-9]+:[0-9]+:pid:[0-9]+:start:[0-9]+$",
        )
        self.assertEqual(self.initiative()["state"], "ready-for-integration")

    def test_ignored_artifact_is_evidence_not_candidate_mutation(self) -> None:
        record = self._run(non_tracked=["__pycache__", "__pycache__/module.pyc"])
        self.assertEqual(record["state"], "passed")
        detail = json.loads(self.store.read_evidence(
            self.initiative_id, record["evidence_ids"][0],
        )["summary"])
        self.assertEqual(
            detail["non_tracked_paths"],
            ["__pycache__", "__pycache__/module.pyc"],
        )
        self.assertFalse(detail["mutation"])

    def test_large_output_is_streamed_and_retained_with_truncation_marker(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "passed")
        command = record["commands"][0]
        output = Path(command["output_path"]).read_bytes()
        self.assertEqual(len(output), 1024 * 1024)
        self.assertTrue(command["output_truncated"])
        self.assertGreater(command["output_original_bytes"], len(output))
        self.assertIn(b"truncated", output)

    def test_explicit_exit_143_is_not_fabricated_as_a_signal(self) -> None:
        record = self._run()
        command = record["commands"][0]
        self.assertEqual(record["state"], "failed")
        self.assertEqual(command["exit_code"], 143)
        self.assertIsNone(command["signal"])

    def test_devnull_and_subprocess_work_inside_pid_containment(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "passed")
        self.assertEqual(record["commands"][0]["exit_code"], 0)

    def test_detached_descendant_cannot_mutate_after_command_returns(self) -> None:
        record = self._run()
        marker = self.materialization / "late-marker"
        self.assertEqual(record["state"], "passed")
        self.assertFalse(marker.exists())
        time.sleep(0.9)
        self.assertFalse(marker.exists())

    def test_unapproved_command_is_denied_before_running(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "failed")
        detail = json.loads(self.store.read_evidence(
            self.initiative_id, record["evidence_ids"][0],
        )["summary"])
        self.assertTrue(detail["denied"])
        self.assertIsNone(record["commands"][0]["exit_code"])

    def test_environment_assignment_cannot_turn_env_into_a_denied_launcher(self) -> None:
        self.assertEqual(
            command_denial(["X=1", "gh", "api", "repos/example/project"]),
            "invalid executable token",
        )

    def test_shell_and_multicall_wrappers_are_denied(self) -> None:
        for argv in (
            ["/usr/bin/env", "gh", "api", "repos/example/project"],
            ["sh", "-c", "gh api repos/example/project"],
            ["busybox", "rm", "-rf", "."],
            ["setsid", "gh", "api", "repos/example/project"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(command_denial(argv))

    def test_compact_python_pip_and_versioned_pip_are_denied(self) -> None:
        self.assertEqual(
            command_denial(["python3", "-mpip", "install", "example"]),
            "python -m pip install",
        )
        self.assertEqual(
            command_denial(["pip3.12", "install", "example"]),
            "pip install",
        )

    def test_release_tool_write_commands_are_denied(self) -> None:
        for argv in (
            ["twine", "upload", "dist/*"],
            ["cargo", "publish"],
            ["gem", "push", "package.gem"],
            ["poetry", "publish"],
            ["uv", "pip", "install", "example"],
            ["python3", "-m", "twine", "upload", "dist/example.whl"],
            ["python3", "-mtwine", "upload", "dist/example.whl"],
            ["python3", "-m", "poetry", "publish"],
            ["python3", "-muv", "pip", "install", "example"],
            ["python3", "-m", "twine.__main__", "upload", "dist/example.whl"],
            ["python3", "-mtwine.__main__", "upload", "dist/example.whl"],
            ["python3", "-m", "pip.__main__", "install", "example"],
            ["python3", "-muv.__main__", "pip", "install", "example"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(command_denial(argv))

    def test_cancel_is_refused_while_verification_dispatch_is_active(self) -> None:
        started = threading.Event()
        release = threading.Event()
        result: list[dict] = []
        execute_local = action_module._execute_local

        def slow_verification(_store, action, payload):
            if action["action_class"] != "dispatch-node":
                return execute_local(_store, action, payload)
            verification = self.store.list_verifications_snapshot(
                self.initiative_id,
            )[0]
            running = copy.deepcopy(verification)
            running.update({"state": "running", "updated_at": now_text()})
            self.store.save_verification(
                self.initiative_id, running,
                expected_digest=record_digest(verification),
            )
            started.set()
            self.assertTrue(release.wait(3))
            return {"status": "passed", "node_id": payload["node_id"]}

        verify = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        with self._intent_plan_patch(), mock.patch(
            "lib.control.orchestration.actions._execute_local",
            side_effect=slow_verification,
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    submit_action(self.store, self.initiative_id, verify)
                ),
            )
            thread.start()
            self.assertTrue(started.wait(2))
            cancel = submit_action(
                self.store, self.initiative_id,
                build_action_document(
                    self.initiative(), "cancel-node", {"node_id": "verify-a"},
                ),
            )
            self.assertEqual(cancel["state"], "refused")
            self.assertIn("active verification", action_outcome(cancel)["reason"])
            self.assertEqual(
                self.store.read_node(self.initiative_id, "verify-a")["state"],
                "evaluating",
            )
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["state"], "completed")

    def test_cancel_is_refused_during_terminal_verification_node_convergence(self) -> None:
        action_id = str(uuid.uuid4())
        with self._intent_plan_patch():
            verification = prepare_verification_intent(
                self.store, self.initiative_id, "verify-a",
                action_id=action_id, verification_id=str(uuid.uuid4()),
            )
        running = copy.deepcopy(verification)
        running.update({"state": "running", "updated_at": now_text()})
        self.store.save_verification(
            self.initiative_id, running,
            expected_digest=record_digest(verification),
        )
        passed = copy.deepcopy(running)
        passed.update({
            "state": "passed", "outcome": "passed", "updated_at": now_text(),
        })
        self.store.save_verification(
            self.initiative_id, passed, expected_digest=record_digest(running),
        )

        cancel = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "cancel-node", {"node_id": "verify-a"},
            ),
        )
        self.assertEqual(cancel["state"], "refused")
        self.assertIn("active verification", action_outcome(cancel)["reason"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "evaluating",
        )

    def test_blocked_verify_dispatch_is_refused_before_dispatching(self) -> None:
        stale = copy.deepcopy(self.review)
        stale.update({
            "state": "stale", "verdict": None, "findings": [],
            "updated_at": now_text(),
        })
        self.store.save_review(
            self.initiative_id, stale, expected_digest=record_digest(self.review),
        )
        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        retained = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(retained["state"], "refused")
        self.assertIn("accepted-pass review", action_outcome(retained)["reason"])
        self.assertEqual(self.store.list_verifications_snapshot(self.initiative_id), [])

    def test_running_verification_releases_initiative_lock_for_publication(self) -> None:
        started = threading.Event()
        release = threading.Event()
        result: list[dict] = []

        publication_node = self.store.read_node(
            self.initiative_id, "implementation-a",
        )
        publication_action, _publication_payload = _parse_document(
            build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            )
        )
        self.store.save_action(self.initiative_id, publication_action)
        publication_workspace = (self.root / "publication-workspace").resolve()
        publication_workspace.mkdir(mode=0o700)
        publication_task = task_record(
            task_id=str(uuid.uuid4()), repository_root=str(self.repo),
            workspace_path=str(publication_workspace),
        )
        publication_task["jj"]["base_commit_id"] = self.candidate["jj_commit_id"]
        publication_attempt = {
            "contract": "asha.orchestration-attempt.v1",
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": publication_node["node_id"],
            "task_id": publication_task["task_id"],
            "action_id": publication_action["action_id"],
            "ordinal": 1,
            "base": copy.deepcopy(publication_node["base"]),
            "state": "running",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        self.store.save_attempt(self.initiative_id, publication_attempt)
        self.store.save_link(
            self.initiative_id,
            build_link(
                self.initiative(), publication_node, publication_attempt,
                publication_action, publication_task,
            ),
        )

        class PublicationJj:
            def inspect_workspace(inner_self, path, name, *, snapshot=False, require_empty=True):
                del inner_self, path, snapshot, require_empty
                return WorkspaceIdentity(
                    name=name,
                    change_id=publication_task["jj"]["change_id"],
                    commit_id=publication_task["jj"]["working_commit_id"],
                    parent_commit_ids=(publication_task["jj"]["base_commit_id"],),
                    description="publication",
                )

        def slow_verification(_store, _action, payload):
            started.set()
            self.assertTrue(release.wait(3))
            return {"status": "passed", "node_id": payload["node_id"]}

        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        with self._intent_plan_patch(), mock.patch(
            "lib.control.orchestration.actions._execute_local",
            side_effect=slow_verification,
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    submit_action(self.store, self.initiative_id, action)
                ),
            )
            thread.start()
            self.assertTrue(started.wait(2))
            before = time.monotonic()
            body = {
                "contract": "asha.orchestration-result.v1",
                "publication_id": str(uuid.uuid4()),
                "supersedes_result_id": None,
                "initiative_id": self.initiative_id,
                "node_id": publication_attempt["node_id"],
                "attempt_id": publication_attempt["attempt_id"],
                "task_id": publication_task["task_id"],
                "run_id": publication_task["runs"][0]["run_id"],
                "claim_status": "completed", "summary": "concurrent publication",
                "files_changed": [], "verification_attestations": [],
                "concerns": [], "follow_up": [], "published_at": now_text(),
            }
            control_store = mock.Mock()
            control_store.peek.return_value = publication_task
            receipt = publish_result(
                self.store, body,
                {
                    "ASHA_CONTROL_MANAGED": "1",
                    "ASHA_CONTROL_TASK_ID": publication_task["task_id"],
                    "ASHA_CONTROL_RUN_ID": publication_task["runs"][0]["run_id"],
                },
                control_store=control_store, jj=PublicationJj(),
            )
            elapsed = time.monotonic() - before
            self.assertEqual(receipt["phase"], "completed", receipt)
            duplicate = submit_action(
                self.store, self.initiative_id,
                build_action_document(
                    self.initiative(), "dispatch-node", {"node_id": "verify-a"},
                ),
            )
            self.assertEqual(duplicate["state"], "refused")
            self.assertIn("ready", action_outcome(duplicate)["reason"])
            live = reconcile_actions(self.store, self.initiative_id)
            self.assertEqual(live["actions"][0]["state"], "dispatching")
            verification = self.store.list_verifications_snapshot(self.initiative_id)
            self.assertEqual(len(verification), 1)
            self.assertEqual(verification[0]["state"], "dispatching")
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result[0]["state"], "completed")

    def test_pid_containment_is_required_before_materialization(self) -> None:
        adapter = VerificationJj(self.repo, self.candidate)
        materializer = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.verification._bubblewrap_program",
            side_effect=ValueError("containment unavailable"),
        ):
            with self.assertRaisesRegex(ValueError, "containment unavailable"):
                run_verification(
                    self.store, self.initiative_id, "verify-a", jj=adapter,
                    materializer=materializer,
                )
        materializer.assert_not_called()
        self.assertEqual(
            self.store.list_verifications_snapshot(self.initiative_id), [],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )

    def test_timeout_fails_and_records_timeout(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "failed")
        self.assertTrue(record["commands"][0]["timed_out"])
        self.assertTrue(record["commands"][0]["process_identity"].startswith("pidns:"))

    def test_timeout_large_output_retains_bounded_stream_and_counts(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "failed")
        command = record["commands"][0]
        output = Path(command["output_path"]).read_bytes()
        self.assertTrue(command["timed_out"])
        self.assertTrue(command["output_truncated"])
        self.assertEqual(len(output), 1024 * 1024)
        self.assertGreaterEqual(command["output_original_bytes"], 2 * 1024 * 1024)
        self.assertIn(b"truncated by Asha verification", output)
        self.assertEqual(
            hashlib.sha256(output).hexdigest(), command["output_digest"],
        )

    def test_signal_fails_and_records_signal(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "failed")
        self.assertIsNotNone(record["commands"][0]["signal"])

    def test_nonzero_required_command_fails(self) -> None:
        record = self._run()
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["commands"][0]["exit_code"], 7)

    def test_materialization_mutation_fails(self) -> None:
        record = self._run(mutate=True)
        self.assertEqual(record["state"], "failed")
        detail = json.loads(self.store.read_evidence(
            self.initiative_id, record["evidence_ids"][0],
        )["summary"])
        self.assertTrue(detail["mutation"])

    def test_pre_identity_probe_failure_is_recorded_as_indeterminate(self) -> None:
        record = self._run(fail_on_inspection=1)
        command = record["commands"][0]
        self.assertEqual(record["state"], "failed")
        self.assertEqual(command["pre_identity_status"], "indeterminate")
        self.assertIsNone(command["pre_identity_digest"])
        self.assertIsNone(command["pre_jj_commit_id"])
        self.assertIsNone(command["pre_tree_digest"])
        self.assertEqual(command["post_identity_status"], "observed")

    def test_post_identity_probe_failure_is_recorded_as_indeterminate(self) -> None:
        record = self._run(fail_on_inspection=2)
        command = record["commands"][0]
        self.assertEqual(record["state"], "failed")
        self.assertEqual(command["pre_identity_status"], "observed")
        self.assertEqual(command["post_identity_status"], "indeterminate")
        self.assertIsNone(command["post_identity_digest"])
        self.assertIsNone(command["post_jj_commit_id"])
        self.assertIsNone(command["post_tree_digest"])

    def test_evidence_is_immutable(self) -> None:
        record = self._run()
        evidence_id = record["evidence_ids"][0]
        evidence = self.store.read_evidence(
            self.initiative_id, evidence_id,
        )
        changed = copy.deepcopy(evidence)
        changed["summary"] = "changed"
        changed["digest"] = hashlib.sha256(b"changed").hexdigest()
        with self.assertRaises(StoreError):
            self.store.save_evidence(self.initiative_id, changed)
        with self.assertRaises(StoreError):
            self.store.finalize_reserved_output(
                self.initiative_id, evidence_id, b"changed",
            )

    def test_corrupted_evidence_digest_is_rejected_on_read(self) -> None:
        record = self._run()
        evidence_id = record["evidence_ids"][0]
        path = (
            self.store.config.initiatives_dir / self.initiative_id
            / "evidence" / f"{evidence_id}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["summary"] = "corrupted"
        path.write_text(
            json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(StoreError):
            self.store.read_evidence(self.initiative_id, evidence_id)

    def test_materializer_failure_reconciles_without_worker_dispatch_fields(self) -> None:
        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        with self._intent_plan_patch(), mock.patch(
            "lib.control.orchestration.verification.run_verification",
            side_effect=ValueError("materializer failed"),
        ):
            retained = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(retained["state"], "indeterminate")
        self.assertEqual(action_outcome(retained)["node_id"], "verify-a")

        reconciled = reconcile_actions(self.store, self.initiative_id)

        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        self.assertEqual(
            action_outcome(reconciled["actions"][0])["status"], "indeterminate",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )

    def test_materializer_failure_retains_durable_intent_and_terminal_fact(self) -> None:
        adapter = VerificationJj(self.repo, self.candidate)

        def fail_materializer(*args, **kwargs):
            del args, kwargs
            raise OSError("injected materializer failure")

        record = run_verification(
            self.store, self.initiative_id, "verify-a", jj=adapter,
            materializer=fail_materializer,
        )

        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["outcome"], "failed")
        self.assertEqual(record["commands"], [])
        self.assertTrue(record["materialization_path"].endswith(
            record["verification_id"][:8]
        ))
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "failed",
        )
        self.assertEqual(
            len(self.store.list_verifications_snapshot(self.initiative_id)), 1,
        )

    def test_interrupted_running_verification_is_indeterminate_and_retryable(self) -> None:
        def interrupt(store, action, payload):
            del payload
            verification_id = action_outcome(action)["verification_id"]
            record = store.read_verification(self.initiative_id, verification_id)
            running = copy.deepcopy(record)
            running.update({"state": "running", "updated_at": now_text()})
            store.save_verification(
                self.initiative_id, running, expected_digest=record_digest(record),
            )
            raise ValueError("controller interrupted")

        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        with self._intent_plan_patch(), mock.patch(
            "lib.control.orchestration.actions._execute_local",
            side_effect=interrupt,
        ):
            retained = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(retained["state"], "indeterminate")

        with mock.patch(
            "lib.control.orchestration.actions.append_event",
            side_effect=OSError("injected interrupted node-ready event failure"),
        ):
            with self.assertRaisesRegex(StoreError, "node-ready event"):
                reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )

        reconciled = reconcile_actions(self.store, self.initiative_id)

        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        self.assertEqual(
            action_outcome(reconciled["actions"][0])["status"], "indeterminate",
        )
        interrupted = self.store.list_verifications_snapshot(self.initiative_id)[0]
        self.assertEqual(interrupted["state"], "indeterminate")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )
        events = self.store.list_events_snapshot(self.initiative_id)
        self.assertEqual(
            sum(
                event["type"] == "node-ready"
                and interrupted["verification_id"] in event["subject_ids"]
                for event in events
            ),
            1,
        )

        passed = self._run()

        self.assertEqual(passed["state"], "passed")
        records = self.store.list_verifications_snapshot(self.initiative_id)
        self.assertEqual({item["state"] for item in records}, {"stale", "passed"})

    def test_pending_intent_before_node_transition_does_not_fabricate_event(self) -> None:
        adapter = VerificationJj(self.repo, self.candidate)
        with mock.patch.object(
            self.store, "save_node",
            side_effect=OSError("injected node transition failure"),
        ):
            with self.assertRaisesRegex(OSError, "node transition"):
                prepare_verification_intent(
                    self.store, self.initiative_id, "verify-a", jj=adapter,
                )
        retained = self.store.list_verifications_snapshot(self.initiative_id)
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["state"], "pending")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )
        self.assertFalse(any(
            event["type"] == "node-ready"
            and retained[0]["verification_id"] in event["subject_ids"]
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))

    def test_terminal_verification_reconciliation_repairs_node_and_event(self) -> None:
        verification_ids: list[str] = []

        def interrupt(store, action, payload):
            del payload
            intent_id = action_outcome(action)["verification_id"]
            verification_ids.append(intent_id)
            record = store.read_verification(self.initiative_id, intent_id)
            running = copy.deepcopy(record)
            running.update({"state": "running", "updated_at": now_text()})
            store.save_verification(
                self.initiative_id, running, expected_digest=record_digest(record),
            )
            failed = copy.deepcopy(running)
            failed.update({
                "state": "failed", "outcome": "failed", "updated_at": now_text(),
            })
            store.save_verification(
                self.initiative_id, failed, expected_digest=record_digest(running),
            )
            raise ValueError("controller interrupted after terminal record")

        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "verify-a"},
        )
        with self._intent_plan_patch(), mock.patch(
            "lib.control.orchestration.actions._execute_local",
            side_effect=interrupt,
        ):
            retained = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(retained["state"], "indeterminate")
        verification_id = verification_ids[0]

        with mock.patch(
            "lib.control.orchestration.actions.append_event",
            side_effect=OSError("injected terminal node event failure"),
        ):
            with self.assertRaisesRegex(StoreError, "node event"):
                reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "failed",
        )

        reconciled = reconcile_actions(self.store, self.initiative_id)

        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "failed",
        )
        events = self.store.list_events_snapshot(self.initiative_id)
        self.assertTrue(any(
            event["type"] == "node-state-changed"
            and verification_id in event["subject_ids"]
            for event in events
        ))
        self.assertTrue(any(
            event["type"] == "verification-finished"
            and verification_id in event["subject_ids"]
            for event in events
        ))


if __name__ == "__main__":
    unittest.main()
