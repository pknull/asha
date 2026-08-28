from __future__ import annotations

import ast
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.harness import process_identity
from lib.control.jj import ImmutableTree, WorkspaceIdentity
from lib.control.reconcile import Evidence, LiveAdapters
from lib.control.store import TaskStore
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.cli import reconcile_one_initiative
from lib.control.orchestration.ingestion import (
    _save_verification_evidence,
    ingest_pending_results,
    result_ingestion_id,
    stage_result,
)
from lib.control.orchestration.supervisor import tick
from lib.control.orchestration.supervisor_daemon import (
    SUPERVISOR_SERVICE_MARKER,
    install_supervisor_service,
    render_supervisor_service,
    run_supervisor,
    status_path,
    supervisor_service_path,
    supervisor_service_status,
    supervisor_lock_path,
    uninstall_supervisor_service,
)
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
        self.before_tree = ImmutableTree(
            commit_id=self.before, digest="d" * 64,
            entries=(("lib/control/orchestration/result.py", "100644", "f" * 40),),
        )
        self.final_tree = ImmutableTree(
            commit_id=self.after, digest="e" * 64,
            entries=(("lib/control/orchestration/result.py", "100644", "f" * 40),),
        )

    def inspect_workspace(
        self, path, name, *, snapshot=False, require_empty=True,
        exclude_control_transport=False,
    ) -> WorkspaceIdentity:
        del path, require_empty
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
        if commit_id == self.before:
            return self.before_tree
        raise AssertionError(f"unexpected commit {commit_id}")


class TerminalAdapters(LiveAdapters):
    def __init__(self, jj, state="exited"):
        super().__init__(config=None, tmux=mock.Mock(), jj=jj)
        self.state = state

    def tmux(self, task, run):
        del task, run
        return Evidence("tmux", "missing", "owned pane exited")

    def process(self, task, run):
        del task, run
        return Evidence(
            "process", "missing", "owned process ended", state=self.state,
        )

    def jj(self, task):
        del task
        return Evidence("jj", "match", "workspace identity matched")

    def event(self, task, run):
        del task, run
        return Evidence("event", "missing", "no semantic event")


class TeardownRaceAdapters(TerminalAdapters):
    def tmux(self, task, run):
        if self.state in {"conflict", "working"}:
            return Evidence("tmux", "match", "owned pane matched")
        return super().tmux(task, run)

    def process(self, task, run):
        if self.state in {"conflict", "working"}:
            return Evidence("process", "match", "owned process matched")
        return super().process(task, run)

    def event(self, task, run):
        if self.state == "conflict":
            return Evidence(
                "event", "match", "stale terminal snapshot", state="exited",
            )
        if self.state == "working":
            return Evidence("event", "match", "worker active", state="working")
        return super().event(task, run)


