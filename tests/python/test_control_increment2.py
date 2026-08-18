from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.context import ContextError, provision_context
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import DEFAULT_BASE_REVSET, JjAdapter, JjError, WorkspaceIdentity
from lib.control.launch import recover_task
from lib.control.prepare import derive_repository_identity
from lib.control.prepare import (
    PrepareRequest, PreparationError, plan_materialization, prepare_materialization,
    prepare_task_workspace, rollback_prelaunch,
)
from lib.control.store import StoreCommittedError, TaskStore, task_digest
from lib.control.transaction import CreationJournalStore, JournalError


class JjAdapterTests(unittest.TestCase):
    def test_visible_commit_refuses_all_zero_id_without_running_jj(self) -> None:
        runner = mock.Mock(side_effect=AssertionError("jj must not run"))
        adapter = JjAdapter(runner=runner)

        for length in (40, 64):
            with self.subTest(length=length), self.assertRaisesRegex(
                JjError, "empty root commit",
            ):
                adapter.require_visible_commit(Path("/repo"), "0" * length)
        runner.assert_not_called()

    def test_workspace_add_uses_full_pinned_operation_and_argv_only(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kwargs):
            calls.append(list(argv))
            if "operation" in argv:
                return subprocess.CompletedProcess(argv, 0, "a" * 128 + "\n", "")
            if "workspace" in argv and "add" in argv:
                return subprocess.CompletedProcess(argv, 0, "created\n", "")
            raise AssertionError(argv)

        adapter = JjAdapter(runner=runner)
        op = adapter.pin_operation(Path("/repo"))
        adapter.add_workspace(
            Path("/repo"), Path("/work/task"), "asha-task-11111111",
            "b" * 40, "Task label", op,
        )

        self.assertEqual(len(op), 128)
        add = calls[-1]
        self.assertEqual(add[:5], ["jj", "-R", "/repo", "--at-operation", op])
        self.assertNotIn("--ignore-working-copy", add)
        self.assertNotIn("@", add)
        self.assertIn("--revision", add)
        self.assertEqual(add[add.index("--revision") + 1], "b" * 40)

    def test_pin_operation_rejects_short_or_multiple_ids(self) -> None:
        for output in ("a" * 64 + "\n", "a" * 128 + "\n" + "b" * 128 + "\n"):
            with self.subTest(output=output):
                adapter = JjAdapter(runner=lambda argv, **kwargs:
                    subprocess.CompletedProcess(argv, 0, output, ""))
                with self.assertRaisesRegex(JjError, "operation ID"):
                    adapter.pin_operation(Path("/repo"))

    def test_subprocess_failure_is_bounded_and_does_not_use_a_shell(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess(
            ["jj"], 1, "", "x" * 20000,
        ))
        with self.assertRaises(JjError) as caught:
            JjAdapter(runner=run).pin_operation(Path("/repo"))
        self.assertLess(len(str(caught.exception)), 5000)
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_production_capture_stops_at_the_streaming_limit(self) -> None:
        adapter = JjAdapter()
        with self.assertRaisesRegex(JjError, "bounded"):
            adapter._run_bytes(
                sys.executable, ["-c", "import sys; sys.stdout.write('x' * 1000000)"],
                limit=1024,
            )


class ContextProvisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        (self.source / ".asha").mkdir(parents=True)
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        self.config_bytes = (json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "project-one",
        }, indent=2) + "\n").encode()
        (self.source / ".asha" / "config.json").write_bytes(self.config_bytes)
        self.active = b"# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        self.decisions = b"# Decisions\n\n- One.\n"
        (self.source / "Memory" / "activeContext.md").write_bytes(self.active)
        (self.source / "Memory" / "decisions.md").write_bytes(self.decisions)
        for path in (
            self.source / ".asha" / "config.json",
            self.source / "Memory" / "activeContext.md",
            self.source / "Memory" / "decisions.md",
        ):
            path.chmod(0o640)
        self.destination.mkdir()

    def marker(self) -> dict:
        return {
            "contract": "asha.control-task-context.v1",
            "task_id": "11111111-1111-4111-8111-111111111111",
            "repository": {"root": str(self.source), "identity": "repo:" + "a" * 64},
            "jj": {
                "workspace_name": "asha-task-11111111",
                "workspace_path": str(self.destination),
                "change_id": "k" * 32,
                "working_commit_id": "a" * 40,
            },
        }

    def test_provisions_only_bounded_copied_context_with_hashes_and_modes(self) -> None:
        before = {p: p.read_bytes() for p in (
            self.source / ".asha" / "config.json",
            self.source / "Memory" / "activeContext.md",
            self.source / "Memory" / "decisions.md",
        )}
        facts = provision_context(self.source, self.destination, self.marker())
        self.assertEqual((self.destination / ".asha" / "config.json").read_bytes(), self.config_bytes)
        self.assertEqual((self.destination / "Memory" / "activeContext.md").read_bytes(), self.active)
        self.assertEqual((self.destination / "Memory" / "decisions.md").read_bytes(), self.decisions)
        self.assertEqual(list((self.destination / "Work" / "session-state").iterdir()), [])
        self.assertFalse(any(path.is_symlink() for path in self.destination.rglob("*")))
        self.assertEqual(stat.S_IMODE((self.destination / "Memory" / "decisions.md").stat().st_mode), 0o640)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(set(facts), {
            ".asha/config.json", ".asha/control-task.json",
            "Memory/activeContext.md", "Memory/decisions.md",
        })
        self.assertTrue(all(set(item) == {"sha256", "mode"} for item in facts.values()))

    def test_tracked_context_paths_are_reused_never_overwritten(self) -> None:
        # A repository that commits .asha/workspace.json and Memory/*.md hands
        # Control a fresh workspace that already carries those entries.
        (self.destination / ".asha").mkdir(mode=0o775)
        (self.destination / ".asha" / "workspace.json").write_text("{}\n")
        (self.destination / "Memory").mkdir(mode=0o775)
        tracked_active = b"# Objective\n\ncommitted\n"
        (self.destination / "Memory" / "activeContext.md").write_bytes(tracked_active)
        created: list[str] = []
        provision_context(
            self.source, self.destination, self.marker(),
            after_entry=lambda relative, _fact: created.append(relative),
        )
        # Tracked entries untouched; only the missing private files created.
        self.assertEqual((self.destination / "Memory" / "activeContext.md").read_bytes(), tracked_active)
        self.assertEqual((self.destination / ".asha" / "workspace.json").read_text(), "{}\n")
        self.assertEqual(stat.S_IMODE((self.destination / ".asha").stat().st_mode), 0o775)
        self.assertTrue((self.destination / ".asha" / "control-task.json").exists())
        self.assertTrue((self.destination / ".asha" / "config.json").exists())
        self.assertTrue((self.destination / "Memory" / "decisions.md").exists())
        self.assertNotIn(".asha", created)
        self.assertNotIn("Memory", created)
        self.assertNotIn("Memory/activeContext.md", created)
        self.assertIn(".asha/control-task.json", created)
        self.assertIn(".asha/config.json", created)
        self.assertIn("Memory/decisions.md", created)

    def test_marker_symlink_and_non_directory_paths_still_collide(self) -> None:
        (self.destination / ".asha").mkdir(mode=0o700)
        (self.destination / ".asha" / "control-task.json").write_text("{}\n")
        with self.assertRaisesRegex(ContextError, "collision: .asha/control-task.json"):
            provision_context(self.source, self.destination, self.marker())
        (self.destination / ".asha" / "control-task.json").unlink()
        (self.destination / ".asha").rmdir()
        (self.destination / ".asha").write_text("not a directory\n")
        with self.assertRaisesRegex(ContextError, "collision: .asha"):
            provision_context(self.source, self.destination, self.marker())
        (self.destination / ".asha").unlink()
        (self.destination / "Memory").mkdir(mode=0o700)
        os.symlink("/etc/hostname", self.destination / "Memory" / "decisions.md")
        with self.assertRaisesRegex(ContextError, "collision: Memory/decisions.md"):
            provision_context(self.source, self.destination, self.marker())

    def test_pending_publication_recovery_fails_without_source_write(self) -> None:
        journal = self.source / "Work" / "session-state" / ".memory-publication-transaction.json"
        journal.write_text("{}", encoding="utf-8")
        before = sorted((str(p.relative_to(self.source)), p.read_bytes() if p.is_file() else b"")
                        for p in self.source.rglob("*"))
        with self.assertRaisesRegex(ContextError, "memory_v2.py recover --project-dir"):
            provision_context(self.source, self.destination, self.marker())
        after = sorted((str(p.relative_to(self.source)), p.read_bytes() if p.is_file() else b"")
                       for p in self.source.rglob("*"))
        self.assertEqual(before, after)

    def test_decisions_cap_is_enforced_before_destination_mutation(self) -> None:
        (self.source / "Memory" / "decisions.md").write_bytes(
            b"# Decisions\n\n" + b"x" * (64 * 1024)
        )
        with self.assertRaisesRegex(ContextError, "65536"):
            provision_context(self.source, self.destination, self.marker())
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_parent_swap_after_validation_never_writes_through_symlink(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        def swap(_plan) -> None:
            moved = self.root / "moved-destination"
            self.destination.rename(moved)
            self.destination.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ContextError):
            provision_context(
                self.source, self.destination, self.marker(), before_mutation=swap,
            )
        self.assertEqual(list(outside.iterdir()), [])


