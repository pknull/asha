from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.config import load_config
from lib.control.jj import RepositoryFacts
from lib.control.store import TaskStore
from tests.python.test_control_config_model import task_record
from tests.python.test_control_increment3 import FakeTmux


TASK_ID = "12345678-1234-4234-8234-123456789abc"


class ExistingTaskCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.store = TaskStore(self.config)

    def record(self, *, source_kind: str = "ad-hoc", number: int | None = None,
               requested_base: str = "trunk()") -> dict:
        workspace = self.config.workspace_root / "repo-key" / "create-by-id"
        workspace.mkdir(parents=True, exist_ok=True)
        value = task_record(
            task_id=TASK_ID,
            slug="create-by-id",
            repository_root=str(self.source),
            workspace_path=str(workspace),
        )
        value["label"] = "Create by id"
        value["source"] = {
            "kind": source_kind,
            "number": number,
            "url": (
                None if source_kind == "ad-hoc"
                else f"https://github.example/repo/{source_kind}/{number}"
            ),
        }
        value["jj"]["requested_base"] = requested_base
        value["jj"]["workspace_name"] = f"asha-create-by-id-{TASK_ID[:8]}"
        value["tmux"]["session"] = f"asha-create-by-id-{TASK_ID[:8]}"
        return value

    def invoke(self, arguments: list[str], jj) -> tuple[int, str, str, mock.Mock]:
        stdout, stderr = io.StringIO(), io.StringIO()
        tmux = mock.Mock(executable="tmux", socket=None)
        with mock.patch("lib.control.cli.JjAdapter", return_value=jj), \
                mock.patch("lib.control.cli.TmuxAdapter", return_value=tmux), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main(arguments, env=self.env)
        return status, stdout.getvalue(), stderr.getvalue(), tmux

    def discovery_only_jj(self):
        adapter = mock.Mock()
        adapter.discover_root.return_value = self.source
        adapter.preflight.side_effect = AssertionError("existing task must not run jj preflight")
        adapter.import_git.side_effect = AssertionError("existing task must not import Git")
        return adapter

    def test_bad_task_id_is_a_usage_error(self) -> None:
        for value in ("not-a-uuid", TASK_ID.upper()):
            with self.subTest(value=value):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = control_main([
                        "task", "start", "--task-id", value, "--goal", "Create by id",
                    ], env=self.env)
                self.assertEqual((status, stdout.getvalue()), (2, ""))
                self.assertIn("canonical UUID", stderr.getvalue())

    def test_supplied_id_is_used_for_new_record_and_json_reports_creation(self) -> None:
        workspace = self.config.workspace_root / "repo-key" / "create-by-id"
        workspace.mkdir(parents=True)
        task = self.record()
        result = {
            "task": task,
            "run": task["runs"][0],
            "session": task["tmux"]["session"],
            "pane": task["runs"][0]["pane_id"],
            "workspace": {
                "path": task["jj"]["workspace_path"],
                "name": task["jj"]["workspace_name"],
                "change_id": task["jj"]["change_id"],
            },
        }
        captured = {}

        def prepare(config, request, jj=None):
            captured["request"] = request
            return task

        def launch(config, prepared, **kwargs):
            captured["launch"] = kwargs
            self.store.save(task)
            return result

        jj = mock.Mock()
        jj.discover_root.return_value = self.source
        jj.preflight.return_value = RepositoryFacts(root=self.source, git_root=self.source)
        jj.working_copy_parent.return_value = "a" * 40
        jj.git_head.return_value = "a" * 40
        jj.import_git.return_value = ()
        with mock.patch(
            "lib.control.cli.prepare_task_workspace", side_effect=prepare,
        ), mock.patch(
            "lib.control.cli.launch_task", side_effect=launch,
        ), mock.patch(
            "lib.control.cli.new_uuid",
            side_effect=AssertionError("supplied ID must replace new_uuid"),
        ):
            status, stdout, stderr, _tmux = self.invoke([
                "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
                "--harness", "codex", "--goal", "Create by id", "--json",
            ], jj)

        self.assertEqual((status, stderr), (0, ""))
        self.assertFalse(json.loads(stdout)["existing"])
        self.assertEqual(captured["request"].task_id, TASK_ID)
        self.assertEqual(captured["request"].slug, "create-by-id")
        self.assertEqual(captured["launch"]["harness"], "codex")
        self.assertTrue((self.config.tasks_dir / f"{TASK_ID}.json").is_file())
        self.assertEqual(task["jj"]["workspace_name"], f"asha-create-by-id-{TASK_ID[:8]}")
        self.assertEqual(task["tmux"]["session"], f"asha-create-by-id-{TASK_ID[:8]}")

    def test_identical_request_returns_stored_task_without_mutation(self) -> None:
        record = self.record()
        path = self.store.save(record)
        before = path.read_bytes()
        jj = self.discovery_only_jj()

        status, stdout, stderr, tmux = self.invoke([
            "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
            "--harness", "codex", "--goal", "Create by id", "--json",
        ], jj)

        self.assertEqual((status, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertTrue(payload["existing"])
        self.assertEqual(payload["task"], record)
        self.assertEqual(payload["run"], record["runs"][0])
        self.assertEqual(payload["source_mutations"], [])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.store.read(TASK_ID)["runs"]), 1)
        jj.preflight.assert_not_called()
        jj.import_git.assert_not_called()
        tmux.create_task_session.assert_not_called()

    def test_identical_human_request_is_prefixed_and_never_opens_popup(self) -> None:
        record = self.record()
        self.store.save(record)
        jj = self.discovery_only_jj()
        env = {**self.env, "TMUX": "/tmp/tmux/default,1,0"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.JjAdapter", return_value=jj), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli._run_popup") as popup, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
                "--harness", "codex", "--goal", "Create by id",
            ], env=env)

        self.assertEqual((status, stderr.getvalue()), (0, ""))
        self.assertTrue(stdout.getvalue().startswith("Existing task (unchanged):\nTask: "))
        self.assertIn(f"Run: {record['runs'][0]['run_id']}", stdout.getvalue())
        popup.assert_not_called()

    def test_mismatched_parameters_refuse_without_mutation(self) -> None:
        path = self.store.save(self.record())
        before = path.read_bytes()
        jj = self.discovery_only_jj()

        status, stdout, stderr, _tmux = self.invoke([
            "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
            "--harness", "codex", "--goal", "Different label", "--json",
        ], jj)

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn(
            f"task {TASK_ID} is already registered with different parameters: label",
            stderr,
        )
        self.assertEqual(path.read_bytes(), before)
        jj.preflight.assert_not_called()
        jj.import_git.assert_not_called()

    def test_creating_record_requires_explicit_recovery(self) -> None:
        record = self.record()
        record["lifecycle"] = "creating"
        record["runs"] = []
        record["jj"]["change_id"] = None
        record["jj"]["working_commit_id"] = None
        path = self.store.save(record)
        before = path.read_bytes()

        status, stdout, stderr, _tmux = self.invoke([
            "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
            "--harness", "codex", "--goal", "Create by id", "--json",
        ], self.discovery_only_jj())

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn(
            f"task {TASK_ID} has an interrupted creation; run "
            f"`asha task recover {TASK_ID}` then retry",
            stderr,
        )
        self.assertEqual(path.read_bytes(), before)

    def test_journal_without_record_refuses_before_preflight(self) -> None:
        journals = mock.Mock()
        journals.read.return_value = {"task_id": TASK_ID, "phase": "intent"}
        jj = self.discovery_only_jj()
        with mock.patch("lib.control.cli.CreationJournalStore", return_value=journals):
            status, stdout, stderr, _tmux = self.invoke([
                "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
                "--harness", "codex", "--goal", "Create by id", "--json",
            ], jj)

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn(f"task {TASK_ID} has an interrupted creation", stderr)
        journals.read.assert_called_once_with(TASK_ID)
        jj.preflight.assert_not_called()
        jj.import_git.assert_not_called()

    def test_existing_pr_performs_no_fetch_or_import(self) -> None:
        pr_number = 17
        record = self.record(
            source_kind="pr", number=pr_number,
            requested_base=f"PR #{pr_number} head",
        )
        path = self.store.save(record)
        before = path.read_bytes()
        jj = self.discovery_only_jj()

        with mock.patch(
            "lib.control.cli.GithubAdapter",
            side_effect=AssertionError("existing PR task must not touch GitHub"),
        ) as github:
            status, stdout, stderr, _tmux = self.invoke([
                "task", "start", "--repo", str(self.source), "--task-id", TASK_ID,
                "--pr", str(pr_number), "--harness", "codex",
                "--goal", "Create by id", "--json",
            ], jj)

        self.assertEqual((status, stderr), (0, ""))
        self.assertTrue(json.loads(stdout)["existing"])
        self.assertEqual(path.read_bytes(), before)
        github.assert_not_called()
        jj.preflight.assert_not_called()
        jj.import_git.assert_not_called()