class SupervisorTickTests(ExecutionFixture, unittest.TestCase):
    task: dict

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
        self.workspace = Path(self.task["jj"]["workspace_path"])
        (self.workspace / "lib/control/orchestration").mkdir(parents=True)
        (self.workspace / "lib/control/orchestration/result.py").write_text("changed\n")
        self.repo.chmod(0o700)
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

    def body(self) -> dict:
        return {
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

    def verifier(self, store, ingestion, _task, _body, commit, tree, **_kwargs):
        return [_save_verification_evidence(
            store, self.initiative_id, ingestion["ingestion_id"], {
                "kind": "snapshot-integrity",
                "claimed_commit_id": commit,
                "claimed_tree_digest": tree,
                "status": "verified",
            },
        )]

    def deps(self, at: datetime, adapters=None):
        control = TaskStore(self.config.control)
        adapters = adapters or TerminalAdapters(self.jj)

        def ingest(store, initiative_id, *, control_store):
            with mock.patch(
                "lib.control.orchestration.ingestion.verify_controller_snapshot",
                side_effect=self.verifier,
            ):
                return ingest_pending_results(
                    store, initiative_id, control_store=control_store,
                    adapters_factory=lambda _task: adapters,
                )

        def reconcile(store, initiative_id, *, control_store, now):
            with mock.patch(
                "lib.control.orchestration.scheduler.storage_report",
                return_value={"pause_recommended": False},
            ), mock.patch(
                "lib.control.orchestration.ingestion.verify_controller_snapshot",
                side_effect=self.verifier,
            ):
                return reconcile_one_initiative(
                    store, initiative_id, control_store=control_store,
                    adapters_factory=lambda _task: adapters, now=now,
                )

        return SimpleNamespace(
            store_factory=lambda _initiative_id: self.store,
            control_store=control,
            now=lambda: at,
            reconcile=reconcile,
            ingest=ingest,
            list_initiatives=self.store.list_initiatives,
        )

    def test_tick_ingests_terminal_staged_candidate_and_reaches_ordinary_seal(self) -> None:
        stage_result(self.config, self.body(), self.managed)

        report = tick(self.deps(datetime.now(timezone.utc) + timedelta(seconds=1)))

        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        seals = self.store.list_seals_snapshot(self.initiative_id)
        self.assertEqual(retained["state"], "completed")
        self.assertEqual(attempts[0]["state"], "sealed-success")
        self.assertEqual([item["outcome"] for item in seals], ["success"])
        self.assertEqual(report["counts"]["errors"], 0)
        self.assertGreater(report["counts"]["transitions"], 0)

    def test_staged_candidate_survives_stale_teardown_evidence_until_terminal(self) -> None:
        stage_result(self.config, self.body(), self.managed)
        adapters = TeardownRaceAdapters(self.jj, state="conflict")
        first = datetime.now(timezone.utc) + timedelta(seconds=1)

        tick(self.deps(first, adapters))
        tick(self.deps(first + timedelta(seconds=1), adapters))

        attempt = self.store.read_attempt(
            self.initiative_id, self.attempt["attempt_id"],
        )
        node = self.store.read_node(self.initiative_id, attempt["node_id"])
        conflicts = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "reconciliation-conflict"
            and attempt["attempt_id"] in event["subject_ids"]
        ]
        self.assertEqual(attempt["state"], "running")
        self.assertEqual(node["state"], "running")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(
            conflicts[0]["payload"]["reason"],
            "event: terminal state contradicts matched live process",
        )

        adapters.state = "exited"
        report = tick(self.deps(first + timedelta(seconds=2), adapters))

        retained = self.store.read_result_ingestion(
            self.initiative_id, self.ingestion["ingestion_id"],
        )
        attempt = self.store.read_attempt(
            self.initiative_id, self.attempt["attempt_id"],
        )
        self.assertEqual(retained["state"], "completed")
        self.assertEqual(attempt["state"], "sealed-success")
        self.assertEqual(
            [item["outcome"] for item in self.store.list_seals_snapshot(self.initiative_id)],
            ["success"],
        )
        self.assertEqual(report["counts"]["errors"], 0)

    def test_tick_expires_missing_result_grace_seals_failure_and_allocates_retry(self) -> None:
        self.store.config = replace(self.store.config, result_grace_seconds=1)
        first = datetime.now(timezone.utc) + timedelta(seconds=1)
        tick(self.deps(first))

        report = tick(self.deps(first + timedelta(seconds=2)))

        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual([item["state"] for item in attempts], ["sealed-failure", "allocated"])
        self.assertEqual(
            [item["outcome"] for item in self.store.list_seals_snapshot(self.initiative_id)],
            ["failure"],
        )
        self.assertIn(
            "result-missing",
            [item["type"] for item in self.store.list_events_snapshot(self.initiative_id)],
        )
        self.assertEqual(report["counts"]["errors"], 0)

    def test_result_staged_during_grace_wins_without_false_missing_failure(self) -> None:
        self.store.config = replace(self.store.config, result_grace_seconds=10)
        first = datetime.now(timezone.utc) + timedelta(seconds=1)
        tick(self.deps(first))
        stage_result(self.config, self.body(), self.managed)

        tick(self.deps(first + timedelta(seconds=1)))

        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "sealed-success",
        )
        self.assertNotIn(
            "result-missing",
            [item["type"] for item in self.store.list_events_snapshot(self.initiative_id)],
        )
        self.assertEqual(
            [item["outcome"] for item in self.store.list_seals_snapshot(self.initiative_id)],
            ["success"],
        )

    def test_tick_restart_is_idempotent(self) -> None:
        stage_result(self.config, self.body(), self.managed)
        deps = self.deps(datetime.now(timezone.utc) + timedelta(seconds=1))
        tick(deps)
        before = {
            "events": len(self.store.list_events_snapshot(self.initiative_id)),
            "seals": len(self.store.list_seals_snapshot(self.initiative_id)),
            "attempts": len(self.store.list_attempts_snapshot(self.initiative_id)),
        }

        second = tick(deps)

        after = {
            "events": len(self.store.list_events_snapshot(self.initiative_id)),
            "seals": len(self.store.list_seals_snapshot(self.initiative_id)),
            "attempts": len(self.store.list_attempts_snapshot(self.initiative_id)),
        }
        self.assertEqual(after, before)
        self.assertEqual(second["counts"]["transitions"], 0)

    def test_tick_over_unchanged_running_initiative_appends_no_events(self) -> None:
        adapters = TeardownRaceAdapters(self.jj, state="working")
        deps = self.deps(datetime.now(timezone.utc) + timedelta(seconds=1), adapters)
        tick(deps)
        before = len(self.store.list_events_snapshot(self.initiative_id))

        second = tick(deps)

        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before,
        )
        self.assertEqual(second["counts"]["transitions"], 0)