class RepositoryIdentityTests(unittest.TestCase):
    def test_identity_and_repo_key_are_deterministic_and_not_caller_prose(self) -> None:
        root = Path("/code/Project")
        first = derive_repository_identity("project-id", root, Path("/code/Project/.git"))
        second = derive_repository_identity("project-id", root, Path("/code/Project/.git"))
        changed = derive_repository_identity("project-id", root, Path("/git/other"))
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        identity, repo_key = first
        self.assertRegex(identity, r"^repo:[0-9a-f]{64}$")
        self.assertRegex(repo_key, r"^project-[0-9a-f]{16}$")


class JournalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        home = root / "home"
        home.mkdir()
        self.config = load_config({
            "HOME": str(home), "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        })
        self.store = CreationJournalStore(self.config)
        self.task_id = str(uuid.uuid4())

    def journal(self) -> dict:
        repository = Path(self.temp.name).resolve() / "repository"
        workspace = self.config.workspace_root / "repository-aaaaaaaaaaaaaaaa" / "task"
        return {
            "contract": "asha.control-creation-journal.v1",
            "task_id": self.task_id,
            "invocation_id": "c" * 32,
            "phase": "intent",
            "launch_attempted": False,
            "config": {
                "workspace_root": str(self.config.workspace_root),
                "tasks_dir": str(self.config.tasks_dir),
                "runtime_dir": str(self.config.runtime_dir),
            },
            "repository": {
                "root": str(repository), "identity": "repo:" + "a" * 64,
                "git_root": str(repository), "repo_key": "repository-aaaaaaaaaaaaaaaa",
            },
            "task": {
                "record_path": str(self.config.tasks_dir / f"{self.task_id}.json"),
                "slug": "task", "label": "Task", "digest": None, "failure": None,
            },
            "workspace": {"path": str(workspace), "name": "asha-task-11111111",
                          "root_fact": None, "created_parents": []},
            "jj": {"pinned_operation_id": "a" * 128, "base_commit_id": "b" * 40,
                   "change_id": None, "working_commit_id": None,
                   "description": "Task", "registration_state": "absent",
                   "last_registration": None},
            "expected_materialization": {},
            "materialized_owned": None,
            "recovery_owned": None,
            "planned_context": None,
            "context_owned": {},
            "removal": {"entries_removed": 0, "root_removed": False,
                        "parents_removed": 0},
        }

    def test_journal_is_atomic_bounded_private_and_uses_task_lock(self) -> None:
        first = self.journal()
        path = self.store.save(first)
        self.assertEqual(self.store.read(self.task_id), first)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        lock = self.config.runtime_dir / "tasks" / f"{self.task_id}.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        changed = json.loads(json.dumps(first))
        changed["phase"] = "task-recorded"
        self.store.save(changed, expected_phase="intent")
        self.assertEqual(self.store.read(self.task_id)["phase"], "task-recorded")

    def test_journal_rejects_symlink_and_oversize(self) -> None:
        journal = self.journal()
        self.store.save(journal)
        path = self.store.path(self.task_id)
        path.unlink()
        path.symlink_to(Path(self.temp.name) / "outside")
        with self.assertRaisesRegex(JournalError, "symlink"):
            self.store.read(self.task_id)
        path.unlink()
        journal["jj"]["description"] = "x" * (300 * 1024)
        with self.assertRaises(JournalError):
            self.store.save(journal)

    def test_journal_rejects_paths_not_bound_to_current_config_and_task(self) -> None:
        journal = self.journal()
        unrelated = Path(self.temp.name) / "unrelated"
        unrelated.mkdir(mode=0o700)
        journal["workspace"]["created_parents"] = [{
            "path": str(unrelated.resolve()), "parent_path": str(unrelated.parent.resolve()),
            "dev": unrelated.stat().st_dev, "ino": unrelated.stat().st_ino,
            "parent_dev": unrelated.parent.stat().st_dev,
            "parent_ino": unrelated.parent.stat().st_ino,
            "mode": 0o700, "uid": os.geteuid(),
        }]
        with self.assertRaisesRegex(JournalError, "workspace|config|ancestry|parent"):
            self.store.save(journal)
        self.assertTrue(unrelated.is_dir())

    def test_journal_rejects_config_task_and_created_parent_chain_rebinding(self) -> None:
        base = self.journal()
        cases = []
        changed = json.loads(json.dumps(base))
        changed["config"]["workspace_root"] = str(Path(self.temp.name).resolve() / "other")
        cases.append(changed)
        changed = json.loads(json.dumps(base))
        changed["task"]["record_path"] = str(Path(self.temp.name).resolve() / "foreign.json")
        cases.append(changed)
        changed = json.loads(json.dumps(base))
        parent = self.config.workspace_root
        child = parent / "repository-aaaaaaaaaaaaaaaa"
        changed["workspace"]["created_parents"] = [
            {"path": str(child), "parent_path": str(parent), "dev": 1, "ino": 2,
             "parent_dev": 1, "parent_ino": 1, "mode": 0o700, "uid": os.geteuid()},
            {"path": str(parent), "parent_path": str(parent.parent), "dev": 1, "ino": 1,
             "parent_dev": 1, "parent_ino": 3, "mode": 0o700, "uid": os.geteuid()},
        ]
        cases.append(changed)
        for journal in cases:
            with self.subTest(case=journal), self.assertRaises(JournalError):
                self.store.save(journal)

    def test_interrupted_journal_replace_keeps_a_valid_old_or_new_record(self) -> None:
        journal = self.journal()
        self.store.save(journal)
        changed = json.loads(json.dumps(journal))
        changed["phase"] = "task-recorded"
        real_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected fsync failure")
            return real_fsync(fd)

        with mock.patch("lib.control.transaction.os.fsync", side_effect=fail_directory_fsync):
            with self.assertRaises(JournalError):
                self.store.save(changed, expected_phase="intent")
        self.assertIn(self.store.read(self.task_id)["phase"], {"intent", "task-recorded"})

    def test_worst_case_journal_capacity_is_bounded_at_supported_entry_limit(self) -> None:
        from lib.control.prepare import (
            MAX_MATERIALIZATION_ENTRIES, _ensure_creation_journal_capacity,
        )
        from lib.control.transaction import MAX_JOURNAL_BYTES

        journal = self.journal()
        journal["expected_materialization"] = {
            f"entry-{index:04d}": {"type": "directory"}
            for index in range(MAX_MATERIALIZATION_ENTRIES)
        }
        size = _ensure_creation_journal_capacity(journal, {})
        self.assertLessEqual(size, MAX_JOURNAL_BYTES)

        over = json.loads(json.dumps(journal))
        over["expected_materialization"]["one-entry-over"] = {"type": "directory"}
        with self.assertRaisesRegex(PreparationError, "entry capacity"):
            _ensure_creation_journal_capacity(over, {})

    def test_worst_case_journal_rejects_serialized_overflow_before_mutation(self) -> None:
        from lib.control.prepare import _ensure_creation_journal_capacity
        from lib.control.transaction import MAX_JOURNAL_BYTES

        journal = self.journal()
        # Every component remains below NAME_MAX while the relative path grows
        # to the largest serialized journal accepted by the exact byte check.
        prefix = "/".join(["p" * 200] * 3000)
        low, high = 1, len(prefix)
        accepted = None
        while low <= high:
            middle = (low + high) // 2
            candidate = json.loads(json.dumps(journal))
            candidate["expected_materialization"] = {
                prefix[:middle].rstrip("/"): {"type": "directory"},
            }
            try:
                size = _ensure_creation_journal_capacity(candidate, {})
            except PreparationError:
                high = middle - 1
            else:
                accepted = (candidate, size)
                low = middle + 1
        self.assertIsNotNone(accepted)
        accepted_journal, accepted_size = accepted
        self.assertLessEqual(accepted_size, MAX_JOURNAL_BYTES)
        path = next(iter(accepted_journal["expected_materialization"]))
        overflow = json.loads(json.dumps(journal))
        overflow["expected_materialization"] = {path + "x": {"type": "directory"}}
        with self.assertRaisesRegex(PreparationError, "byte capacity"):
            _ensure_creation_journal_capacity(overflow, {})


@unittest.skipUnless(__import__("shutil").which("jj"), "jj is required")
class RealJjPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", str(self.source)], check=True, env=env)
        (self.source / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", "tracked.txt"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "base-no-ignore"], check=True, env=env)
        self.no_ignore_git_commit = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, env=env,
        ).stdout.strip()
        (self.source / ".gitignore").write_text("/.asha/\n/Memory/\n/Work/\n*.ignored\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.source), "add", ".gitignore"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "base"], check=True, env=env)
        subprocess.run(["jj", "git", "init", "--colocate", str(self.source)],
                       check=True, capture_output=True, text=True)
        (self.source / ".asha").mkdir()
        (self.source / "Memory").mkdir()
        (self.source / "Work" / "session-state").mkdir(parents=True)
        (self.source / ".asha" / "config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "project-one",
        }) + "\n", encoding="utf-8")
        (self.source / "Memory" / "activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n", encoding="utf-8")
        (self.source / "Memory" / "decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8")
        self.source.chmod(0o755)
        self.config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })

    def jj(self, *args: str) -> str:
        return subprocess.run(["jj", "-R", str(self.source), "--ignore-working-copy", *args],
                              check=True, capture_output=True, text=True).stdout

    def test_resolve_base_refuses_empty_root_and_ambiguous_revsets(self) -> None:
        adapter = JjAdapter()
        with self.assertRaisesRegex(JjError, "empty root commit"):
            adapter.resolve_base(self.source, "trunk()")
        with self.assertRaisesRegex(JjError, "ambiguous base commit ID"):
            adapter.resolve_base(self.source, "all()")

    def test_default_base_falls_back_to_the_local_main_bookmark(self) -> None:
        """A local-only colocated repository has no remote trunk(); the default
        base must resolve to its local main/master/trunk bookmark instead."""
        adapter = JjAdapter()
        branch = subprocess.run(
            ["git", "-C", str(self.source), "symbolic-ref", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if branch not in {"main", "master", "trunk"}:
            self.jj("bookmark", "create", "main", "-r", head)
        self.assertEqual(adapter.resolve_base(self.source, DEFAULT_BASE_REVSET), head)
        # With no such bookmark at all, the default names the remedy exactly.
        for name in ("main", "master", "trunk"):
            subprocess.run(
                ["jj", "-R", str(self.source), "--ignore-working-copy",
                 "bookmark", "delete", name],
                check=False, capture_output=True,
            )
        with self.assertRaisesRegex(JjError, "the default base resolved to the empty root commit"):
            adapter.resolve_base(self.source, DEFAULT_BASE_REVSET)

    def source_facts(self) -> dict:
        working_copy = {}
        for path in self.source.rglob("*"):
            relative = path.relative_to(self.source)
            if relative.parts[0] in {".git", ".jj"}:
                continue
            metadata = path.lstat()
            if path.is_symlink():
                value = ("symlink", stat.S_IMODE(metadata.st_mode), os.readlink(path))
            elif path.is_file():
                value = ("file", stat.S_IMODE(metadata.st_mode), path.read_bytes())
            else:
                value = ("directory", stat.S_IMODE(metadata.st_mode), None)
            working_copy[str(relative)] = value
        git_head = subprocess.run(["git", "-C", str(self.source), "rev-parse", "HEAD"],
                                  check=True, capture_output=True, text=True).stdout
        # Staged CONTENT, not raw index bytes: jj 0.38 rewrites the colocated
        # index file's layout (cache-tree extension) under --ignore-working-copy
        # while leaving every staged entry identical.  The invariant Control
        # promises is that nothing staged changes.
        git_index = subprocess.run(["git", "-C", str(self.source), "ls-files", "-s"],
                                   check=True, capture_output=True, text=True).stdout
        return {
            "working_copy": working_copy,
            "source_at": self.jj("log", "-r", "@", "--no-graph", "-T", 'change_id ++ " " ++ commit_id'),
            "bookmarks": self.jj("bookmark", "list", "-T", 'name ++ " " ++ normal_target.commit_id() ++ "\\n"'),
            "git_head": git_head,
            "git_index": git_index,
            "tracked": (self.source / "tracked.txt").read_bytes(),
        }

    @staticmethod
    def workspace_bytes(root: Path) -> dict[str, tuple]:
        result = {}
        for path in root.rglob("*"):
            relative = str(path.relative_to(root))
            metadata = path.lstat()
            if path.is_symlink():
                result[relative] = ("symlink", stat.S_IMODE(metadata.st_mode), os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", stat.S_IMODE(metadata.st_mode), path.read_bytes())
            else:
                result[relative] = ("directory", stat.S_IMODE(metadata.st_mode), None)
        return result

    def request(self, slug="task-one") -> PrepareRequest:
        return PrepareRequest(
            repository=self.source, requested_base="@-", task_id=str(uuid.uuid4()),
            slug=slug, label="Task one",
        )

    def create_workspace_root(self, mode: int) -> None:
        self.config.workspace_root.parent.mkdir(parents=True, mode=0o700)
        current = self.config.workspace_root.parent
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        self.config.workspace_root.mkdir(mode=mode)
        self.config.workspace_root.chmod(mode)

    def test_repository_that_tracks_context_paths_still_prepares(self) -> None:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        (self.source / ".gitignore").write_text(
            "/.asha/*\n!/.asha/workspace.json\n/Memory/*\n!/Memory/activeContext.md\n"
            "/Work/\n*.ignored\n", encoding="utf-8",
        )
        (self.source / ".asha" / "workspace.json").write_text('{"tracked": true}\n')
        committed_active = "# Objective\n\ncommitted\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        (self.source / "Memory" / "activeContext.md").write_text(committed_active)
        subprocess.run(
            ["git", "-C", str(self.source), "add", ".gitignore", ".asha/workspace.json",
             "Memory/activeContext.md"], check=True, env=env,
        )
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "track context"], check=True, env=env)
        subprocess.run(["jj", "-R", str(self.source), "status"], check=True, capture_output=True)
        # The live source Memory now differs from the committed copy.
        (self.source / "Memory" / "activeContext.md").write_text(
            committed_active.replace("committed", "uncommitted edit")
        )
        request = self.request("tracked-context")
        result = prepare_task_workspace(self.config, request)
        workspace = Path(result["jj"]["workspace_path"])
        self.assertEqual((workspace / ".asha" / "workspace.json").read_text(), '{"tracked": true}\n')
        self.assertEqual((workspace / "Memory" / "activeContext.md").read_text(), committed_active)
        self.assertTrue((workspace / ".asha" / "control-task.json").exists())
        self.assertTrue((workspace / ".asha" / "config.json").exists())
        self.assertTrue((workspace / "Memory" / "decisions.md").exists())
        status = subprocess.run(
            ["jj", "-R", str(workspace), "diff", "--summary"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(status.strip(), "", status)
        head_before = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        rollback_prelaunch(self.config, request.task_id)
        self.assertFalse(workspace.exists())
        # Rollback abandons Control's own empty described commit and leaves
        # the Git checkout alone.
        described = subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy", "log", "-r",
             result["jj"]["change_id"], "--no-graph", "-T", "change_id"],
            check=False, capture_output=True, text=True,
        )
        self.assertNotEqual(described.returncode, 0, described.stdout)
        head_after = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head_before, head_after)

    def test_refusals_lead_with_the_cause_and_name_the_remedy(self) -> None:
        # Preflight refusal: cause first, framing last, no task state.
        request = PrepareRequest(
            repository=self.source, requested_base=DEFAULT_BASE_REVSET,
            task_id=str(uuid.uuid4()), slug="cause-first", label="Cause first",
        )
        for name in ("main", "master", "trunk"):
            subprocess.run(
                ["jj", "-R", str(self.source), "--ignore-working-copy",
                 "bookmark", "delete", name], check=False, capture_output=True,
            )
        with self.assertRaises(PreparationError) as caught:
            prepare_task_workspace(self.config, request)
        message = str(caught.exception)
        self.assertTrue(
            message.startswith("the default base resolved to the empty root commit"), message,
        )
        self.assertIn("--base main", message)
        self.assertTrue(message.endswith("(preflight refused; no task state was created)"), message)
        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())
        # Namespace refusal after the intent was claimed: cause, remedy, then the
        # rollback outcome, and the record is gone.
        self.source.chmod(0o775)
        try:
            request = self.request("cause-second")
            with self.assertRaises(PreparationError) as caught:
                prepare_task_workspace(self.config, request)
        finally:
            self.source.chmod(0o755)
        message = str(caught.exception)
        self.assertTrue(message.startswith("writable non-sticky ancestor rejected in"), message)
        self.assertIn(f"remediate with: chmod g-w,o-w {self.source}", message)
        self.assertTrue(
            message.endswith("(workspace preparation rolled back; nothing to recover)")
            or message.endswith("(preflight refused; no task state was created)"),
            message,
        )
        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())

    def test_success_uses_exact_base_and_preserves_source(self) -> None:
        before = self.source_facts()
        result = prepare_task_workspace(self.config, self.request())
        after = self.source_facts()
        self.assertEqual(before, after)
        workspace = Path(result["jj"]["workspace_path"])
        self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
        identity = JjAdapter().inspect_workspace(workspace, result["jj"]["workspace_name"])
        self.assertEqual(identity.parent_commit_ids, (result["jj"]["base_commit_id"],))
        self.assertEqual(identity.description, "Task one")
        self.assertEqual(identity.change_id, result["jj"]["change_id"])
        self.assertEqual(identity.commit_id, result["jj"]["working_commit_id"])
        marker = json.loads((workspace / ".asha" / "control-task.json").read_text())
        self.assertEqual(marker["task_id"], result["task_id"])
        journal = CreationJournalStore(self.config).read(result["task_id"])
        self.assertEqual(journal["phase"], "ready-for-launch")
        self.assertFalse(journal["launch_attempted"])
        from lib.control.cli import main as control_main
        cli_env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(control_main(["task", "list", "--json"], env=cli_env), 0)
        listed = json.loads(stdout.getvalue())["tasks"]
        self.assertEqual(next(item for item in listed if item["task_id"] == result["task_id"])["status"], "creating")
        missing = workspace.with_name(workspace.name + "-missing")
        workspace.rename(missing)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(control_main(["task", "list", "--json"], env=cli_env), 0)
        listed = json.loads(stdout.getvalue())["tasks"]
        observed = next(item for item in listed if item["task_id"] == result["task_id"])
        self.assertEqual(observed["status"], "stale")
        self.assertIn("missing", observed["blocker"])

    def test_controller_materialization_is_exact_retained_and_runless(self) -> None:
        other = prepare_task_workspace(self.config, self.request("other-workspace"))
        before_source = self.source_facts()
        before_tasks = TaskStore(self.config).list()
        before_workspaces = JjAdapter().workspace_identities(self.source)
        base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        materialized = prepare_materialization(
            self.config, self.source, base, "verification-one",
        )

        path = Path(materialized["workspace_path"])
        identity = JjAdapter().inspect_workspace(
            path, materialized["workspace_name"], require_empty=True,
        )
        self.assertEqual(identity.parent_commit_ids, (base,))
        self.assertEqual(identity.change_id, materialized["change_id"])
        self.assertEqual(identity.commit_id, materialized["working_commit_id"])
        self.assertEqual(path.parent.name, "materializations")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertFalse((path / ".asha" / "control-task.json").exists())
        self.assertEqual(before_source, self.source_facts())
        self.assertEqual(before_tasks, TaskStore(self.config).list())
        after_workspaces = JjAdapter().workspace_identities(self.source)
        self.assertEqual(
            after_workspaces[other["jj"]["workspace_name"]],
            before_workspaces[other["jj"]["workspace_name"]],
        )
        self.assertIn(materialized["workspace_name"], after_workspaces)
        self.assertTrue(path.is_dir(), "controller materialization must be retained")

    def test_controller_materialization_planner_is_deterministic_and_read_only(self) -> None:
        before_source = self.source_facts()
        before_tasks = TaskStore(self.config).list()
        before_workspaces = JjAdapter().workspace_identities(self.source)

        first = plan_materialization(
            self.config, self.source, "planned-verification",
        )
        second = plan_materialization(
            self.config, self.source, "planned-verification",
        )

        self.assertEqual(first, second)
        self.assertEqual(
            Path(first["workspace_path"]).parent.name, "materializations",
        )
        self.assertFalse(Path(first["workspace_path"]).exists())
        self.assertEqual(before_source, self.source_facts())
        self.assertEqual(before_tasks, TaskStore(self.config).list())
        self.assertEqual(
            before_workspaces, JjAdapter().workspace_identities(self.source),
        )

    def test_controller_materialization_partial_add_is_retained_private(self) -> None:
        destinations: list[Path] = []

        class PartialAddAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                destinations.append(destination)
                destination.chmod(0o755)
                raise JjError("injected after materialization add")

        base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        with self.assertRaises(JjError):
            prepare_materialization(
                self.config, self.source, base, "partial-verification",
                jj=PartialAddAdapter(),
            )
        self.assertEqual(len(destinations), 1)
        self.assertTrue(destinations[0].is_dir())
        self.assertEqual(stat.S_IMODE(destinations[0].stat().st_mode), 0o700)

    def test_existing_unmarked_materializations_path_is_preserved_and_refused(self) -> None:
        task = prepare_task_workspace(self.config, self.request("namespace-probe"))
        container = Path(task["jj"]["workspace_path"]).parent / "materializations"
        container.mkdir(mode=0o700)
        (container / "legacy-task-bytes").write_bytes(b"preserve exactly\n")
        before = self.workspace_bytes(container)
        base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        with self.assertRaisesRegex(PreparationError, "authenticated controller namespace"):
            prepare_materialization(
                self.config, self.source, base, "collision-one",
            )

        self.assertEqual(self.workspace_bytes(container), before)

    def test_materialization_namespace_reserves_same_named_task_slug(self) -> None:
        base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        materialized = prepare_materialization(
            self.config, self.source, base, "namespace-first",
        )
        container = Path(materialized["workspace_path"]).parent
        before = self.workspace_bytes(container)

        with self.assertRaisesRegex(PreparationError, "reserved"):
            prepare_task_workspace(self.config, self.request("materializations"))

        self.assertEqual(self.workspace_bytes(container), before)

    def test_cli_creation_reconciliation_detects_live_change_drift_read_only(self) -> None:
        request = self.request("cli-live-drift")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        subprocess.run(
            ["jj", "-R", str(workspace), "describe", "-m", "foreign description"],
            check=True, capture_output=True, text=True,
        )
        before_source = self.source_facts()
        before_workspace = self.workspace_bytes(workspace)
        from lib.control.cli import main as control_main
        cli_env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(control_main(["task", "show", request.task_id, "--json"], env=cli_env), 0)
        result = json.loads(stdout.getvalue())["reconciliation"]
        self.assertEqual(result["state"], "stale")
        self.assertTrue(any(
            identity in result["blocker"] for identity in ("registration", "commit", "description")
        ))
        self.assertEqual(before_source, self.source_facts())
        self.assertEqual(before_workspace, self.workspace_bytes(workspace))

    def test_cli_creation_reconciliation_detects_missing_live_registration_read_only(self) -> None:
        request = self.request("cli-missing-registration")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        JjAdapter().forget_workspace(self.source, prepared["jj"]["workspace_name"])
        before_source = self.source_facts()
        before_workspace = self.workspace_bytes(workspace)
        from lib.control.cli import main as control_main
        cli_env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(control_main(["task", "reconcile", request.task_id, "--json"], env=cli_env), 0)
        result = json.loads(stdout.getvalue())["results"][0]
        self.assertEqual(result["state"], "stale")
        self.assertIn("registration", result["blocker"])
        self.assertEqual(before_source, self.source_facts())
        self.assertEqual(before_workspace, self.workspace_bytes(workspace))

    def test_cli_creation_reconciliation_reports_unavailable_jj_as_stale(self) -> None:
        request = self.request("cli-jj-unavailable")
        prepare_task_workspace(self.config, request)
        from lib.control.cli import main as control_main
        cli_env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        stdout = io.StringIO()
        with mock.patch(
            "lib.control.cli.JjAdapter.preflight",
            side_effect=JjError("command invocation failed: injected unavailable jj"),
        ), contextlib.redirect_stdout(stdout):
            self.assertEqual(control_main(["task", "list", "--json"], env=cli_env), 0)
        result = json.loads(stdout.getvalue())["tasks"][0]
        self.assertEqual(result["status"], "stale")
        self.assertIn("unavailable", result["blocker"])

    def test_failure_after_owned_manifest_rolls_back_only_owned_workspace(self) -> None:
        request = self.request("rollback")
        before = self.source_facts()
        with self.assertRaises(PreparationError):
            prepare_task_workspace(
                self.config, request,
                failure_injector=lambda phase: (_ for _ in ()).throw(RuntimeError("injected"))
                if phase == "workspace-recorded" else None,
            )
        self.assertEqual(before, self.source_facts())
        task = __import__("lib.control.store", fromlist=["TaskStore"]).TaskStore(self.config).read(request.task_id)
        self.assertEqual(task["lifecycle"], "failed")
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertFalse(Path(journal["workspace"]["path"]).exists())
        self.assertNotIn(journal["workspace"]["name"], JjAdapter().workspace_identities(self.source))

    def test_failure_after_add_before_ownership_capture_recovers_expected_materialization(self) -> None:
        request = self.request("ambiguous")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(
                self.config, request,
                failure_injector=lambda phase: (_ for _ in ()).throw(RuntimeError("injected"))
                if phase == "workspace-added" else None,
            )
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertFalse(Path(journal["workspace"]["path"]).exists())

    def test_keyboard_interrupt_after_workspace_add_rolls_back_and_propagates(self) -> None:
        request = self.request("keyboard-interrupt")

        def interrupt(phase: str) -> None:
            if phase == "workspace-added":
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            prepare_task_workspace(
                self.config, request, failure_injector=interrupt,
            )
        task = TaskStore(self.config).read(request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(task["lifecycle"], "failed")
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertNotIn(
            journal["workspace"]["name"],
            JjAdapter().workspace_identities(self.source),
        )

    def test_recover_ready_for_launch_rolls_back_and_doctor_clears_transaction(self) -> None:
        request = self.request("recover-ready")
        prepared = prepare_task_workspace(self.config, request)
        before = run_doctor(
            self.config, probes={"transactions": DEFAULT_PROBES["transactions"]},
        )["probes"][0]
        self.assertEqual(before["outcome"], "mismatch")
        self.assertIn(request.task_id, before["detail"])

        result = recover_task(
            self.config, prepared, tasks=TaskStore(self.config),
            journals=CreationJournalStore(self.config), jj=JjAdapter(),
        )

        self.assertEqual(result["message"], "rolled back")
        self.assertEqual(result["task"]["lifecycle"], "failed")
        self.assertEqual(result["journal"]["phase"], "rolled-back")
        after = run_doctor(
            self.config, probes={"transactions": DEFAULT_PROBES["transactions"]},
        )["probes"][0]
        self.assertEqual(after["outcome"], "match")

    def test_rollback_forgets_from_source_when_destination_jj_disappears(self) -> None:
        request = self.request("missing-destination-jj")
        prepared = prepare_task_workspace(self.config, request)
        destination = Path(prepared["jj"]["workspace_path"])

        def remove_destination_jj(phase: str) -> None:
            if phase == "before-forget":
                shutil.rmtree(destination / ".jj")

        with self.assertRaises(PreparationError):
            rollback_prelaunch(
                self.config, request.task_id, failure_injector=remove_destination_jj,
            )
        self.assertNotIn(
            prepared["jj"]["workspace_name"],
            JjAdapter().workspace_identities(self.source),
        )

    def test_foreign_ignored_file_blocks_prelaunch_rollback(self) -> None:
        request = self.request("foreign")
        result = prepare_task_workspace(self.config, request)
        workspace = Path(result["jj"]["workspace_path"])
        (workspace / "foreign.ignored").write_text("user data", encoding="utf-8")
        from lib.control.prepare import rollback_prelaunch
        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id)
        self.assertTrue((workspace / "foreign.ignored").exists())
        self.assertIn(result["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source))

    def test_same_change_different_working_commit_preserves_before_forget_or_removal(self) -> None:
        request = self.request("commit-evolved")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        evolved_commit = "f" * len(prepared["jj"]["working_commit_id"])

        class EvolvedMetadataAdapter(JjAdapter):
            forget_called = False

            def workspace_identities(inner_self, repository):
                identities = super().workspace_identities(repository)
                identities[prepared["jj"]["workspace_name"]] = (
                    prepared["jj"]["change_id"], evolved_commit,
                )
                return identities

            def inspect_workspace(inner_self, destination, expected_name, **kwargs):
                return WorkspaceIdentity(
                    expected_name, prepared["jj"]["change_id"], evolved_commit,
                    (prepared["jj"]["base_commit_id"],), "Task one",
                )

            def forget_workspace(inner_self, source, name):
                inner_self.forget_called = True
                raise AssertionError("forget must not be reached for commit drift")

        adapter = EvolvedMetadataAdapter()
        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id, jj=adapter)

        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertFalse(adapter.forget_called)
        self.assertTrue(workspace.is_dir())
        self.assertIn(
            prepared["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source),
        )

    def test_launch_attempted_blocks_rollback(self) -> None:
        request = self.request("launched")
        prepare_task_workspace(self.config, request)
        store = CreationJournalStore(self.config)
        journal = store.read(request.task_id)
        journal["phase"] = "tmux-intent"
        store.save(journal, expected_phase="ready-for-launch")
        journal["phase"] = "tmux-session-created"
        store.save(journal, expected_phase="tmux-intent")
        journal["launch_attempted"] = True
        journal["phase"] = "launch-attempted"
        store.save(journal, expected_phase="tmux-session-created")
        from lib.control.prepare import rollback_prelaunch
        with self.assertRaisesRegex(PreparationError, "launch"):
            rollback_prelaunch(self.config, request.task_id)

    def test_each_injected_boundary_preserves_all_source_planes(self) -> None:
        phases = (
            "task-recorded", "parent-intent", "parent-ready",
            "workspace-add-intent", "workspace-added", "workspace-recorded",
            "context-intent", "context-file:.asha/config.json",
            "context-file:.asha/control-task.json",
            "context-file:Memory/activeContext.md",
            "context-file:Memory/decisions.md", "context-provisioned",
            "task-identity-intent", "task-identity-recorded", "ready-for-launch",
        )
        for index, injected_phase in enumerate(phases):
            with self.subTest(phase=injected_phase):
                before = self.source_facts()
                request = self.request(f"boundary-{index}")

                def inject(observed, expected=injected_phase):
                    if observed == expected:
                        raise RuntimeError("injected boundary failure")

                with self.assertRaises(PreparationError):
                    prepare_task_workspace(
                        self.config, request, failure_injector=inject,
                    )
                self.assertEqual(before, self.source_facts())

    def test_every_prelaunch_durable_phase_rolls_back_to_terminal_recovery_facts(self) -> None:
        phases = (
            "intent", "task-recorded", "parent-intent", "parent-ready",
            "workspace-add-intent", "workspace-added", "workspace-recorded",
            "context-intent", "context-provisioning", "context-provisioned",
            "task-identity-intent", "task-identity-recorded", "ready-for-launch",
        )
        for index, durable_phase in enumerate(phases):
            with self.subTest(phase=durable_phase):
                request = self.request(f"early-recovery-{index}")

                def inject(observed, expected=f"journal:{durable_phase}"):
                    if observed == expected:
                        raise RuntimeError("injected early restart boundary")

                with self.assertRaises(PreparationError):
                    prepare_task_workspace(self.config, request, failure_injector=inject)

                journal = CreationJournalStore(self.config).read(request.task_id)
                self.assertEqual(journal["phase"], "rolled-back")
                self.assertIn(
                    journal["jj"]["registration_state"], {"absent", "absent-after-forget"},
                )
                self.assertTrue(journal["removal"]["root_removed"])
                self.assertFalse(Path(journal["workspace"]["path"]).exists())
                self.assertNotIn(
                    journal["workspace"]["name"], JjAdapter().workspace_identities(self.source),
                )
                if durable_phase == "intent":
                    with self.assertRaisesRegex(Exception, "not found"):
                        TaskStore(self.config).read(request.task_id)
                else:
                    self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")

    def test_indeterminate_identity_task_replace_reconciles_exact_planned_record(self) -> None:
        request = self.request("identity-replace")
        original = TaskStore.save
        injected = False

        def save_then_report_indeterminate(store, task, *, expected_digest=None):
            nonlocal injected
            result = original(store, task, expected_digest=expected_digest)
            if not injected and task["jj"]["change_id"] is not None:
                injected = True
                raise StoreCommittedError("injected after exact task replacement")
            return result

        with mock.patch.object(TaskStore, "save", new=save_then_report_indeterminate):
            with self.assertRaises(PreparationError):
                prepare_task_workspace(self.config, request)

        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertEqual(journal["jj"]["registration_state"], "absent-after-forget")
        self.assertFalse(Path(journal["workspace"]["path"]).exists())
        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")

    def test_indeterminate_failed_task_replace_resumes_rollback_after_restart(self) -> None:
        request = self.request("failed-replace")
        prepared = prepare_task_workspace(self.config, request)
        original = TaskStore.save
        injected = False

        def save_then_report_indeterminate(store, task, *, expected_digest=None):
            nonlocal injected
            result = original(store, task, expected_digest=expected_digest)
            if not injected and task["lifecycle"] == "failed":
                injected = True
                raise StoreCommittedError("injected after failed task replacement")
            return result

        with mock.patch.object(TaskStore, "save", new=save_then_report_indeterminate):
            with self.assertRaises(PreparationError):
                rollback_prelaunch(self.config, request.task_id)

        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")
        rollback_prelaunch(self.config, request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        task = TaskStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertEqual(journal["task"]["digest"], task_digest(task))
        self.assertFalse(Path(prepared["jj"]["workspace_path"]).exists())

    def test_indeterminate_preserved_task_replace_reconciles_exact_failure_on_restart(self) -> None:
        request = self.request("preserved-failed-replace")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        (workspace / "foreign.ignored").write_text("user bytes", encoding="utf-8")
        original = TaskStore.save
        injected = False

        def save_then_report_indeterminate(store, task, *, expected_digest=None):
            nonlocal injected
            result = original(store, task, expected_digest=expected_digest)
            if not injected and task["lifecycle"] == "failed":
                injected = True
                raise StoreCommittedError("injected preserved task replacement")
            return result

        with mock.patch.object(TaskStore, "save", new=save_then_report_indeterminate):
            with self.assertRaises(PreparationError):
                rollback_prelaunch(self.config, request.task_id)

        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        task = TaskStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["task"]["digest"], task_digest(task))
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")

    def test_restart_after_preserved_phase_precedes_task_failure_completes_exact_task(self) -> None:
        request = self.request("preserved-phase-crash")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        (workspace / "foreign.ignored").write_text("user bytes", encoding="utf-8")
        original = CreationJournalStore.save
        injected = False

        class SimulatedProcessDeath(BaseException):
            pass

        def save_preserved_then_die(store, journal, *, expected_phase=None):
            nonlocal injected
            result = original(store, journal, expected_phase=expected_phase)
            if not injected and journal["phase"] == "preserved":
                injected = True
                raise SimulatedProcessDeath("died after preserved journal replacement")
            return result

        with mock.patch.object(CreationJournalStore, "save", new=save_preserved_then_die):
            with self.assertRaises(SimulatedProcessDeath):
                rollback_prelaunch(self.config, request.task_id)

        self.assertEqual(CreationJournalStore(self.config).read(request.task_id)["phase"], "preserved")
        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "creating")
        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id)

        journal = CreationJournalStore(self.config).read(request.task_id)
        task = TaskStore(self.config).read(request.task_id)
        self.assertEqual(task["lifecycle"], "failed")
        self.assertEqual(journal["task"]["digest"], task_digest(task))
        self.assertIsNotNone(journal["task"]["failure"])
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")
        self.assertIn(prepared["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source))

    def test_restart_after_preserved_phase_does_not_touch_conflicting_task_bytes(self) -> None:
        request = self.request("preserved-phase-conflict")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        (workspace / "foreign.ignored").write_text("user bytes", encoding="utf-8")
        original = CreationJournalStore.save
        injected = False

        class SimulatedProcessDeath(BaseException):
            pass

        def save_preserved_then_die(store, journal, *, expected_phase=None):
            nonlocal injected
            result = original(store, journal, expected_phase=expected_phase)
            if not injected and journal["phase"] == "preserved":
                injected = True
                raise SimulatedProcessDeath("died after preserved journal replacement")
            return result

        with mock.patch.object(CreationJournalStore, "save", new=save_preserved_then_die):
            with self.assertRaises(SimulatedProcessDeath):
                rollback_prelaunch(self.config, request.task_id)

        tasks = TaskStore(self.config)
        current = tasks.read(request.task_id)
        conflicting = copy.deepcopy(current)
        conflicting["updated_at"] = "9999-12-31T23:59:59.999999Z"
        tasks.save(conflicting, expected_digest=task_digest(current))
        conflicting_digest = task_digest(conflicting)

        with self.assertRaisesRegex(PreparationError, "changed outside"):
            rollback_prelaunch(self.config, request.task_id)

        self.assertEqual(task_digest(tasks.read(request.task_id)), conflicting_digest)
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertIsNone(journal["task"]["failure"])
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")
        self.assertIn(prepared["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source))

    def test_foreign_ignored_file_created_before_add_returns_is_never_adopted(self) -> None:
        class InjectingAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                (destination / "foreign.ignored").write_text("foreign", encoding="utf-8")

        request = self.request("post-add-foreign")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=InjectingAdapter())
        journal = CreationJournalStore(self.config).read(request.task_id)
        workspace = Path(journal["workspace"]["path"])
        self.assertEqual((workspace / "foreign.ignored").read_text(), "foreign")
        self.assertEqual(journal["phase"], "preserved")

    def test_retry_collision_cannot_rollback_prior_ready_transaction(self) -> None:
        request = self.request("retry-collision")
        first = prepare_task_workspace(self.config, request)
        workspace = Path(first["jj"]["workspace_path"])
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(
            CreationJournalStore(self.config).read(request.task_id)["phase"],
            "ready-for-launch",
        )
        self.assertIn(first["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source))

    def test_workspace_root_below_source_is_rejected_before_mutation(self) -> None:
        nested = self.source / "controller-workspaces"
        config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "nested-config.json"),
            "XDG_STATE_HOME": str(self.root / "nested-state"),
            "XDG_DATA_HOME": str(self.root / "nested-data"),
            "XDG_RUNTIME_DIR": str(self.root / "nested-runtime"),
        })
        object.__setattr__(config, "workspace_root", nested)
        before = self.source_facts()
        with self.assertRaisesRegex(PreparationError, "workspace_root"):
            prepare_task_workspace(config, self.request("nested-root"))
        self.assertEqual(before, self.source_facts())
        self.assertFalse(nested.exists())

    def test_eight_missing_destination_ancestors_are_preflighted_and_supported(self) -> None:
        anchor = self.root / "eight-parent-anchor"
        anchor.mkdir(mode=0o700)
        workspace_root = anchor.joinpath(*(f"level-{index}" for index in range(7)))
        config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "eight-config.json"),
            "XDG_STATE_HOME": str(self.root / "eight-state"),
            "XDG_DATA_HOME": str(self.root / "eight-data"),
            "XDG_RUNTIME_DIR": str(self.root / "eight-runtime"),
        })
        object.__setattr__(config, "workspace_root", workspace_root)
        request = self.request("eight-parents")
        before = self.source_facts()

        prepared = prepare_task_workspace(config, request)

        journal = CreationJournalStore(config).read(request.task_id)
        self.assertEqual(len(journal["workspace"]["created_parents"]), 8)
        self.assertEqual(before, self.source_facts())
        rollback_prelaunch(config, request.task_id)
        self.assertEqual(list(anchor.iterdir()), [])
        self.assertFalse(Path(prepared["jj"]["workspace_path"]).exists())

    def test_nine_missing_destination_ancestors_reject_before_all_mutation(self) -> None:
        anchor = self.root / "nine-parent-anchor"
        anchor.mkdir(mode=0o700)
        workspace_root = anchor.joinpath(*(f"level-{index}" for index in range(8)))
        config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "nine-config.json"),
            "XDG_STATE_HOME": str(self.root / "nine-state"),
            "XDG_DATA_HOME": str(self.root / "nine-data"),
            "XDG_RUNTIME_DIR": str(self.root / "nine-runtime"),
        })
        object.__setattr__(config, "workspace_root", workspace_root)
        request = self.request("nine-parents")
        before_source = self.source_facts()
        before_registration = JjAdapter().workspace_identities(self.source)
        before_anchor = self.workspace_bytes(anchor)

        with self.assertRaisesRegex(PreparationError, "eight|8|ancestor"):
            prepare_task_workspace(config, request)

        self.assertEqual(before_source, self.source_facts())
        self.assertEqual(before_registration, JjAdapter().workspace_identities(self.source))
        self.assertEqual(before_anchor, self.workspace_bytes(anchor))
        self.assertFalse((config.tasks_dir / f"{request.task_id}.json").exists())
        self.assertFalse(CreationJournalStore(config).path(request.task_id).exists())

    def test_preexisting_workspace_root_must_have_mode_0700(self) -> None:
        self.create_workspace_root(0o755)
        request = self.request("public-workspace-root")

        with self.assertRaisesRegex(PreparationError, "mode 0700"):
            prepare_task_workspace(self.config, request)

        self.assertEqual(stat.S_IMODE(self.config.workspace_root.stat().st_mode), 0o755)
        self.assertEqual(list(self.config.workspace_root.iterdir()), [])

    def test_preexisting_repository_parent_must_have_mode_0700(self) -> None:
        self.create_workspace_root(0o700)
        repository = JjAdapter().preflight(self.source)
        _, repo_key = derive_repository_identity(
            "project-one", repository.root, repository.git_root,
        )
        repository_parent = self.config.workspace_root / repo_key
        repository_parent.mkdir(mode=0o755)
        repository_parent.chmod(0o755)
        request = self.request("public-repository-parent")

        with self.assertRaisesRegex(PreparationError, "mode 0700"):
            prepare_task_workspace(self.config, request)

        self.assertEqual(stat.S_IMODE(repository_parent.stat().st_mode), 0o755)
        self.assertEqual(list(repository_parent.iterdir()), [])

    def test_preexisting_repository_parent_must_be_euid_owned(self) -> None:
        self.create_workspace_root(0o700)
        repository = JjAdapter().preflight(self.source)
        _, repo_key = derive_repository_identity(
            "project-one", repository.root, repository.git_root,
        )
        repository_parent = self.config.workspace_root / repo_key
        repository_parent.mkdir(mode=0o700)
        repository_inode = (repository_parent.stat().st_dev, repository_parent.stat().st_ino)
        real_fstat = os.fstat

        def foreign_owner(fd):
            metadata = real_fstat(fd)
            if (metadata.st_dev, metadata.st_ino) != repository_inode:
                return metadata
            fields = list(metadata)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)

        request = self.request("foreign-repository-parent")
        with mock.patch("lib.control.prepare.os.fstat", side_effect=foreign_owner):
            with self.assertRaisesRegex(PreparationError, "effective user"):
                prepare_task_workspace(self.config, request)

        self.assertEqual(list(repository_parent.iterdir()), [])

    def test_base_without_private_ignore_rules_fails_and_leaves_no_registered_workspace(self) -> None:
        before = self.source_facts()
        request = PrepareRequest(
            repository=self.source, requested_base=self.no_ignore_git_commit,
            task_id=str(uuid.uuid4()), slug="base-no-ignore", label="Base no ignore",
        )
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request)
        self.assertEqual(before, self.source_facts())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertNotEqual(journal["phase"], "ready-for-launch")
        self.assertNotIn(journal["workspace"]["name"], JjAdapter().workspace_identities(self.source))

    def test_thousand_file_revision_is_capacity_checked_before_mutation_and_prepares(self) -> None:
        bulk = self.source / "bulk"
        bulk.mkdir()
        for index in range(1000):
            (bulk / f"controller-capacity-boundary-file-{index:04d}.txt").write_text(
                f"{index}\n", encoding="utf-8",
            )
        env = {
            **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "-C", str(self.source), "add", "bulk"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "thousand files"],
            check=True, env=env,
        )
        base = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["jj", "-R", str(self.source), "--ignore-working-copy", "git", "import"],
            check=True, capture_output=True, text=True,
        )
        request = PrepareRequest(
            repository=self.source, requested_base=base, task_id=str(uuid.uuid4()),
            slug="thousand-files", label="Thousand files",
        )
        expected = JjAdapter().expected_materialization(self.source, base)
        self.assertGreaterEqual(len(expected), 1002)
        before = self.source_facts()

        prepared = prepare_task_workspace(self.config, request)

        self.assertEqual(before, self.source_facts())
        journal_path = CreationJournalStore(self.config).path(request.task_id)
        self.assertLessEqual(
            journal_path.stat().st_size,
            __import__("lib.control.transaction", fromlist=["MAX_JOURNAL_BYTES"]).MAX_JOURNAL_BYTES,
        )
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).is_dir())

    def test_unsupported_materialization_is_rejected_before_any_creation_mutation(self) -> None:
        class OversizedAdapter(JjAdapter):
            add_called = False

            def expected_materialization(inner_self, git_root, base):
                return {
                    f"entry-{index:04d}": {"type": "directory"}
                    for index in range(1025)
                }

            def add_workspace(inner_self, *args):
                inner_self.add_called = True
                return super().add_workspace(*args)

        adapter = OversizedAdapter()
        request = self.request("unsupported-capacity")

        with self.assertRaisesRegex(PreparationError, "entry capacity"):
            prepare_task_workspace(self.config, request, jj=adapter)

        self.assertFalse(adapter.add_called)
        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(request.task_id).exists())
        self.assertFalse(self.config.workspace_root.exists())

    def test_serialized_capacity_overflow_is_rejected_before_any_creation_mutation(self) -> None:
        class OversizedAdapter(JjAdapter):
            add_called = False

            def expected_materialization(inner_self, git_root, base):
                return {"x" * (260 * 1024): {"type": "directory"}}

            def add_workspace(inner_self, *args):
                inner_self.add_called = True
                return super().add_workspace(*args)

        adapter = OversizedAdapter()
        request = self.request("unsupported-bytes")

        with self.assertRaisesRegex(PreparationError, "byte capacity"):
            prepare_task_workspace(self.config, request, jj=adapter)

        self.assertFalse(adapter.add_called)
        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(request.task_id).exists())
        self.assertFalse(self.config.workspace_root.exists())

    def test_partial_add_exception_is_recovered_from_expected_materialization(self) -> None:
        class PartialAddAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                raise JjError("injected after add")

        request = self.request("partial-add")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=PartialAddAdapter())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertFalse(Path(journal["workspace"]["path"]).exists())
        self.assertNotIn(journal["workspace"]["name"], JjAdapter().workspace_identities(self.source))

    def test_unregistered_add_collision_is_preserved_without_chmod(self) -> None:
        class CollidingAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                destination.mkdir(mode=0o755)
                destination.chmod(0o755)
                (destination / "foreign.ignored").write_text("foreign", encoding="utf-8")
                raise JjError("injected unregistered collision")

        request = self.request("unregistered-collision")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=CollidingAdapter())

        journal = CreationJournalStore(self.config).read(request.task_id)
        destination = Path(journal["workspace"]["path"])
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
        self.assertEqual((destination / "foreign.ignored").read_text(), "foreign")

    def test_partial_add_with_missing_destination_retains_exact_registration_recovery(self) -> None:
        moved_paths: list[Path] = []

        class MissingDestinationAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                moved = destination.with_name(destination.name + "-displaced")
                destination.rename(moved)
                moved_paths.append(moved)
                raise JjError("injected missing destination after registration")

        request = self.request("partial-missing")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=MissingDestinationAdapter())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "workspace-add-intent")
        self.assertEqual(journal["jj"]["registration_state"], "present")
        self.assertIsNotNone(journal["jj"]["last_registration"])
        self.assertEqual(
            __import__("lib.control.store", fromlist=["TaskStore"]).TaskStore(self.config)
            .read(request.task_id)["lifecycle"],
            "failed",
        )
        self.assertTrue(moved_paths[0].is_dir())

    def test_rollback_resumes_after_forget_returned_but_journal_did_not_advance(self) -> None:
        request = self.request("forget-resume")
        prepared = prepare_task_workspace(self.config, request)

        class ForgetThenFail(JjAdapter):
            failed = False

            def forget_workspace(inner_self, source, name):
                super().forget_workspace(source, name)
                if not inner_self.failed:
                    inner_self.failed = True
                    raise JjError("interrupted after forget")

        with self.assertRaises(PreparationError):
            rollback_prelaunch(self.config, request.task_id, jj=ForgetThenFail())
        rollback_prelaunch(self.config, request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertFalse(Path(prepared["jj"]["workspace_path"]).exists())

    def test_transaction_lock_serializes_prepare_and_rollback(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class PausingAdapter(JjAdapter):
            def add_workspace(inner_self, *args):
                entered.set()
                self.assertTrue(release.wait(10))
                return super().add_workspace(*args)

        request = self.request("serialized")
        prepare_error: list[BaseException] = []
        rollback_error: list[BaseException] = []
        prepare_thread = threading.Thread(target=lambda: self._capture_error(
            prepare_error, prepare_task_workspace, self.config, request, jj=PausingAdapter()
        ))
        prepare_thread.start()
        self.assertTrue(entered.wait(10))
        other_runtime_config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "other-runtime"),
        })
        rollback_thread = threading.Thread(target=lambda: self._capture_error(
            rollback_error, rollback_prelaunch, other_runtime_config, request.task_id
        ))
        rollback_thread.start()
        time.sleep(0.2)
        self.assertTrue(rollback_thread.is_alive(), "rollback did not wait for preparation lock")
        registry_probe = subprocess.run(
            [
                sys.executable, "-c",
                (
                    "import fcntl,os,sys; "
                    "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
                    "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                    "\nexcept BlockingIOError: print('contended')"
                    "\nelse: print('acquired')"
                ),
                str(self.config.tasks_dir),
            ],
            capture_output=True, text=True, timeout=5, check=True,
        )
        self.assertEqual(
            registry_probe.stdout.strip(), "acquired",
            "workspace preparation held the global registry flock",
        )
        release.set()
        prepare_thread.join(20)
        rollback_thread.join(20)
        self.assertEqual(prepare_error, [])
        self.assertEqual(len(rollback_error), 1)
        self.assertIn("not bound to the current Control config", str(rollback_error[0]))
        rollback_prelaunch(self.config, request.task_id)
        self.assertEqual(CreationJournalStore(self.config).read(request.task_id)["phase"], "rolled-back")

    def test_dirty_source_tracked_state_is_byte_and_revision_stable(self) -> None:
        (self.source / "tracked.txt").write_text("dirty source bytes\n", encoding="utf-8")
        before = self.source_facts()
        result = prepare_task_workspace(self.config, self.request("dirty-source"))
        self.assertEqual(before, self.source_facts())
        self.assertTrue(Path(result["jj"]["workspace_path"]).is_dir())

    def test_concurrent_repository_operation_is_not_lost_by_pinned_add(self) -> None:
        class ConcurrentOperationAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                subprocess.run([
                    "jj", "-R", str(source), "--ignore-working-copy", "bookmark", "create",
                    "concurrent-controller-test", "-r", base,
                ], check=True, capture_output=True, text=True)
                return super().add_workspace(source, destination, name, base, message, operation)

        result = prepare_task_workspace(
            self.config, self.request("concurrent-operation"), jj=ConcurrentOperationAdapter(),
        )
        bookmarks = self.jj(
            "bookmark", "list", "-T", 'name ++ " " ++ normal_target.commit_id() ++ "\\n"'
        )
        self.assertIn("concurrent-controller-test", bookmarks)
        self.assertTrue(Path(result["jj"]["workspace_path"]).is_dir())

    def test_two_concurrent_pinned_workspace_adds_do_not_corrupt_each_other(self) -> None:
        requests = [self.request("concurrent-a"), self.request("concurrent-b")]
        results: list[dict] = []
        errors: list[BaseException] = []

        def run(request):
            try:
                results.append(prepare_task_workspace(self.config, request))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        identities = JjAdapter().workspace_identities(self.source)
        for result in results:
            self.assertIn(result["jj"]["workspace_name"], identities)

    def test_destination_parent_symlink_replacement_before_add_writes_nowhere_foreign(self) -> None:
        outside = self.root / "foreign-outside"
        outside.mkdir()
        request = self.request("parent-replaced")
        replaced = False

        def inject(phase):
            nonlocal replaced
            if phase == "parent-ready" and not replaced:
                replaced = True
                journal = CreationJournalStore(self.config).read(request.task_id)
                repo_parent = Path(journal["workspace"]["path"]).parent
                moved = repo_parent.with_name(repo_parent.name + "-moved")
                repo_parent.rename(moved)
                repo_parent.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, failure_injector=inject)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rollback_resumes_after_each_owned_removal_boundary(self) -> None:
        request = self.request("remove-resume")
        prepare_task_workspace(self.config, request)
        fired = False

        def interrupt(event):
            nonlocal fired
            if not fired and (event.startswith("removed:") or event.startswith("removed-parent:")):
                fired = True
                raise RuntimeError("interrupted removal")

        with self.assertRaises(PreparationError):
            rollback_prelaunch(
                self.config, request.task_id, failure_injector=interrupt,
            )
        rollback_prelaunch(self.config, request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "rolled-back")
        self.assertFalse(Path(journal["workspace"]["path"]).exists())

    def test_foreign_data_during_removal_records_terminal_preserved_absent_registration(self) -> None:
        request = self.request("foreign-during-removal")
        prepared = prepare_task_workspace(self.config, request)
        workspace = Path(prepared["jj"]["workspace_path"])
        injected = False

        def interrupt_with_foreign(event):
            nonlocal injected
            if not injected and event.startswith("removed:"):
                injected = True
                (workspace / "foreign.ignored").write_text("user bytes", encoding="utf-8")
                raise RuntimeError("interrupted after foreign data arrived")

        with self.assertRaises(PreparationError):
            rollback_prelaunch(
                self.config, request.task_id, failure_injector=interrupt_with_foreign,
            )
        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id)

        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["jj"]["registration_state"], "absent-after-forget")
        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")
        with self.assertRaisesRegex(PreparationError, "preserved"):
            rollback_prelaunch(self.config, request.task_id)

    @staticmethod
    def _capture_error(target: list[BaseException], function, *args, **kwargs) -> None:
        try:
            function(*args, **kwargs)
        except BaseException as exc:  # recorded for the owning test thread
            target.append(exc)


if __name__ == "__main__":
    unittest.main()
