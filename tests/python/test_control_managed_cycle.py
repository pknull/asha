"""One assignment through question, worker and sealed result without a UI.

Provider reasoning and worker launch/VCS are deterministic fixtures. Session
ownership, question IPC, actions, result staging/ingestion/sealing and delivery
use the production paths. This is backend acceptance, not native model evidence.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from lib.control.orchestration import coordinator
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.ingestion import ingest_result, result_ingestion_id
from lib.control.orchestration.seals import prepare_and_publish_seal
from lib.control.session_store import SessionStore
from lib.control.sessions import bridge_initiative, overview, run_owner
from lib.control.store import StoreError, TaskStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_control_managed_coordinator import NoTmux
from tests.python import test_orchestration_result_ingestion as ingestion_fixtures


class ManagedCycleTests(ExecutionFixture, unittest.TestCase):
    body = ingestion_fixtures.ResultIngestionTests.body
    verifier = ingestion_fixtures.ResultIngestionTests.verifier

    def setUp(self):
        super().setUp()
        self.sessions = SessionStore(self.config.control, create=True)
        self.addCleanup(self.sessions.close)
        self.sid = self.sessions.create(cwd=str(self.repo), prompt="Review chapter two", initiative_id=self.initiative_id)["session_id"]
        session = self.sessions.claim_owner(self.sid)
        self.actor_env = {**self.env, "ASHA_MANAGED_SESSION_ID": self.sid,
            "ASHA_MANAGED_GENERATION": str(session["generation"]),
            "ASHA_MANAGED_STATE_DIR": str(self.config.control.tasks_dir.parent)}
        self.record = coordinator.claim(self.store, self.initiative(), env=self.actor_env,
            tmux=NoTmux(), harness="claude")
        self.prompts = []
        self.dispatches = 0
        self.worker_token = None
        self.question_receipt = None
        self.code_root = Path(__file__).resolve().parents[2]

    def capture_worker(self, argv, **kwargs):
        self.dispatches += 1
        self.worker_token = kwargs["env"]["ASHA_CONTROL_RESULT_TOKEN"]
        payload = self.control_payload(argv)
        workspace = self.config.control.workspace_root / payload["task"]["task_id"]
        payload["task"]["jj"]["workspace_path"] = str(workspace)
        payload["task"]["jj"]["base_commit_id"] = "b" * 40
        payload["workspace"]["path"] = str(workspace)
        self.task = payload["task"]
        return 0, json.dumps(payload).encode(), b""

    def factory(self, _argv, **kwargs):
        fixture = self
        class Provider:
            input_not_submitted = False

            def events(self, prompt, **_kwargs):
                fixture.prompts.append(prompt)
                yield "initialized", {"native_id": "cycle-thread"}
                if len(fixture.prompts) == 1:
                    fixture.assertIn("Review chapter two", prompt)
                    fixture.assertIn(f"Read `asha initiative show {fixture.initiative_id} --json`", prompt)
                    script = "from lib.control.sessions import main; raise SystemExit(main(['ask','--question','Which tone?','--json']))"
                    child = subprocess.run([sys.executable, "-c", script],
                        env={**kwargs["env"], "PYTHONPATH": str(fixture.code_root)},
                        capture_output=True, text=True, timeout=10)
                    fixture.assertEqual(child.returncode, 0, child.stderr)
                    fixture.question_receipt = json.loads(child.stdout)
                elif len(fixture.prompts) == 2:
                    fixture.assertIn("Quiet", prompt)
                    current = fixture.store.current_coordinator(fixture.initiative_id)
                    coordinator.require_anchored_caller(current, kwargs["env"], NoTmux())
                    action = build_action_document(fixture.initiative(), "dispatch-node", {"node_id": "implementation-a"},
                        actor_id=coordinator.actor_id(current), coordinator=current)
                    with mock.patch("lib.control.orchestration.scheduler.storage_report", return_value={"pause_recommended": False}), \
                         mock.patch("lib.control.orchestration.scheduler.capture_bytes", side_effect=fixture.capture_worker):
                        one = submit_action(fixture.store, fixture.initiative_id, action)
                        two = submit_action(fixture.store, fixture.initiative_id, action)
                    fixture.assertEqual(one, two)
                    fixture.assertEqual(one["state"], "completed")
                elif len(fixture.prompts) == 3:
                    fixture.assertIn("seal-published", prompt)
                    seals = fixture.store.list_seals_snapshot(fixture.initiative_id)
                    fixture.assertEqual(len(seals), 1)
                    fixture.assertEqual(seals[0]["outcome"], "success")
                    yield "text", {"text": "Chapter work verified and sealed; downstream review remains."}
                else:
                    fixture.fail("unexpected extra coordinator turn")
                yield "completed", {"reason": "fixture completed"}
        return Provider()

    def tick(self):
        values = {key: value for key, value in os.environ.items()
                  if not key.startswith(("ASHA_", "TMUX"))}
        values.update(self.env)
        self.assertEqual(run_owner(self.config.control, self.sid, env=values,
                                   once=True, transport_factory=self.factory), 0)

    def stage_and_seal_worker(self):
        self.attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.repo.chmod(0o700)
        self.workspace = Path(self.task["jj"]["workspace_path"])
        (self.workspace / "lib/control/orchestration").mkdir(parents=True)
        current = self.workspace
        while current != Path(self.env["ASHA_HOME"]):
            current.chmod(0o700)
            current = current.parent
        TaskStore(self.config.control).save(self.task)
        self.ingestion = self.store.read_result_ingestion(self.initiative_id, result_ingestion_id(self.attempt["attempt_id"]))
        result_path = self.workspace / "result.json"
        result_path.write_text(json.dumps(self.body(summary="Chapter work finished")))
        result_path.chmod(0o600)
        worker_env = {key: value for key, value in os.environ.items()
                      if not key.startswith(("ASHA_", "TMUX"))}
        worker_env.update({**self.env, "ASHA_CONTROL_MANAGED": "1", "ASHA_CONTROL_TASK_ID": self.task["task_id"],
            "ASHA_CONTROL_RUN_ID": self.task["runs"][0]["run_id"], "ASHA_CONTROL_RESULT_INGESTION_ID": self.ingestion["ingestion_id"],
            "ASHA_CONTROL_RESULT_OUTBOX": str(self.workspace / self.ingestion["outbox_path"]),
            "ASHA_CONTROL_RESULT_TOKEN": self.worker_token, "TMUX_PANE": "%1", "PYTHONPATH": str(self.code_root)})
        script = '''import sys
from pathlib import Path
from unittest import mock
from lib.control.orchestration.cli import task_main
from lib.control.tmux import TmuxError
Path('lib/control/orchestration/result.py').write_text('changed\\n')
with mock.patch('lib.control.orchestration.ingestion.TmuxAdapter') as adapter:
    adapter.return_value.pane_facts.side_effect = TmuxError('fixture worker uses reserved token')
    raise SystemExit(task_main(['report','--file',sys.argv[1],'--json']))
'''
        child = subprocess.run([sys.executable, "-c", script, str(result_path)], env=worker_env,
            cwd=self.workspace, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)["phase"], "staged")
        self.jj = ingestion_fixtures.SnapshotJj(self.task)
        observed = ingestion_fixtures.ResultIngestionTests._terminal_observed(self.task)
        receipt = ingest_result(self.store, self.initiative_id, self.ingestion["ingestion_id"],
            control_store=TaskStore(self.config.control), jj=self.jj,
            terminal_reconciliation=observed, verifier=self.verifier)
        self.assertEqual(receipt["phase"], "completed")
        seal = prepare_and_publish_seal(self.store, self.initiative_id, self.attempt["attempt_id"],
            TaskStore(self.config.control).peek(self.task["task_id"]), observed, jj=self.jj)
        self.assertEqual(seal["result_id"], receipt["result_id"])

    def test_assignment_question_answer_worker_result_has_no_duplicate_turn_or_dispatch(self):
        self.tick()
        self.assertEqual(self.sessions.get(self.sid)["state"], "waiting-input")
        self.assertEqual(overview(self.config.control)["questions"], 1)
        self.assertEqual(self.dispatches, 0)
        # Repeated scheduling while waiting never asks the same question again.
        self.tick()
        self.assertEqual(len(self.prompts), 1)
        request = self.sessions.get_request(self.question_receipt["request_id"])
        with self.assertRaises(StoreError):
            self.sessions.answer(request["request_id"], "Quiet", expected_digest="stale")
        for _ in range(2):
            self.sessions.answer(request["request_id"], "Quiet", expected_digest=request["digest"])
        self.tick()
        self.assertEqual(self.dispatches, 1)
        self.assertEqual(overview(self.config.control)["questions"], 0)
        self.stage_and_seal_worker()
        self.tick()
        self.tick()
        self.assertEqual(self.dispatches, 1)
        self.assertEqual(self.sessions.get(self.sid)["turns"], 3)
        self.assertEqual(len(self.prompts), 3)
        snapshot = self.sessions.snapshot(self.sid)
        text = [e["payload"].get("text", "") for e in snapshot["events"] if e["kind"] == "text"]
        self.assertEqual(text, ["Chapter work verified and sealed; downstream review remains."])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(len(self.store.list_results_snapshot(self.initiative_id)), 1)
        self.assertEqual(len(self.store.list_seals_snapshot(self.initiative_id)), 1)