class SupervisorIsolationTests(unittest.TestCase):
    def test_one_initiative_exception_does_not_stop_the_sweep(self) -> None:
        ids = [
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ]
        initiatives = [
            {"initiative_id": item, "state": "running", "active_plan": {"revision": 1}}
            for item in ids
        ]
        stores = {}
        for initiative in initiatives:
            store = mock.Mock()
            store.peek.return_value = initiative
            store.list_attempts_snapshot.return_value = []
            store.list_nodes_snapshot.return_value = []
            store.list_events_snapshot.return_value = []
            store.config.supervisor_interval_seconds = 15
            store.config.result_grace_seconds = 120
            stores[initiative["initiative_id"]] = store
        swept = []

        def reconcile(_store, initiative_id, **_kwargs):
            swept.append(initiative_id)
            if initiative_id == ids[0]:
                raise RuntimeError("injected store failure for A")
            return {}

        report = tick(SimpleNamespace(
            store_factory=lambda initiative_id: stores[initiative_id],
            control_store=mock.Mock(),
            now=lambda: datetime.now(timezone.utc),
            reconcile=reconcile,
            ingest=lambda *_args, **_kwargs: [],
            list_initiatives=lambda: initiatives,
        ))

        self.assertEqual(swept, ids)
        self.assertEqual(report["counts"]["errors"], 1)
        self.assertIn("injected store failure for A", report["initiatives"][0]["error"])
        self.assertIsNone(report["initiatives"][1]["error"])


class SupervisorProcessTests(ExecutionFixture, unittest.TestCase):
    def test_run_changes_cwd_to_home_before_the_first_tick(self) -> None:
        observed = []

        def one_tick(_deps):
            observed.append(Path.cwd())
            os.kill(os.getpid(), signal.SIGTERM)
            return {"finished_at": now_text(), "counts": {"transitions": 0}}

        with contextlib.chdir(self.repo), \
                mock.patch(
                    "lib.control.orchestration.supervisor_daemon.Path.home",
                    return_value=Path(self.env["HOME"]),
                ), \
                mock.patch(
                    "lib.control.orchestration.supervisor_daemon.tick",
                    side_effect=one_tick,
                ), redirect_stdout(StringIO()):
            result = run_supervisor(self.config, deps=SimpleNamespace())

        self.assertEqual(result, 0)
        self.assertEqual(observed, [Path(self.env["HOME"])])

    def test_second_run_refuses_while_another_process_holds_the_flock(self) -> None:
        path = supervisor_lock_path(self.config)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        program = (
            "import fcntl,os,signal,sys\n"
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n"
            "fcntl.flock(fd,fcntl.LOCK_EX)\n"
            "print('ready',flush=True)\n"
            "signal.pause()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert child.stdout is not None
        assert child.stderr is not None
        child_stdout = child.stdout
        child_stderr = child.stderr

        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child_stdout.close()
            child_stderr.close()

        self.addCleanup(cleanup_child)
        self.assertEqual(child_stdout.readline().strip(), "ready")
        output = StringIO()

        with contextlib.chdir(self.repo), redirect_stdout(output):
            status = control_main(
                ["control", "supervisor", "run", "--json"], env=self.env,
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["message"], "already running")

    def test_stop_reports_stale_dead_process_identity_without_signalling(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        identity = None
        for _ in range(100):
            identity = process_identity(child.pid)
            if identity is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(identity)
        child.terminate()
        child.wait(timeout=5)
        path = status_path(self.config)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": child.pid,
            "process_identity": identity,
            "started_at": now_text(),
            "last_tick_at": None,
            "last_tick_summary": None,
        }) + "\n")
        path.chmod(0o600)
        output = StringIO()

        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.os.kill",
        ) as kill, redirect_stdout(output):
            result = control_main(
                ["control", "supervisor", "stop", "--json"], env=self.env,
            )

        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())["signalled"])
        self.assertIn("stale", json.loads(output.getvalue())["message"])
        kill.assert_not_called()


class SupervisorServiceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.home.mkdir(mode=0o700)
        self.home.chmod(0o700)
        self.env = {
            "HOME": str(self.home),
            "ASHA_HOME": str(self.home / ".asha"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "USER": "keeper",
        }
        Path(self.env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)
        Path(self.env["XDG_RUNTIME_DIR"]).chmod(0o700)
        self.asha_root = Path("/opt/asha")
        self.config = SimpleNamespace()
        self.calls: list[list[str]] = []

    def which(self, command: str) -> str | None:
        if command in {"systemctl", "loginctl"}:
            return f"/usr/bin/{command}"
        return None

    def runner(self, argv, **_kwargs):
        self.calls.append(list(argv))
        stdout = b"Linger=yes\n" if Path(argv[0]).name == "loginctl" else b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    def expected_unit(self, asha_home_line: str = "") -> str:
        return (
            "[Unit]\n"
            f"{SUPERVISOR_SERVICE_MARKER}\n"
            "Description=Asha Control supervisor\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            'Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
            f"{asha_home_line}"
            "ExecStart=/opt/asha/bin/asha control supervisor run\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "WorkingDirectory=%h\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def test_unit_body_renders_exactly_for_default_and_nondefault_asha_home(self) -> None:
        self.assertEqual(
            render_supervisor_service(self.env, self.asha_root),
            self.expected_unit(),
        )

        custom = dict(self.env, ASHA_HOME=str(self.root / "custom-asha"))
        self.assertEqual(
            render_supervisor_service(custom, self.asha_root),
            self.expected_unit(
                f'Environment="ASHA_HOME={self.root / "custom-asha"}"\n',
            ),
        )

    def test_install_refuses_foreign_unit_and_replaces_owned_unit(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "foreign unit"):
            install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=self.runner, which=self.which,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), "[Unit]\nDescription=foreign\n")
        self.assertEqual(self.calls, [])

        path.write_text(
            f"[Unit]\n{SUPERVISOR_SERVICE_MARKER}\nDescription=old\n",
            encoding="utf-8",
        )
        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.stop_supervisor",
            return_value=({"running": False}, 1),
        ), mock.patch(
            "lib.control.orchestration.supervisor_daemon._lock_held",
            return_value=False,
        ):
            payload, code = install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=self.runner, which=self.which,
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["linger_enabled"])
        self.assertIn("without user lingering", payload["message"].lower())
        self.assertIn("with lingering it starts at boot", payload["message"])
        self.assertEqual(path.read_text(encoding="utf-8"), self.expected_unit())

    def test_install_stops_manual_supervisor_before_enable_now(self) -> None:
        ordering: list[str] = []

        def stop(_config):
            ordering.append("stop")
            return {"running": False, "message": "stopped"}, 0

        def runner(argv, **kwargs):
            ordering.append(" ".join(argv[1:]))
            return self.runner(argv, **kwargs)

        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.stop_supervisor",
            side_effect=stop,
        ), mock.patch(
            "lib.control.orchestration.supervisor_daemon._lock_held",
            return_value=False,
        ):
            install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=runner, which=self.which,
            )

        # The bus must be proven reachable (daemon-reload) BEFORE the manual
        # supervisor is stopped, and the stop must precede enable --now: a
        # dead bus must never leave the plane with no supervisor at all.
        self.assertLess(
            ordering.index("--user daemon-reload"), ordering.index("stop"),
        )
        self.assertLess(ordering.index("stop"), ordering.index(
            "--user enable --now asha-supervisor.service",
        ))

    def test_install_refuses_while_the_single_instance_lock_remains_held(self) -> None:
        path = supervisor_service_path(self.env)
        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.stop_supervisor",
            return_value=({"running": False, "message": "not running"}, 1),
        ), mock.patch(
            "lib.control.orchestration.supervisor_daemon._lock_held",
            return_value=True,
        ):
            with self.assertRaisesRegex(ValueError, "still stopping"):
                install_supervisor_service(
                    self.config, self.env, asha_root=self.asha_root,
                    runner=self.runner, which=self.which,
                )

        # Bus-first ordering: daemon-reload proves the bus before the manual
        # supervisor is touched, so exactly that one call is expected; the
        # refusal restores the filesystem as found and never reaches enable.
        self.assertFalse(path.exists())
        self.assertEqual(
            [call[1:] for call in self.calls],
            [["--user", "daemon-reload"]],
        )

    def test_uninstall_removes_only_owned_unit_and_is_idempotent(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "foreign unit"):
            uninstall_supervisor_service(
                self.env, runner=self.runner, which=self.which,
            )
        self.assertTrue(path.exists())
        self.assertEqual(self.calls, [])

        path.write_text(self.expected_unit(), encoding="utf-8")
        first, first_code = uninstall_supervisor_service(
            self.env, runner=self.runner, which=self.which,
        )
        second, second_code = uninstall_supervisor_service(
            self.env, runner=self.runner, which=self.which,
        )

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertFalse(path.exists())
        self.assertTrue(first["removed"])
        self.assertFalse(second["removed"])

    def test_install_dry_run_writes_nothing_and_prints_commands(self) -> None:
        path = supervisor_service_path(self.env)
        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.stop_supervisor",
        ) as stop:
            payload, code = install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root, dry_run=True,
                runner=self.runner, which=self.which,
            )

        self.assertEqual(code, 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.calls, [])
        stop.assert_not_called()
        self.assertIn(self.expected_unit(), payload["message"])
        self.assertIn("would run: systemctl --user daemon-reload", payload["message"])
        self.assertIn(
            "would run: systemctl --user enable --now asha-supervisor.service",
            payload["message"],
        )

        output = StringIO()
        with redirect_stdout(output):
            cli_code = control_main(
                ["control", "supervisor", "install", "--dry-run"], env=self.env,
            )
        self.assertEqual(cli_code, 0)
        self.assertTrue(output.getvalue().startswith(f"would write {path}:\n[Unit]\n"))
        self.assertNotIn("asha control supervisor:", output.getvalue())
        self.assertFalse(path.exists())

    def test_service_status_uses_is_enabled_and_is_active(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text(self.expected_unit(), encoding="utf-8")

        def runner(argv, **_kwargs):
            self.calls.append(list(argv))
            returncode = 3 if argv[2] == "is-active" else 0
            return subprocess.CompletedProcess(argv, returncode, b"", b"")

        status = supervisor_service_status(
            self.env, runner=runner, which=self.which,
        )

        self.assertEqual(status, {
            "service_present": True,
            "service_enabled": True,
            "service_active": False,
        })
        self.assertEqual(self.calls, [
            ["/usr/bin/systemctl", "--user", "is-enabled", "asha-supervisor.service"],
            ["/usr/bin/systemctl", "--user", "is-active", "asha-supervisor.service"],
        ])

    def test_service_status_fields_are_null_without_systemctl(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text(self.expected_unit(), encoding="utf-8")

        status = supervisor_service_status(
            self.env, runner=self.runner, which=lambda _command: None,
        )

        self.assertEqual(status, {
            "service_present": None,
            "service_enabled": None,
            "service_active": None,
        })
        self.assertEqual(self.calls, [])

        output = StringIO()
        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.shutil.which",
            return_value=None,
        ), redirect_stdout(output):
            code = control_main(
                ["control", "supervisor", "status", "--json"], env=self.env,
            )
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertIsNone(payload["service_present"])
        self.assertIsNone(payload["service_enabled"])
        self.assertIsNone(payload["service_active"])

        output = StringIO()
        with mock.patch(
            "lib.control.orchestration.supervisor_daemon.shutil.which",
            return_value=None,
        ), redirect_stdout(output):
            control_main(["control", "supervisor", "status"], env=self.env)
        self.assertIn(
            "service present=unknown, enabled=unknown, active=unknown",
            output.getvalue(),
        )


class SupervisorBoundaryTests(unittest.TestCase):
    def test_supervisor_has_no_hitl_authority_surface(self) -> None:
        import lib.control.orchestration.supervisor as supervisor

        forbidden = {
            "submit_action", "approve_plan", "approve_salvage",
            "record_integration", "archive", "finalize",
        }
        self.assertTrue(forbidden.isdisjoint(supervisor.__dict__))
        source = Path(supervisor.__file__).read_text()
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        self.assertTrue(forbidden.isdisjoint(imported))
        tick_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "tick"
        )
        dependency_calls = {
            node.func.attr
            for node in ast.walk(tick_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "deps"
        }
        self.assertEqual(
            dependency_calls,
            {"store_factory", "now", "reconcile", "ingest", "list_initiatives"},
        )


if __name__ == "__main__":
    unittest.main()