@unittest.skipUnless(shutil.which("jj"), "jj is required")
class RealJjCreateByIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "init", "-q", str(self.source)], check=True, env=git_env)
        (self.source / ".gitignore").write_text(
            "/.asha/\n/Memory/\n/Work/\n", encoding="utf-8",
        )
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.source), "add", ".gitignore", "tracked.txt"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "base"],
            check=True, env=git_env,
        )
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.source)],
            check=True, capture_output=True, text=True,
        )
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / ".asha" / "config.json").write_text(
            json.dumps({
                "initialized": True,
                "memory_version": 2,
                "project_id": "create-by-id-fixture",
            }) + "\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        self.env = {
            "HOME": str(self.home),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)

    def workspace_names(self) -> str:
        return subprocess.run(
            [
                "jj", "-R", str(self.source), "--ignore-working-copy",
                "workspace", "list", "-T", 'name ++ "\\n"',
            ],
            check=True, capture_output=True, text=True,
        ).stdout

    def invoke(self, adapter: FakeTmux) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.TmuxAdapter", return_value=adapter), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch(
                    "lib.control.launch.harness_api.process_identity",
                    return_value="proc:create-by-id",
                ), \
                mock.patch(
                    "lib.control.launch.harness_api.pane_ancestry_ok", return_value=True,
                ), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.source), "--base", "@-",
                "--task-id", TASK_ID, "--harness", "codex",
                "--goal", "Create by id", "--json",
            ], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_supplied_id_creates_once_and_second_call_returns_byte_equal_record(self) -> None:
        class CountingTmux(FakeTmux):
            def __init__(inner) -> None:
                super().__init__()
                inner.socket = None
                inner.create_calls = 0

            def create_task_session(inner, **kwargs):
                inner.create_calls += 1
                return super().create_task_session(**kwargs)

        adapter = CountingTmux()
        status, stdout, stderr = self.invoke(adapter)
        self.assertEqual(status, 0, stderr)
        created = json.loads(stdout)
        self.assertFalse(created["existing"])
        record_path = self.config.tasks_dir / f"{TASK_ID}.json"
        self.assertTrue(record_path.is_file())
        record = TaskStore(self.config).read(TASK_ID)
        self.assertEqual(record["slug"], "create-by-id")
        self.assertEqual(record["jj"]["workspace_name"], f"asha-create-by-id-{TASK_ID[:8]}")
        self.assertEqual(record["tmux"]["session"], f"asha-create-by-id-{TASK_ID[:8]}")
        before = record_path.read_bytes()
        workspaces_before = self.workspace_names()

        status, stdout, stderr = self.invoke(adapter)

        self.assertEqual(status, 0, stderr)
        existing = json.loads(stdout)
        self.assertTrue(existing["existing"])
        self.assertEqual(existing["source_mutations"], [])
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.workspace_names(), workspaces_before)
        self.assertEqual(adapter.create_calls, 1)
        self.assertEqual(len(TaskStore(self.config).read(TASK_ID)["runs"]), 1)


if __name__ == "__main__":
    unittest.main()
