from __future__ import annotations

import json
import copy
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.orchestration.actions import (
    build_action_document,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.scheduler import SchedulerError
from lib.control.jj import JjAdapter
from lib.control.orchestration.cli import _approve, _create, _plan
from lib.control.orchestration.config import load_config
from lib.control.orchestration.store import InitiativeStore
from lib.control.store import TaskStore
from tests.python.test_orchestration_graph import valid_plan
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class OrchestrationDispatchTests(ExecutionFixture, unittest.TestCase):
    def fake_capture(self, calls: list[list[str]], tasks: list[dict]):
        def run(argv, **_kwargs):
            calls.append(list(argv))
            payload = self.control_payload(argv, existing=len(calls) > 1)
            tasks.append(payload["task"])
            return 0, json.dumps(payload, sort_keys=True).encode(), b""

        return run

    def fake_asha(self) -> tuple[Path, Path]:
        root = self.root / "fake-asha-root"
        executable = root / "bin" / "asha"
        executable.parent.mkdir(parents=True)
        log = self.root / "fake-asha-argv.json"
        repository_root = str(Path(__file__).resolve().parents[2])
        executable.write_text("""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path
""" + f"sys.path.insert(0, {repository_root!r})\n" + """
from tests.python.test_control_config_model import task_record

argv = sys.argv[1:]
Path(os.environ["ASHA_FAKE_ARGV"]).write_text(json.dumps(argv))
mode = os.environ.get("ASHA_FAKE_MODE", "success")
if mode == "exit-two":
    print("deliberate refusal", file=sys.stderr)
    raise SystemExit(2)
if mode == "hang":
    time.sleep(10)
task_id = argv[argv.index("--task-id") + 1]
repo = argv[argv.index("--repo") + 1]
goal = argv[argv.index("--goal") + 1]
attempt_id = Path(goal.rsplit(" ", 1)[1]).stem
task = task_record(
    task_id=task_id,
    repository_root=repo,
    workspace_path=str(Path(os.environ["ASHA_FAKE_WORKSPACES"]) / task_id),
)
task["label"] = f"assignment {attempt_id}"
task["jj"]["requested_base"] = argv[argv.index("--base") + 1]
task["runs"][0]["harness"] = argv[argv.index("--harness") + 1]
task["runs"][0]["role"] = argv[argv.index("--role") + 1]
print(json.dumps({
    "contract": "asha.control-task-start.v1",
    "task": task,
    "run": task["runs"][0],
    "workspace": {
        "name": task["jj"]["workspace_name"],
        "path": task["jj"]["workspace_path"],
        "change_id": task["jj"]["change_id"],
    },
    "session": task["tmux"]["session"],
    "pane": task["runs"][0]["pane_id"],
    "attach": "tmux attach",
    "source_mutations": [],
    "existing": False,
}, sort_keys=True))
""")
        executable.chmod(0o700)
        return root, log

    def test_dispatch_uses_asha_executable_argv_without_shell(self) -> None:
        fake_root, log = self.fake_asha()
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        env = {
            "ASHA_ROOT": str(fake_root),
            "ASHA_FAKE_ARGV": str(log),
            "ASHA_FAKE_WORKSPACES": str(self.root / "fake-workspaces"),
            "PATH": f"{fake_root / 'bin'}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": "",
        }
        real_popen = subprocess.Popen
        with mock.patch.dict(os.environ, env), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.process.subprocess.Popen", wraps=real_popen,
        ) as popen:
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed", action["outcome"])
        argv = json.loads(log.read_text())
        self.assertEqual(argv[:2], ["task", "start"])
        self.assertEqual(argv.count("--task-id"), 1)
        self.assertEqual(argv[argv.index("--repo") + 1], str(self.repo))
        self.assertEqual(argv[-2], "--goal")
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_fake_asha_exit_two_is_launch_failed(self) -> None:
        fake_root, log = self.fake_asha()
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        env = {
            "ASHA_ROOT": str(fake_root),
            "ASHA_FAKE_ARGV": str(log),
            "ASHA_FAKE_WORKSPACES": str(self.root / "fake-workspaces"),
            "ASHA_FAKE_MODE": "exit-two",
            "PATH": f"{fake_root / 'bin'}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": "",
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "launch-failed",
        )

    def test_fake_asha_timeout_is_indeterminate(self) -> None:
        fake_root, log = self.fake_asha()
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        env = {
            "ASHA_ROOT": str(fake_root),
            "ASHA_FAKE_ARGV": str(log),
            "ASHA_FAKE_WORKSPACES": str(self.root / "fake-workspaces"),
            "ASHA_FAKE_MODE": "hang",
            "PATH": f"{fake_root / 'bin'}:{os.environ.get('PATH', '')}",
            "PYTHONPATH": "",
        }
        with mock.patch.dict(os.environ, env), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.DISPATCH_TIMEOUT_SECONDS", 1.0,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertTrue(log.exists())
        self.assertEqual(action["state"], "indeterminate")

    def test_dispatch_argv_assignment_link_and_action_replay(self) -> None:
        calls: list[list[str]] = []
        tasks: list[dict] = []
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ):
            action = submit_action(self.store, self.initiative_id, document)
            replay = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action, replay)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[1:4], ["task", "start", "--repo"])
        self.assertIn("--task-id", argv)
        self.assertEqual(argv[argv.index("--base") + 1], "b" * 40)
        self.assertEqual(argv[argv.index("--harness") + 1], "codex")
        self.assertEqual(argv[argv.index("--role") + 1], "implementer")
        self.assertIn("--detach", argv)
        self.assertIn("--json", argv)
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["state"], "running")
        assignment = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempts[0]['attempt_id']}.md"
        )
        self.assertEqual(stat.S_IMODE(assignment.stat().st_mode), 0o600)
        text = assignment.read_text()
        for expected in (
            self.initiative_id,
            "implementation-a",
            attempts[0]["attempt_id"],
            str(self.repo),
            "asha task report --file .asha/result.json",
            "Nested workflows are prohibited",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("Accepted review findings", text)
        self.assertLessEqual(len(assignment.read_bytes()), 32 * 1024)
        link = self.store.read_link(self.initiative_id, attempts[0]["attempt_id"])
        self.assertEqual(link["control_task_id"], attempts[0]["task_id"])
        self.assertEqual(link["action_id"], action["action_id"])
        self.assertEqual(link["expected_initiative_revision"], document["expected_state_revision"])

    def test_repair_dispatch_binds_the_accepted_findings_into_the_assignment(self) -> None:
        from tests.python.orchestration_increment3_fixtures import (
            advance_node, save_accepted_review, save_candidate,
        )

        advance_node(self, "implementation-a", ["ready", "dispatching", "running", "evaluating", "succeeded"])
        candidate = save_candidate(self)
        review = save_accepted_review(self, candidate, verdict="findings")
        advance_node(self, "implementation-a", ["ready"])
        calls: list[list[str]] = []
        tasks: list[dict] = []
        repair = build_action_document(
            self.initiative(), "repair-node",
            {"node_id": "implementation-a", "seal_id": candidate["seal_id"]},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ):
            repair_action = submit_action(self.store, self.initiative_id, repair)
            dispatch = build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            )
            submit_action(self.store, self.initiative_id, dispatch)
        self.assertEqual(repair_action["state"], "completed")
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        attempt = attempts[-1]
        self.assertEqual(attempt["base"]["policy"], "upstream-seal")
        self.assertEqual(
            [item["seal_id"] for item in attempt["base"]["seal_inputs"]],
            [candidate["seal_id"]],
        )
        text = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempt['attempt_id']}.md"
        ).read_text()
        self.assertIn("## Accepted review findings to fix", text)
        self.assertIn("fixing them IS the goal", text)
        self.assertIn(review["review_id"], text)
        self.assertIn(candidate["seal_id"], text)
        self.assertIn("Repair the exact candidate before verification.", text)
        self.assertIn("lib/file.py", text)

    def test_interactive_node_brief_requests_the_operator_close(self) -> None:
        calls: list[list[str]] = []
        tasks: list[dict] = []
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ):
            submit_action(self.store, self.initiative_id, document)
        self.assertNotIn("--headless", calls[0])
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        text = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempts[-1]['attempt_id']}.md"
        ).read_text()
        self.assertIn("you cannot end the session yourself", text)
        self.assertIn("the X key in asha control", text)

    def test_exit_two_is_proven_launch_failure(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            return_value=(2, b"", b"deliberate refusal"),
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertIn("launch-failed", action["outcome"])
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "launch-failed",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "evaluating",
        )

    def test_nonzero_with_creating_control_record_stays_indeterminate(self) -> None:
        argv_seen = []

        def refused(argv, **_kwargs):
            argv_seen.append(list(argv))
            return 2, b"", b"interrupted creation"

        control = mock.Mock()

        def peek(task_id):
            task = self.control_payload(argv_seen[0])["task"]
            self.assertEqual(task["task_id"], task_id)
            task["lifecycle"] = "creating"
            return task

        control.peek.side_effect = peek
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=refused,
        ), mock.patch(
            "lib.control.orchestration.scheduler.TaskStore", return_value=control,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "indeterminate", action["outcome"])
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(attempt["state"], "indeterminate")
        self.assertIn(f"asha task recover {attempt['task_id']}", action["outcome"])

    def test_hung_control_call_is_indeterminate_until_reconciled(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=SchedulerError("command timed out"),
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "indeterminate")
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "indeterminate",
        )

    def test_crash_between_control_response_and_link_replays_same_task_id(self) -> None:
        calls: list[list[str]] = []
        tasks: list[dict] = []
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        real_save_link = self.store.save_link
        save_calls = 0

        def flaky_save_link(*args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                raise OSError("injected crash after Control acknowledgement")
            return real_save_link(*args, **kwargs)

        fake_control_store = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ), mock.patch.object(
            self.store, "save_link", side_effect=flaky_save_link,
        ):
            action = submit_action(self.store, self.initiative_id, document)
            self.assertEqual(action["state"], "indeterminate")
            fake_control_store.peek.return_value = tasks[0]
            with mock.patch(
                "lib.control.orchestration.actions.TaskStore",
                return_value=fake_control_store,
            ):
                reconciled = reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        self.assertEqual(len(calls), 2)
        first_id = calls[0][calls[0].index("--task-id") + 1]
        second_id = calls[1][calls[1].index("--task-id") + 1]
        self.assertEqual(first_id, second_id)
        self.assertTrue(json.loads(reconciled["actions"][0]["outcome"])["existing"])
        self.assertEqual(len(self.store.list_links_snapshot(self.initiative_id)), 1)

    def test_process_death_in_dispatching_phase_is_reconciled_with_same_task_id(self) -> None:
        class SimulatedProcessDeath(BaseException):
            pass

        calls: list[list[str]] = []
        tasks: list[dict] = []
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        real_save_link = self.store.save_link
        save_calls = 0

        def die_before_first_link(*args, **kwargs):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 1:
                raise SimulatedProcessDeath
            return real_save_link(*args, **kwargs)

        fake_control_store = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ), mock.patch.object(
            self.store, "save_link", side_effect=die_before_first_link,
        ):
            with self.assertRaises(SimulatedProcessDeath):
                submit_action(self.store, self.initiative_id, document)
            retained = self.store.read_action(
                self.initiative_id, document["action_id"],
            )
            self.assertEqual(retained["state"], "dispatching")
            fake_control_store.peek.return_value = tasks[0]
            with mock.patch(
                "lib.control.orchestration.actions.TaskStore",
                return_value=fake_control_store,
            ):
                reconciled = reconcile_actions(self.store, self.initiative_id)

        action = reconciled["actions"][0]
        self.assertEqual(action["state"], "completed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0][calls[0].index("--task-id") + 1],
            calls[1][calls[1].index("--task-id") + 1],
        )
        self.assertEqual(len(self.store.list_links_snapshot(self.initiative_id)), 1)


@unittest.skipUnless(
    shutil.which("tmux") and shutil.which("jj") and shutil.which("git") and shutil.which("jq"),
    "real orchestration integration requires tmux, jj, git, and jq",
)

class HeadlessDispatchTests(ExecutionFixture, unittest.TestCase):
    """A node born headless dispatches with --headless and a structural-exit brief."""

    fake_capture = OrchestrationDispatchTests.fake_capture

    def customize_plan(self, plan_value: dict) -> None:
        plan_value["nodes"][0]["interactive"] = False  # codex has a headless mode

    def test_headless_node_dispatch_passes_headless_and_adapts_the_brief(self) -> None:
        calls: list[list[str]] = []
        tasks: list[dict] = []
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertIn("--headless", calls[0])
        stored = self.store.read_node(self.initiative_id, "implementation-a")
        self.assertIs(stored["interactive"], False)
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        text = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempts[-1]['attempt_id']}.md"
        ).read_text()
        self.assertIn("ends by itself when this turn completes", text)
        self.assertNotIn("you cannot end the session yourself", text)


class RealOrchestrationDispatchTests(unittest.TestCase):
    """One real Control/jj/tmux dispatch with an isolated default socket."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo_root = Path(__file__).resolve().parents[2]
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.tmux_tmp = self.root / "tmux"
        self.tmux_tmp.mkdir(mode=0o700)
        self.path_bin = self.root / "bin"
        self.path_bin.mkdir(mode=0o700)
        codex = self.path_bin / "codex"
        codex.write_text("#!/bin/sh\nexec /bin/sleep 3600\n")
        codex.chmod(0o700)
        codex_home = self.home / ".codex"
        codex_home.mkdir(mode=0o700)
        (codex_home / "config.toml").write_text("\n")
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "control.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "TMUX_TMPDIR": str(self.tmux_tmp),
            "PATH": f"{self.path_bin}:{os.environ.get('PATH', '')}",
            "ASHA_CODEX_CMD": str(codex),
            "ASHA_ROOT": str(self.repo_root),
        }
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        for key in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        tmux_probe = subprocess.run(
            ["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "asha-orchestration-probe", "sleep", "1"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        if tmux_probe.returncode != 0:
            self.skipTest("isolated tmux server unavailable in this execution sandbox")
        subprocess.run(
            ["tmux", "kill-server"], env=self.env, capture_output=True, check=False,
        )
        Path(self.env["ASHA_CONFIG"]).write_text("{}\n")
        Path(self.env["ASHA_CONFIG"]).chmod(0o600)
        installed = subprocess.run(
            ["bash", str(self.repo_root / "install.sh"), "--target", "codex"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        if installed.returncode != 0:
            self.skipTest(f"sandbox Codex install unavailable: {installed.stderr[:200]}")

        self.source = self.root / "source"
        self.source.mkdir(mode=0o755)
        git_env = {
            **self.env,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(self.source)], check=True, env=git_env)
        (self.source / "tracked.txt").write_text("base\n")
        (self.source / ".gitignore").write_text("/.asha/\n/Memory/\n/Work/\n")
        subprocess.run(
            ["git", "-C", str(self.source), "add", "tracked.txt", ".gitignore"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.source)],
            check=True, env=self.env, capture_output=True, text=True,
        )
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work/session-state").mkdir(parents=True)
        (self.source / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "orchestration-real-integration",
        }) + "\n")
        (self.source / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        )
        (self.source / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")
        self.config = load_config(self.env)
        self.store = InitiativeStore(self.config)

    def tearDown(self) -> None:
        if hasattr(self, "env"):
            subprocess.run(
                ["tmux", "kill-server"], env=self.env,
                capture_output=True, check=False,
            )

    def test_real_control_task_workspace_and_tmux_are_created_once(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=True):
            jj = JjAdapter()
            initiative = _create([
                "--repo", str(self.source), "--slug", "real-dispatch",
                "--label", "Real dispatch", "--objective", "Run one real worker.",
            ], self.config, self.store, jj)["initiative"]
            base_commit = subprocess.run(
                ["git", "-C", str(self.source), "rev-parse", "HEAD"],
                env=self.env, capture_output=True, text=True, check=True,
            ).stdout.strip()
            base_tree = jj.immutable_tree(self.source, base_commit)
            plan_value = valid_plan()
            plan_value["initiative_id"] = initiative["initiative_id"]
            plan_value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
            repository_id = initiative["scope"]["repository"]["repository_id"]
            for node in plan_value["nodes"]:
                if node["repository_id"] is not None:
                    node["repository_id"] = repository_id
                if node["base"] is not None:
                    node["base"]["scope_origin"] = {
                        "jj_commit_id": base_commit,
                        "tree_digest": base_tree.digest,
                    }
            plan_file = self.root / "real-plan.json"
            plan_file.write_text(json.dumps(plan_value))
            plan, _ = _plan(
                [initiative["initiative_id"], "--file", str(plan_file)],
                self.store, self.config, jj=jj,
            )
            approved, _ = _approve(
                [initiative["initiative_id"], "--digest", plan["digest"]], self.store,
            )
            activation = build_action_document(
                approved["initiative"], "activate-initiative", {},
            )
            with mock.patch(
                "lib.control.orchestration.actions.run_orchestration_doctor",
                return_value={
                    "contract": "asha.orchestration-doctor.v1", "ok": True,
                    "probes": [], "limitations": [],
                },
            ):
                activated = submit_action(self.store, initiative["initiative_id"], activation)
            self.assertEqual(activated["state"], "completed")
            dispatch_document = build_action_document(
                self.store.peek(initiative["initiative_id"]),
                "dispatch-node", {"node_id": "implementation-a"},
            )
            real_save_link = self.store.save_link
            link_writes = 0

            def fail_first_link(*args, **kwargs):
                nonlocal link_writes
                link_writes += 1
                if link_writes == 1:
                    raise OSError("injected link write fault")
                return real_save_link(*args, **kwargs)

            with mock.patch.object(
                self.store, "save_link", side_effect=fail_first_link,
            ):
                interrupted = submit_action(
                    self.store, initiative["initiative_id"], dispatch_document,
                )
                self.assertEqual(interrupted["state"], "indeterminate")
                reconciled = reconcile_actions(
                    self.store, initiative["initiative_id"],
                )
            action = reconciled["actions"][0]
            self.assertTrue(json.loads(action["outcome"])["existing"])
            replay = submit_action(
                self.store, initiative["initiative_id"], dispatch_document,
            )
            self.assertEqual(action, replay)
            self.assertEqual(action["state"], "completed")
            attempts = self.store.list_attempts_snapshot(initiative["initiative_id"])
            self.assertEqual(len(attempts), 1)
            links = self.store.list_links_snapshot(initiative["initiative_id"])
            self.assertEqual(len(links), 1, action["outcome"])
            control_store = TaskStore(self.config.control)
            self.assertEqual(len(control_store.list()), 1)
            task = control_store.peek(links[0]["control_task_id"])
            self.assertTrue(Path(task["jj"]["workspace_path"]).is_dir())
            identity = JjAdapter().inspect_workspace(
                Path(task["jj"]["workspace_path"]), task["jj"]["workspace_name"],
            )
            self.assertEqual(identity.parent_commit_ids, (task["jj"]["base_commit_id"],))
            self.assertEqual(subprocess.run(
                ["tmux", "has-session", "-t", task["tmux"]["session"]],
                env=self.env, capture_output=True, check=False,
            ).returncode, 0)
            stop = build_action_document(
                self.store.peek(initiative["initiative_id"]),
                "stop-attempt", {"attempt_id": attempts[0]["attempt_id"]},
            )
            stopped = submit_action(self.store, initiative["initiative_id"], stop)
            self.assertEqual(stopped["state"], "completed")


if __name__ == "__main__":
    unittest.main()
