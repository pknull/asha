from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import multiprocessing
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

from lib.control import tui as tui_module
from lib.control import prepare as prepare_module
from lib.control.config import load_config
from lib.control.context import ContextError, provision_context
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import (
    DEFAULT_BASE_REVSET, JjAdapter, JjError, MaterializationEntry,
    MaterializationPlan, MAX_IMMUTABLE_TREE_ENTRIES, WorkspaceIdentity,
)
from lib.control.launch import recover_task
from lib.control.prepare import derive_repository_identity
from lib.control.prepare import (
    PrepareRequest, PreparationError, _capture_tree, _compact_materialized_ownership,
    adopt_preserved_task_workspace,
    plan_materialization, prepare_materialization, prepare_task_workspace,
    rollback_prelaunch,
)
from lib.control.store import (
    StoreCommittedError, StoreError, TaskStore, TransactionCoordinator,
    task_digest,
)
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
            "ASHA_HOME": str(root / "asha"),
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
        lock_id = TransactionCoordinator.lock_key("task", self.task_id)
        lock = self.config.runtime_dir / "tasks" / f"{lock_id}.lock"
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
        self.env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)

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

    def prepare_nested_workspace(self, relative: str, slug: str) -> tuple[PrepareRequest, dict]:
        path = self.source / relative
        path.parent.mkdir(parents=True)
        path.write_text("owned bytes\n", encoding="utf-8")
        env = {
            **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(
            ["git", "-C", str(self.source), "add", relative], check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", slug],
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
            slug=slug, label="Nested rollback race",
        )
        return request, prepare_task_workspace(self.config, request)

    @staticmethod
    def removal_quarantine_name(task_id: str, relative: str) -> str:
        digest = hashlib.sha256(
            task_id.encode("ascii") + b"\0" + relative.encode("utf-8"),
        ).hexdigest()[:32]
        return f".asha-control-remove-{digest}"

    def assert_no_workspace_removal_calls(self, unlink, rmdir) -> None:
        workspace_unlinks = [
            call for call in unlink.call_args_list
            if ".json.tmp." not in os.fspath(call.args[0])
        ]
        self.assertEqual(workspace_unlinks, [])
        rmdir.assert_not_called()

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
        with self.assertRaisesRegex(
                PreparationError, "manual inspection and cleanup required",
        ):
            rollback_prelaunch(self.config, request.task_id)
        self.assertTrue(workspace.exists())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertIn(
            result["jj"]["workspace_name"],
            JjAdapter().workspace_identities(self.source),
        )
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
            "ASHA_HOME": str(self.root / "asha"),
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
            "ASHA_HOME": str(self.root / "asha"),
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
            "ASHA_HOME": str(self.root / "asha"),
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
            "ASHA_HOME": str(self.root / "asha"),
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

    def test_failure_after_owned_manifest_retains_v2_workspace(self) -> None:
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
        self.assertEqual(journal["phase"], "preserved")
        self.assertTrue(Path(journal["workspace"]["path"]).exists())
        self.assertIn(journal["workspace"]["name"], JjAdapter().workspace_identities(self.source))

    def test_first_post_add_boundary_has_durable_exact_ownership_facts(self) -> None:
        request = self.request("ambiguous")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(
                self.config, request,
                failure_injector=lambda phase: (_ for _ in ()).throw(RuntimeError("injected"))
                if phase == "workspace-added" else None,
            )
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertIsNotNone(journal["workspace"]["root_fact"])
        self.assertIsNotNone(journal["materialization_ownership"])
        self.assertIsNotNone(journal["jj"]["change_id"])
        self.assertIsNotNone(journal["jj"]["working_commit_id"])
        self.assertIsNotNone(journal["jj"]["last_registration"])
        self.assertIsNotNone(journal["jj"]["workspace_add_operation_id"])
        self.assertIsNotNone(journal["jj"]["checkout_operation_id"])
        self.assertEqual(journal["jj"]["registration_state"], "present")
        self.assertTrue(Path(journal["workspace"]["path"]).exists())
        self.assertIn(
            journal["workspace"]["name"], JjAdapter().workspace_identities(self.source),
        )

    def test_public_operation_chain_authenticates_the_exact_workspace_add(self) -> None:
        request = self.request("operation-proof")
        prepared = prepare_task_workspace(self.config, request)
        journal = CreationJournalStore(self.config).read(request.task_id)

        proof = JjAdapter().workspace_add_operation_proof(
            self.source,
            pinned_operation_id=journal["jj"]["pinned_operation_id"],
            workspace_name=journal["workspace"]["name"],
            base_commit_id=journal["jj"]["base_commit_id"],
            description=journal["jj"]["description"],
            destination=Path(journal["workspace"]["path"]),
        )

        self.assertEqual(
            proof.workspace_add_operation_id,
            journal["jj"]["workspace_add_operation_id"],
        )
        self.assertEqual(
            proof.checkout_operation_id,
            journal["jj"]["checkout_operation_id"],
        )
        with self.assertRaisesRegex(JjError, "operation ancestry"):
            JjAdapter().workspace_add_operation_proof(
                self.source,
                pinned_operation_id=journal["jj"]["pinned_operation_id"],
                workspace_name=journal["workspace"]["name"],
                base_commit_id=journal["jj"]["base_commit_id"],
                description="different",
                destination=Path(prepared["jj"]["workspace_path"]),
            )

    def test_add_error_after_exact_registration_persists_recovery_identity(self) -> None:
        class ErrorAfterAddAdapter(JjAdapter):
            def add_workspace(inner_self, *args, **kwargs):
                super().add_workspace(*args, **kwargs)
                raise JjError("reported failure after successful add")

        request = self.request("add-error-observed")
        with self.assertRaisesRegex(PreparationError, "reported failure"):
            prepare_task_workspace(self.config, request, jj=ErrorAfterAddAdapter())

        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["jj"]["registration_state"], "present")
        self.assertIsNotNone(journal["workspace"]["root_fact"])
        self.assertIsNotNone(journal["materialization_ownership"])
        self.assertIsNotNone(journal["jj"]["workspace_add_operation_id"])
        self.assertIsNotNone(journal["jj"]["checkout_operation_id"])

    def test_explicit_adoption_authenticates_retained_null_fact_shape_forward(self) -> None:
        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        request = self.request("retained-adopt")
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)
        with self.assertRaisesRegex(PreparationError, "synthetic post-add"):
            prepare_task_workspace(self.config, request, jj=UnprovableDuringStart())
        retained = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(retained["phase"], "preserved")
        self.assertEqual(retained["jj"]["registration_state"], "add-intent")
        self.assertIsNone(retained["workspace"]["root_fact"])
        self.assertIsNone(retained["materialization_ownership"])

        with mock.patch.object(
            JjAdapter, "forget_workspace",
            side_effect=AssertionError("adoption must never forget"),
        ):
            adopted = adopt_preserved_task_workspace(
                self.config, request.task_id, harness="codex",
                role="implementer", goal="Task one",
            )

        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(adopted["lifecycle"], "creating")
        self.assertEqual(journal["phase"], "ready-for-launch")
        self.assertEqual(journal["adoption"]["state"], "ready-for-launch")
        self.assertIsNotNone(journal["workspace"]["root_fact"])
        self.assertIsNotNone(journal["materialization_ownership"])
        workspace = Path(journal["workspace"]["path"])
        self.assertTrue((workspace / ".asha" / "control-task.json").is_file())
        self.assertEqual(
            JjAdapter().inspect_workspace(
                workspace, journal["workspace"]["name"], require_empty=True,
            ).change_id,
            adopted["jj"]["change_id"],
        )

    def test_doctor_and_tui_identify_only_the_exact_forward_adoption_candidate(self) -> None:
        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        request = self.request("retained-guidance")
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=UnprovableDuringStart())

        task = TaskStore(self.config).read(request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        doctor = run_doctor(
            self.config, probes={"transactions": DEFAULT_PROBES["transactions"]},
        )["probes"][0]
        self.assertEqual(doctor["outcome"], "mismatch")
        self.assertIn("--adopt --yes --harness <harness> --role <role>", doctor["detail"])
        self.assertIn("--goal 'Task one'", doctor["detail"])

        guidance = tui_module.retained_recovery_guidance(task, journal)
        self.assertIn("explicit authenticated forward-adoption", guidance)
        self.assertIn("--adopt --yes", guidance)

        ambiguous = copy.deepcopy(journal)
        ambiguous["jj"]["registration_state"] = "present"
        self.assertIn(
            "manual inspection only",
            tui_module.retained_recovery_guidance(task, ambiguous),
        )

    def test_recovery_controller_validates_direct_adoption_authorization_before_mutation(self) -> None:
        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        request = self.request("retained-direct-auth")
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=UnprovableDuringStart())
        tasks = TaskStore(self.config)
        journals = CreationJournalStore(self.config)
        task = tasks.read(request.task_id)
        before_task = task_digest(task)
        before_journal = journals.digest(journals.read(request.task_id))

        with self.assertRaisesRegex(ValueError, "unsupported harness"):
            recover_task(
                self.config, task, tasks=tasks, journals=journals,
                adopt=True, harness="bad harness", role="implementer", goal="Task one",
            )
        self.assertEqual(task_digest(tasks.read(request.task_id)), before_task)
        self.assertEqual(journals.digest(journals.read(request.task_id)), before_journal)

        with self.assertRaisesRegex(ValueError, "exactly match"):
            recover_task(
                self.config, task, tasks=tasks, journals=journals,
                adopt=True, harness="codex", role="implementer", goal="different",
            )
        self.assertEqual(task_digest(tasks.read(request.task_id)), before_task)
        self.assertEqual(journals.digest(journals.read(request.task_id)), before_journal)

    def test_recovery_adoption_resumes_after_every_forward_durable_boundary(self) -> None:
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)

        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        boundaries = (
            "sidecar:temp-written",
            "sidecar:renamed",
            "adoption:intent",
            "adoption:context-owned:",
            "adoption:context-provisioned",
            "adoption:ready-for-launch",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                request = self.request(f"adoption-resume-{index}")
                with self.assertRaises(PreparationError):
                    prepare_task_workspace(
                        self.config, request, jj=UnprovableDuringStart(),
                    )
                fired = False

                def interrupt(observed: str) -> None:
                    nonlocal fired
                    if not fired and (
                        observed == boundary
                        or (boundary.endswith(":") and observed.startswith(boundary))
                    ):
                        fired = True
                        raise RuntimeError(f"interrupted at {observed}")

                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    adopt_preserved_task_workspace(
                        self.config, request.task_id, harness="codex",
                        role="implementer", goal="Task one",
                        failure_injector=interrupt,
                    )
                self.assertTrue(fired)

                resumed = adopt_preserved_task_workspace(
                    self.config, request.task_id, harness="codex",
                    role="implementer", goal="Task one",
                )
                journal = CreationJournalStore(self.config).read(request.task_id)
                self.assertEqual(resumed["lifecycle"], "creating")
                self.assertEqual(journal["phase"], "ready-for-launch")
                self.assertEqual(journal["adoption"]["state"], "ready-for-launch")

    def test_recovery_adoption_mismatch_matrix_preserves_retained_bytes_and_registration(self) -> None:
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)

        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        cases = ("foreign-content", "public-root", "operation-ancestry")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                request = self.request(f"adoption-refuse-{index}")
                with self.assertRaises(PreparationError):
                    prepare_task_workspace(
                        self.config, request, jj=UnprovableDuringStart(),
                    )
                journal_store = CreationJournalStore(self.config)
                journal = journal_store.read(request.task_id)
                workspace = Path(journal["workspace"]["path"])
                adapter: JjAdapter = JjAdapter()
                if case == "foreign-content":
                    (workspace / "foreign.ignored").write_bytes(b"foreign bytes\n")
                elif case == "public-root":
                    workspace.chmod(0o755)
                else:
                    class RefusingOperationAdapter(JjAdapter):
                        def workspace_add_operation_proof(inner_self, *args, **kwargs):
                            raise JjError("operation ancestry mismatch")
                    adapter = RefusingOperationAdapter()
                task_path = self.config.tasks_dir / f"{request.task_id}.json"
                journal_path = journal_store.path(request.task_id)
                if case != "public-root":
                    self.assertEqual(stat.S_IMODE(workspace.stat().st_mode), 0o700)
                before_task = task_path.read_bytes()
                before_journal = journal_path.read_bytes()
                before_workspace = self.workspace_bytes(workspace)
                before_registrations = JjAdapter().workspace_identities(self.source)

                with mock.patch.object(
                    JjAdapter, "forget_workspace",
                    side_effect=AssertionError("refusal must never forget"),
                ), self.assertRaises(PreparationError):
                    adopt_preserved_task_workspace(
                        self.config, request.task_id, harness="codex",
                        role="implementer", goal="Task one", jj=adapter,
                    )

                self.assertEqual(task_path.read_bytes(), before_task)
                self.assertEqual(journal_path.read_bytes(), before_journal)
                self.assertEqual(self.workspace_bytes(workspace), before_workspace)
                self.assertEqual(
                    JjAdapter().workspace_identities(self.source), before_registrations,
                )

    def test_distinct_task_can_start_while_retained_candidate_stays_exact(self) -> None:
        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        retained_request = self.request("retained-old")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(
                self.config, retained_request, jj=UnprovableDuringStart(),
            )
        tasks = TaskStore(self.config)
        journals = CreationJournalStore(self.config)
        retained = journals.read(retained_request.task_id)
        retained_workspace = Path(retained["workspace"]["path"])
        before_task = task_digest(tasks.read(retained_request.task_id))
        before_journal = journals.digest(retained)
        before_workspace = self.workspace_bytes(retained_workspace)
        retained_registration = JjAdapter().workspace_identities(self.source)[
            retained["workspace"]["name"]
        ]

        fresh = prepare_task_workspace(self.config, self.request("retained-new"))

        self.assertEqual(fresh["lifecycle"], "creating")
        self.assertEqual(task_digest(tasks.read(retained_request.task_id)), before_task)
        self.assertEqual(
            journals.digest(journals.read(retained_request.task_id)), before_journal,
        )
        self.assertEqual(self.workspace_bytes(retained_workspace), before_workspace)
        self.assertEqual(
            JjAdapter().workspace_identities(self.source)[retained["workspace"]["name"]],
            retained_registration,
        )

    def test_transaction_lock_domains_are_distinct_and_serialize_same_identity(self) -> None:
        coordinator = TransactionCoordinator(self.config)
        identity = str(uuid.uuid4())
        keyed = {
            domain: coordinator.lock_key(domain, identity)
            for domain in ("task", "source", "repository")
        }
        keys = set(keyed.values())
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(keyed[domain].startswith(f"{domain}-") for domain in keyed))
        with coordinator.source_lock(self.source), self.assertRaisesRegex(
            StoreError, "task -> source -> repository",
        ):
            with coordinator.task_lock(identity):
                self.fail("lock inversion must be refused before acquisition")

        for domain, value in (
            ("source", str(self.source)),
            ("repository", "repo:" + "a" * 64),
        ):
            with self.subTest(domain=domain):
                entered = threading.Event()
                release = threading.Event()
                second_entered = threading.Event()

                def first() -> None:
                    with coordinator.lock(domain, value):
                        entered.set()
                        release.wait(5)

                def second() -> None:
                    entered.wait(5)
                    with TransactionCoordinator(self.config).lock(domain, value):
                        second_entered.set()

                first_thread = threading.Thread(target=first)
                second_thread = threading.Thread(target=second)
                first_thread.start()
                second_thread.start()
                self.assertTrue(entered.wait(5))
                self.assertFalse(second_entered.wait(0.1))
                release.set()
                first_thread.join(5)
                second_thread.join(5)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())
                self.assertTrue(second_entered.is_set())

    def test_actual_start_and_adoption_separate_caller_task_from_repository_lock(self) -> None:
        class UnprovableDuringStart(JjAdapter):
            def workspace_add_operation_proof(inner_self, *args, **kwargs):
                raise JjError("synthetic post-add proof interruption")

        request = self.request("retained-lock-order")
        intents = __import__(
            "lib.control.jj", fromlist=["ColocationIntentStore"],
        ).ColocationIntentStore(self.config)
        intents.begin(self.source)
        intents.mark_verified(self.source)
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=UnprovableDuringStart())
        journal = CreationJournalStore(self.config).read(request.task_id)
        repository_lock_id = prepare_module._repository_lock_id(
            journal["repository"]["identity"],
        )
        context = multiprocessing.get_context("fork")
        adoption_source_held = context.Event()
        start_waiting_source = context.Event()
        errors = context.Queue()
        outcomes = context.Queue()

        def normal_start() -> None:
            try:
                from lib.control import cli as cli_module
                from lib.control.jj import ColocationIntentStore

                original = ColocationIntentStore.mutation_lock

                @contextlib.contextmanager
                def observed_source_wait(inner_self, source):
                    start_waiting_source.set()
                    with original(inner_self, source):
                        yield

                with mock.patch.object(
                    ColocationIntentStore, "mutation_lock", observed_source_wait,
                ), mock.patch.object(
                    cli_module.shutil, "which", return_value="/usr/bin/codex",
                ), mock.patch.object(
                    cli_module.harness_api, "launch_argv", return_value=["codex"],
                ), mock.patch.object(
                    cli_module, "_repo_argument",
                    return_value=cli_module.RepositorySelection(
                        self.source, plain_git=False,
                    ),
                ), mock.patch.object(
                    cli_module, "_ensure_colocated", return_value=(),
                ), mock.patch.object(
                    cli_module, "_start_new_task",
                    side_effect=ValueError("bounded lock-order test refusal"),
                ):
                    try:
                        cli_module._start_command_inner([
                            "--repo", str(self.source), "--task-id", repository_lock_id,
                            "--harness", "codex", "--role", "implementer",
                            "--goal", "Collision contender",
                        ], self.env)
                    except ValueError as exc:
                        if str(exc) != "bounded lock-order test refusal":
                            raise
                outcomes.put("start-refused")
            except BaseException as exc:
                errors.put(f"start: {type(exc).__name__}: {exc}")

        def recovery_adoption() -> None:
            try:
                from lib.control.jj import ColocationIntentStore

                original = ColocationIntentStore.mutation_lock

                @contextlib.contextmanager
                def observed_source_lock(inner_self, source):
                    with original(inner_self, source):
                        adoption_source_held.set()
                        if not start_waiting_source.wait(5):
                            raise RuntimeError("start never reached the source-lock boundary")
                        yield

                with mock.patch.object(
                    ColocationIntentStore, "mutation_lock", observed_source_lock,
                ):
                    adopt_preserved_task_workspace(
                        self.config, request.task_id, harness="codex",
                        role="implementer", goal="Task one",
                    )
                outcomes.put("adoption-complete")
            except BaseException as exc:
                errors.put(f"adoption: {type(exc).__name__}: {exc}")

        adopter = context.Process(target=recovery_adoption)
        adopter.start()
        self.assertTrue(adoption_source_held.wait(5))
        starter = context.Process(target=normal_start)
        starter.start()
        starter.join(15)
        adopter.join(15)
        if starter.is_alive():
            starter.terminate()
        if adopter.is_alive():
            adopter.terminate()
        starter.join(5)
        adopter.join(5)

        self.assertFalse(starter.is_alive(), "normal start lock contender deadlocked")
        self.assertFalse(adopter.is_alive(), "adoption lock contender deadlocked")
        observed_errors = []
        while not errors.empty():
            observed_errors.append(errors.get())
        self.assertEqual(observed_errors, [])
        observed_outcomes = []
        while not outcomes.empty():
            observed_outcomes.append(outcomes.get())
        self.assertCountEqual(
            observed_outcomes, ["start-refused", "adoption-complete"],
        )
        with self.assertRaisesRegex(StoreError, f"task not found: {repository_lock_id}"):
            TaskStore(self.config).read(repository_lock_id)
        with self.assertRaisesRegex(
            JournalError, f"creation journal not found: {repository_lock_id}",
        ):
            CreationJournalStore(self.config).read(repository_lock_id)
        self.assertEqual(
            TaskStore(self.config).read(request.task_id)["lifecycle"], "creating",
        )
        self.assertEqual(
            CreationJournalStore(self.config).read(request.task_id)["phase"],
            "ready-for-launch",
        )

    def test_sidecar_temp_and_rename_interruptions_retain_exact_workspace(self) -> None:
        for index, boundary in enumerate(("sidecar:temp-written", "sidecar:renamed")):
            with self.subTest(boundary=boundary):
                request = self.request(f"sidecar-{index}")

                def interrupt(phase: str) -> None:
                    if phase == boundary:
                        raise RuntimeError(boundary)

                with self.assertRaisesRegex(PreparationError, boundary):
                    prepare_task_workspace(
                        self.config, request, failure_injector=interrupt,
                    )
                journal = CreationJournalStore(self.config).read(request.task_id)
                self.assertEqual(journal["phase"], "preserved")
                self.assertTrue(Path(journal["workspace"]["path"]).exists())
                self.assertIn(
                    journal["workspace"]["name"],
                    JjAdapter().workspace_identities(self.source),
                )

    def test_recovery_reads_and_rolls_back_a_v1_inline_ownership_journal(self) -> None:
        request = self.request("legacy-v1")
        prepared = prepare_task_workspace(self.config, request)
        store = CreationJournalStore(self.config)
        journal = store.read(request.task_id)
        workspace = Path(prepared["jj"]["workspace_path"])
        expected = JjAdapter().expected_materialization(
            self.source, prepared["jj"]["base_commit_id"],
        )
        actual, _root = _capture_tree(workspace, journal["workspace"]["root_fact"])
        journal["contract"] = "asha.control-creation-journal.v1"
        journal["expected_materialization"] = expected
        journal["materialized_owned"] = _compact_materialized_ownership(actual, expected)
        journal.pop("materialization_plan")
        journal.pop("materialization_ownership")
        journal["jj"].pop("workspace_add_operation_id")
        journal["jj"].pop("checkout_operation_id")
        raw = json.dumps(
            journal, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode() + b"\n"
        store.path(request.task_id).write_bytes(raw)
        store.path(request.task_id).chmod(0o600)

        rollback_prelaunch(self.config, request.task_id)

        recovered = store.read(request.task_id)
        self.assertEqual(recovered["contract"], "asha.control-creation-journal.v1")
        self.assertEqual(recovered["phase"], "rolled-back")
        self.assertFalse(workspace.exists())

    def test_corrupt_or_replaced_v2_sidecar_preserves_before_forget(self) -> None:
        for index, replace in enumerate((False, True)):
            with self.subTest(replace=replace):
                request = self.request(f"sidecar-corrupt-{index}")
                prepared = prepare_task_workspace(self.config, request)
                store = CreationJournalStore(self.config)
                journal = store.read(request.task_id)
                sidecar = Path(
                    journal["materialization_ownership"]["sidecar"]["path"],
                )
                if replace:
                    replacement = sidecar.with_suffix(".replacement")
                    replacement.write_bytes(sidecar.read_bytes())
                    replacement.chmod(0o600)
                    os.replace(replacement, sidecar)
                else:
                    sidecar.write_bytes(sidecar.read_bytes()[:-1])

                with self.assertRaisesRegex(PreparationError, "retained"):
                    rollback_prelaunch(self.config, request.task_id)

                recovered = store.read(request.task_id)
                self.assertEqual(recovered["phase"], "preserved")
                self.assertTrue(Path(prepared["jj"]["workspace_path"]).exists())
                self.assertIn(
                    recovered["workspace"]["name"],
                    JjAdapter().workspace_identities(self.source),
                )

    def test_v2_recovery_never_forgets_a_same_name_replacement_registration(self) -> None:
        request = self.request("replacement-registration")
        prepared = prepare_task_workspace(self.config, request)
        foreign = self.root / "foreign-registration"

        class ReplacingAdapter(JjAdapter):
            production_forget_calls = 0
            replacement: WorkspaceIdentity | None = None

            def inspect_workspace(
                inner_self, destination, expected_name, **kwargs,
            ):
                authenticated = JjAdapter.inspect_workspace(
                    inner_self, destination, expected_name, **kwargs,
                )
                JjAdapter.forget_workspace(
                    inner_self, self.source, prepared["jj"]["workspace_name"],
                )
                operation = JjAdapter.pin_operation(inner_self, self.source)
                JjAdapter.add_workspace(
                    inner_self, self.source, foreign,
                    prepared["jj"]["workspace_name"],
                    prepared["jj"]["base_commit_id"],
                    "Foreign replacement", operation,
                )
                foreign.chmod(0o700)
                inner_self.replacement = JjAdapter.inspect_workspace(
                    inner_self, foreign, prepared["jj"]["workspace_name"],
                )
                return authenticated

            def forget_workspace(inner_self, source, name):
                inner_self.production_forget_calls += 1
                return JjAdapter.forget_workspace(inner_self, source, name)

        adapter = ReplacingAdapter()
        with self.assertRaisesRegex(PreparationError, "retained"):
            rollback_prelaunch(self.config, request.task_id, jj=adapter)

        self.assertEqual(adapter.production_forget_calls, 0)
        self.assertIsNotNone(adapter.replacement)
        self.assertEqual(
            JjAdapter().workspace_identities(self.source)[
                prepared["jj"]["workspace_name"]
            ],
            (adapter.replacement.change_id, adapter.replacement.commit_id),
        )
        self.assertTrue(foreign.is_dir())

    def test_partial_add_retention_requires_truthful_manual_cleanup(self) -> None:
        class PartialAddAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                raise JjError("injected after partial add")

        request = self.request("partial-manual-cleanup")
        with self.assertRaises(PreparationError) as caught:
            prepare_task_workspace(self.config, request, jj=PartialAddAdapter())

        message = str(caught.exception)
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertIn("manual inspection and cleanup required", message)
        self.assertIn(f"jj -R {self.source} workspace list", message)
        self.assertIn(journal["workspace"]["path"], message)
        self.assertNotIn("task prune", message)
        self.assertIsNotNone(journal["workspace"]["root_fact"])
        self.assertIsNotNone(journal["materialization_ownership"])
        self.assertIsNotNone(journal["jj"]["workspace_add_operation_id"])
        self.assertIsNotNone(journal["jj"]["checkout_operation_id"])
        self.assertTrue(journal["workspace"]["created_parents"])
        self.assertIn(
            journal["workspace"]["name"], JjAdapter().workspace_identities(self.source),
        )

    def test_v2_recovery_interruptions_preserve_task_and_registration(self) -> None:
        boundaries = (
            "preflight", "enumeration", "path-evidence", "inspection", "comparison",
            "mutation-evidence",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                request = self.request(f"interrupt-retention-{index}")
                prepared = prepare_task_workspace(self.config, request)
                workspace = Path(prepared["jj"]["workspace_path"])
                actual = JjAdapter().inspect_workspace(
                    workspace, prepared["jj"]["workspace_name"],
                )

                class InterruptedAdapter(JjAdapter):
                    def preflight(inner_self, repository):
                        if boundary == "preflight":
                            raise KeyboardInterrupt
                        return super().preflight(repository)

                    def workspace_identities(inner_self, repository):
                        if boundary == "enumeration":
                            raise KeyboardInterrupt
                        return super().workspace_identities(repository)

                    def inspect_workspace(inner_self, destination, expected_name, **kwargs):
                        if boundary == "inspection":
                            raise KeyboardInterrupt
                        if boundary == "comparison":
                            class InterruptedIdentity:
                                @property
                                def parent_commit_ids(self):
                                    raise KeyboardInterrupt

                                description = actual.description
                                change_id = actual.change_id
                                commit_id = actual.commit_id
                            return InterruptedIdentity()
                        return super().inspect_workspace(
                            destination, expected_name, **kwargs,
                        )

                fired = False
                real_exists = Path.exists
                real_is_dir = Path.is_dir

                def interrupted_exists(path):
                    nonlocal fired
                    if boundary == "mutation-evidence" and path == workspace and not fired:
                        fired = True
                        raise KeyboardInterrupt
                    return real_exists(path)

                def interrupted_is_dir(path):
                    nonlocal fired
                    if boundary == "path-evidence" and path == workspace and not fired:
                        fired = True
                        raise KeyboardInterrupt
                    return real_is_dir(path)

                adapter = InterruptedAdapter()
                exists_patch = mock.patch.object(Path, "exists", new=interrupted_exists)
                is_dir_patch = mock.patch.object(Path, "is_dir", new=interrupted_is_dir)
                with exists_patch, is_dir_patch, self.assertRaises(KeyboardInterrupt):
                    rollback_prelaunch(self.config, request.task_id, jj=adapter)

                self.assertEqual(
                    CreationJournalStore(self.config).read(request.task_id)["phase"],
                    "preserved",
                )
                self.assertEqual(
                    TaskStore(self.config).read(request.task_id)["lifecycle"], "failed",
                )
                self.assertIn(
                    prepared["jj"]["workspace_name"],
                    JjAdapter().workspace_identities(self.source),
                )

    def test_original_baseexception_survives_preservation_persistence_failures(self) -> None:
        cases = (
            ("keyboard-once", KeyboardInterrupt("original-keyboard-once"), False),
            ("keyboard-always", KeyboardInterrupt("original-keyboard-always"), True),
            ("system-exit-once", SystemExit(71), False),
            ("system-exit-always", SystemExit(72), True),
        )
        original_save = CreationJournalStore.save
        for slug, interruption, persistent in cases:
            with self.subTest(slug=slug):
                request = self.request(slug)
                prepared = prepare_task_workspace(self.config, request)

                class InterruptingAdapter(JjAdapter):
                    def workspace_identities(inner_self, repository):
                        raise interruption

                attempts = 0

                def fail_preserved_save(store, journal, *, expected_phase=None):
                    nonlocal attempts
                    if journal["phase"] == "preserved":
                        attempts += 1
                        if persistent or attempts == 1:
                            raise JournalError("injected preservation persistence failure")
                    return original_save(store, journal, expected_phase=expected_phase)

                with mock.patch.object(
                    CreationJournalStore, "save", new=fail_preserved_save,
                ):
                    try:
                        rollback_prelaunch(
                            self.config, request.task_id, jj=InterruptingAdapter(),
                        )
                    except BaseException as raised:
                        self.assertIs(raised, interruption)
                        notes = getattr(raised, "__notes__", [])
                        if persistent:
                            self.assertEqual(len(notes), 1)
                            self.assertIn("could not confirm", notes[0])
                        else:
                            self.assertEqual(notes, [])
                    else:
                        self.fail("the original interruption was not propagated")

                journal = CreationJournalStore(self.config).read(request.task_id)
                task = TaskStore(self.config).read(request.task_id)
                if persistent:
                    self.assertNotEqual(journal["phase"], "preserved")
                    self.assertNotEqual(task["lifecycle"], "failed")
                else:
                    self.assertEqual(journal["phase"], "preserved")
                    self.assertEqual(task["lifecycle"], "failed")
                self.assertIn(
                    prepared["jj"]["workspace_name"],
                    JjAdapter().workspace_identities(self.source),
                )

    def test_recover_and_cli_preserve_baseexception_exit_semantics_when_storage_fails(self) -> None:
        from lib.control.cli import main as control_main

        cli_env = {
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        cases = (
            ("recover-keyboard", "recover", KeyboardInterrupt("recover-keyboard"), True),
            ("recover-system-exit", "recover", SystemExit(73), False),
            ("cli-keyboard", "cli", KeyboardInterrupt("cli-keyboard"), False),
            ("cli-system-exit", "cli", SystemExit(74), True),
        )
        original_save = CreationJournalStore.save
        for slug, route, interruption, persistent in cases:
            with self.subTest(slug=slug):
                request = self.request(slug)
                prepared = prepare_task_workspace(self.config, request)

                class InterruptingAdapter(JjAdapter):
                    def workspace_identities(inner_self, repository):
                        raise interruption

                attempts = 0

                def fail_preserved_save(store, journal, *, expected_phase=None):
                    nonlocal attempts
                    if journal["phase"] == "preserved":
                        attempts += 1
                        if persistent or attempts == 1:
                            raise JournalError("injected preservation persistence failure")
                    return original_save(store, journal, expected_phase=expected_phase)

                with mock.patch.object(
                    CreationJournalStore, "save", new=fail_preserved_save,
                ):
                    if route == "recover":
                        try:
                            recover_task(
                                self.config, prepared,
                                tasks=TaskStore(self.config),
                                journals=CreationJournalStore(self.config),
                                tmux=mock.Mock(), jj=InterruptingAdapter(),
                            )
                        except BaseException as raised:
                            self.assertIs(raised, interruption)
                        else:
                            self.fail("explicit recovery replaced the original interruption")
                    elif isinstance(interruption, KeyboardInterrupt):
                        stderr = io.StringIO()
                        with mock.patch(
                            "lib.control.cli.JjAdapter", return_value=InterruptingAdapter(),
                        ), contextlib.redirect_stderr(stderr):
                            self.assertEqual(
                                control_main(["task", "recover", request.task_id], env=cli_env),
                                130,
                            )
                        self.assertIn("interrupted", stderr.getvalue())
                    else:
                        with mock.patch(
                            "lib.control.cli.JjAdapter", return_value=InterruptingAdapter(),
                        ):
                            try:
                                control_main(["task", "recover", request.task_id], env=cli_env)
                            except BaseException as raised:
                                self.assertIs(raised, interruption)
                            else:
                                self.fail("CLI replaced the original SystemExit")

    def test_ordinary_recovery_persistence_message_matches_final_store_outcome(self) -> None:
        original_save = CreationJournalStore.save
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                request = self.request(
                    "ordinary-persistent" if persistent else "ordinary-once",
                )
                prepare_task_workspace(self.config, request)

                class FailingAdapter(JjAdapter):
                    def workspace_identities(inner_self, repository):
                        raise JjError("injected ordinary recovery failure")

                attempts = 0

                def fail_preserved_save(store, journal, *, expected_phase=None):
                    nonlocal attempts
                    if journal["phase"] == "preserved":
                        attempts += 1
                        if persistent or attempts == 1:
                            raise JournalError("injected preservation persistence failure")
                    return original_save(store, journal, expected_phase=expected_phase)

                with mock.patch.object(
                    CreationJournalStore, "save", new=fail_preserved_save,
                ), self.assertRaises(PreparationError) as caught:
                    rollback_prelaunch(self.config, request.task_id, jj=FailingAdapter())

                message = str(caught.exception)
                journal = CreationJournalStore(self.config).read(request.task_id)
                task = TaskStore(self.config).read(request.task_id)
                if persistent:
                    self.assertIn(
                        "durable preserved/failed state is not confirmed", message,
                    )
                    self.assertNotIn("v2 automatic recovery retained", message)
                    self.assertNotEqual(journal["phase"], "preserved")
                    self.assertNotEqual(task["lifecycle"], "failed")
                else:
                    self.assertIn("v2 automatic recovery retained", message)
                    self.assertNotIn("not confirmed", message)
                    self.assertEqual(journal["phase"], "preserved")
                    self.assertEqual(task["lifecycle"], "failed")

    def test_keyboard_interrupt_after_workspace_add_retains_and_propagates(self) -> None:
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
        self.assertEqual(journal["phase"], "preserved")
        self.assertTrue(Path(journal["workspace"]["path"]).exists())
        self.assertIn(
            journal["workspace"]["name"],
            JjAdapter().workspace_identities(self.source),
        )

    def test_recover_ready_for_launch_retains_workspace_as_terminal_recovery(self) -> None:
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

        self.assertIn("manual inspection and cleanup required", result["message"])
        self.assertEqual(result["task"]["lifecycle"], "failed")
        self.assertEqual(result["journal"]["phase"], "preserved")
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).exists())
        after = run_doctor(
            self.config, probes={"transactions": DEFAULT_PROBES["transactions"]},
        )["probes"][0]
        self.assertEqual(after["outcome"], "mismatch")
        self.assertIn("manual inspection only", after["detail"])
        self.assertNotIn("--adopt --yes", after["detail"])

    def test_rollback_retains_registration_when_destination_jj_disappears(self) -> None:
        request = self.request("missing-destination-jj")
        prepared = prepare_task_workspace(self.config, request)
        destination = Path(prepared["jj"]["workspace_path"])

        shutil.rmtree(destination / ".jj")
        with self.assertRaises(PreparationError):
            rollback_prelaunch(self.config, request.task_id)
        self.assertIn(
            prepared["jj"]["workspace_name"],
            JjAdapter().workspace_identities(self.source),
        )

    def test_foreign_ignored_file_blocks_prelaunch_rollback(self) -> None:
        request = self.request("foreign")
        result = prepare_task_workspace(self.config, request)
        workspace = Path(result["jj"]["workspace_path"])
        (workspace / "foreign.ignored").write_text("user data", encoding="utf-8")
        from lib.control.prepare import rollback_prelaunch
        with self.assertRaisesRegex(PreparationError, "retained"):
            rollback_prelaunch(self.config, request.task_id)
        self.assertTrue((workspace / "foreign.ignored").exists())
        self.assertIn(
            result["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source),
        )

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
        with self.assertRaisesRegex(PreparationError, "retained"):
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

    def test_every_prelaunch_durable_phase_is_clean_before_mutation_or_retained_after(self) -> None:
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
                expected_phase = (
                    "rolled-back"
                    if durable_phase in {"intent", "task-recorded"}
                    else "preserved"
                )
                self.assertEqual(journal["phase"], expected_phase)
                expected_registration_state = (
                    "present"
                    if durable_phase in {
                        "workspace-added", "workspace-recorded", "context-intent", "context-provisioning",
                        "context-provisioned", "task-identity-intent",
                        "task-identity-recorded", "ready-for-launch",
                    }
                    else "add-intent" if durable_phase == "workspace-add-intent"
                    else "absent"
                )
                self.assertEqual(
                    journal["jj"]["registration_state"], expected_registration_state,
                )
                mutation_was_completed = durable_phase in {
                    "workspace-added", "workspace-recorded", "context-intent",
                    "context-provisioning", "context-provisioned",
                    "task-identity-intent", "task-identity-recorded",
                    "ready-for-launch",
                }
                self.assertEqual(
                    Path(journal["workspace"]["path"]).exists(), mutation_was_completed,
                )
                registrations = JjAdapter().workspace_identities(self.source)
                if mutation_was_completed:
                    self.assertIn(journal["workspace"]["name"], registrations)
                else:
                    self.assertNotIn(journal["workspace"]["name"], registrations)
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
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["jj"]["registration_state"], "present")
        self.assertTrue(Path(journal["workspace"]["path"]).exists())
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
        with self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id)
        journal = CreationJournalStore(self.config).read(request.task_id)
        task = TaskStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["task"]["digest"], task_digest(task))
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).exists())

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

        with self.assertRaisesRegex(PreparationError, "retained"):
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
        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")
        with self.assertRaisesRegex(PreparationError, "retained"):
            rollback_prelaunch(self.config, request.task_id)

        journal = CreationJournalStore(self.config).read(request.task_id)
        task = TaskStore(self.config).read(request.task_id)
        self.assertEqual(task["lifecycle"], "failed")
        self.assertEqual(journal["task"]["digest"], task_digest(task))
        self.assertIsNotNone(journal["task"]["failure"])
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")
        self.assertIn(
            prepared["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source),
        )

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
        self.assertIsNotNone(journal["task"]["failure"])
        self.assertEqual((workspace / "foreign.ignored").read_text(), "user bytes")
        self.assertIn(
            prepared["jj"]["workspace_name"], JjAdapter().workspace_identities(self.source),
        )

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
            "ASHA_HOME": str(self.root / "nested-asha"),
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
            "ASHA_HOME": str(self.root / "eight-asha"),
            "XDG_RUNTIME_DIR": str(self.root / "eight-runtime"),
        })
        object.__setattr__(config, "workspace_root", workspace_root)
        request = self.request("eight-parents")
        before = self.source_facts()

        prepared = prepare_task_workspace(config, request)

        journal = CreationJournalStore(config).read(request.task_id)
        self.assertEqual(len(journal["workspace"]["created_parents"]), 8)
        self.assertEqual(before, self.source_facts())
        with self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(config, request.task_id)
        self.assertTrue(list(anchor.iterdir()))
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).exists())

    def test_nine_missing_destination_ancestors_reject_before_all_mutation(self) -> None:
        anchor = self.root / "nine-parent-anchor"
        anchor.mkdir(mode=0o700)
        workspace_root = anchor.joinpath(*(f"level-{index}" for index in range(8)))
        config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(self.root / "nine-config.json"),
            "ASHA_HOME": str(self.root / "nine-asha"),
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

    def test_base_without_private_ignore_rules_refuses_before_all_creation_state(self) -> None:
        before = self.source_facts()
        before_registrations = JjAdapter().workspace_identities(self.source)
        request = PrepareRequest(
            repository=self.source, requested_base=self.no_ignore_git_commit,
            task_id=str(uuid.uuid4()), slug="base-no-ignore", label="Base no ignore",
        )
        with self.assertRaisesRegex(PreparationError, "not positively ignored"):
            prepare_task_workspace(self.config, request)
        self.assertEqual(before, self.source_facts())
        self.assertEqual(
            before_registrations, JjAdapter().workspace_identities(self.source),
        )
        self.assertFalse(CreationJournalStore(self.config).path(request.task_id).exists())
        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())
        self.assertFalse(self.config.workspace_root.exists())

    def test_more_than_v1_entry_limit_is_compact_and_prepares(self) -> None:
        bulk = self.source / "bulk"
        bulk.mkdir()
        for index in range(1100):
            (bulk / f"controller-capacity-boundary-file-{index:04d}.txt").write_text(
                f"{index}\n", encoding="utf-8",
            )
        env = {
            **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "-C", str(self.source), "add", "bulk"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.source), "commit", "-qm", "large tree"],
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
        plan = JjAdapter().materialization_plan(
            self.source / ".git", base, exact_root=self.source,
        )
        self.assertGreater(plan.entry_count, 1024)
        before = self.source_facts()

        prepared = prepare_task_workspace(self.config, request)

        self.assertEqual(before, self.source_facts())
        journal_path = CreationJournalStore(self.config).path(request.task_id)
        self.assertLessEqual(
            journal_path.stat().st_size,
            __import__("lib.control.transaction", fromlist=["MAX_JOURNAL_BYTES"]).MAX_JOURNAL_BYTES,
        )
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["contract"], "asha.control-creation-journal.v2")
        self.assertGreater(journal["materialization_plan"]["entry_count"], 1024)
        self.assertNotIn("expected_materialization", journal)
        sidecar = Path(journal["materialization_ownership"]["sidecar"]["path"])
        self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).is_dir())

    def test_unsupported_materialization_is_rejected_before_any_creation_mutation(self) -> None:
        class OversizedAdapter(JjAdapter):
            add_called = False

            def materialization_plan(inner_self, git_root, base, *, exact_root):
                entry = MaterializationEntry("entry", "040000", None, 0)
                entries = (entry,) * (MAX_IMMUTABLE_TREE_ENTRIES + 1)
                return MaterializationPlan(
                    base, "a" * 64, entries, 0, len(entries), 0,
                )

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
        request = self.request("unsupported-bytes")
        context_item = type("ContextItem", (), {
            "mode": 0o600, "sha256": "a" * 64, "content": b"",
        })()

        with mock.patch(
            "lib.control.prepare.build_context_plan",
            return_value={"x" * (260 * 1024): context_item},
        ), self.assertRaisesRegex(PreparationError, "byte (?:limit|capacity)"):
            prepare_task_workspace(self.config, request)

        self.assertFalse((self.config.tasks_dir / f"{request.task_id}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(request.task_id).exists())
        self.assertFalse(self.config.workspace_root.exists())

    def test_partial_add_exception_retains_expected_materialization(self) -> None:
        class PartialAddAdapter(JjAdapter):
            def add_workspace(inner_self, source, destination, name, base, message, operation):
                super().add_workspace(source, destination, name, base, message, operation)
                raise JjError("injected after add")

        request = self.request("partial-add")
        with self.assertRaises(PreparationError):
            prepare_task_workspace(self.config, request, jj=PartialAddAdapter())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertTrue(Path(journal["workspace"]["path"]).exists())
        self.assertIn(journal["workspace"]["name"], JjAdapter().workspace_identities(self.source))

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
        self.assertEqual(journal["phase"], "preserved")
        self.assertEqual(journal["jj"]["registration_state"], "add-intent")
        self.assertIsNone(journal["jj"]["last_registration"])
        self.assertEqual(
            __import__("lib.control.store", fromlist=["TaskStore"]).TaskStore(self.config)
            .read(request.task_id)["lifecycle"],
            "failed",
        )
        self.assertTrue(moved_paths[0].is_dir())

    def test_v2_recovery_never_calls_forget_and_retry_is_stable(self) -> None:
        request = self.request("forget-resume")
        prepared = prepare_task_workspace(self.config, request)

        class ForbidForget(JjAdapter):
            def forget_workspace(inner_self, source, name):
                self.fail("v2 recovery must not call jj workspace forget")

        with self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id, jj=ForbidForget())
        with self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id, jj=ForbidForget())
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(journal["phase"], "preserved")
        self.assertTrue(Path(prepared["jj"]["workspace_path"]).exists())
        self.assertIn(
            prepared["jj"]["workspace_name"],
            JjAdapter().workspace_identities(self.source),
        )

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
            "ASHA_HOME": str(self.root / "asha"),
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
        # Runtime-dir drift no longer breaks the journal binding: an ephemeral
        # path was never a sound durable identity (XDG_RUNTIME_DIR flips by
        # invocation context; a live audit found 42/67 journals unreadable for
        # exactly that). The other-runtime rollback therefore proceeds like a
        # same-config one and lands on the retained-recovery refusal.
        self.assertIn("automatic recovery retained", str(rollback_error[0]))
        with self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id)
        self.assertEqual(CreationJournalStore(self.config).read(request.task_id)["phase"], "preserved")
        # The DURABLE pair still binds: a config whose workspace_root differs
        # must refuse the journal outright.
        foreign_file = self.root / "foreign-config.json"
        foreign_file.write_text(json.dumps(
            {"control": {"workspace_root": str(self.root / "foreign-workspaces")}}
        ) + "\n")
        foreign_file.chmod(0o600)
        foreign_config = load_config({
            "HOME": str(self.home), "ASHA_CONFIG": str(foreign_file),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        })
        with self.assertRaisesRegex(PreparationError, "not bound to the current Control config"):
            rollback_prelaunch(foreign_config, request.task_id)

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

    def test_v1_rollback_resumes_after_an_owned_removal_boundary(self) -> None:
        request = self.request("legacy-remove-resume")
        prepared = prepare_task_workspace(self.config, request)
        store = CreationJournalStore(self.config)
        journal = store.read(request.task_id)
        workspace = Path(prepared["jj"]["workspace_path"])
        expected = JjAdapter().expected_materialization(
            self.source, prepared["jj"]["base_commit_id"],
        )
        actual, _root = _capture_tree(workspace, journal["workspace"]["root_fact"])
        journal["contract"] = "asha.control-creation-journal.v1"
        journal["expected_materialization"] = expected
        journal["materialized_owned"] = _compact_materialized_ownership(actual, expected)
        journal.pop("materialization_plan")
        journal.pop("materialization_ownership")
        journal["jj"].pop("workspace_add_operation_id")
        journal["jj"].pop("checkout_operation_id")
        raw = json.dumps(
            journal, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode() + b"\n"
        store.path(request.task_id).write_bytes(raw)
        store.path(request.task_id).chmod(0o600)
        fired = False

        def interrupt(event: str) -> None:
            nonlocal fired
            if not fired and event.startswith("removed:"):
                fired = True
                raise RuntimeError("interrupted legacy removal")

        with self.assertRaisesRegex(PreparationError, "durable recovery"):
            rollback_prelaunch(
                self.config, request.task_id, failure_injector=interrupt,
            )
        self.assertTrue(fired)

        rollback_prelaunch(self.config, request.task_id)

        recovered = store.read(request.task_id)
        self.assertEqual(recovered["contract"], "asha.control-creation-journal.v1")
        self.assertEqual(recovered["phase"], "rolled-back")
        self.assertFalse(workspace.exists())

    def test_v2_retention_never_deletes_a_replaced_tracked_leaf(self) -> None:
        request, prepared = self.prepare_nested_workspace(
            "retained/file.txt", "retain-leaf",
        )
        workspace = Path(prepared["jj"]["workspace_path"])
        visible = workspace / "retained" / "file.txt"
        owned_away = self.root / "retained-owned-leaf"
        foreign_staging = self.root / "retained-foreign-leaf"
        foreign_staging.write_text("foreign leaf\n", encoding="utf-8")
        plan = JjAdapter().materialization_plan(
            self.source / ".git", prepared["jj"]["base_commit_id"],
            exact_root=self.source,
        )
        entry = next(item for item in plan.entries if item.path == "retained/file.txt")
        parent_fd = os.open(visible.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            prepare_module._verify_plan_entry_at(parent_fd, visible.name, entry)
        finally:
            os.close(parent_fd)
        visible.rename(owned_away)
        foreign_staging.rename(visible)
        real_unlink = os.unlink
        real_rmdir = os.rmdir

        with mock.patch(
                "lib.control.prepare.os.unlink", side_effect=real_unlink,
        ) as unlink, mock.patch(
                "lib.control.prepare.os.rmdir", side_effect=real_rmdir,
        ) as rmdir, \
                self.assertRaisesRegex(
                    PreparationError, "manual inspection and cleanup required",
                ):
            rollback_prelaunch(self.config, request.task_id)

        self.assert_no_workspace_removal_calls(unlink, rmdir)
        self.assertEqual(owned_away.read_text(encoding="utf-8"), "owned bytes\n")
        self.assertEqual(visible.read_text(encoding="utf-8"), "foreign leaf\n")
        self.assertEqual(
            CreationJournalStore(self.config).read(request.task_id)["phase"],
            "preserved",
        )
        self.assertEqual(TaskStore(self.config).read(request.task_id)["lifecycle"], "failed")

    def test_v2_retention_never_deletes_a_replaced_quarantine_leaf(self) -> None:
        request, prepared = self.prepare_nested_workspace(
            "retained-q/file.txt", "retain-quarantine",
        )
        workspace = Path(prepared["jj"]["workspace_path"])
        original = workspace / "retained-q" / "file.txt"
        owned_away = self.root / "retained-owned-quarantine"
        quarantine = original.parent / self.removal_quarantine_name(
            request.task_id, "retained-q/file.txt",
        )
        foreign_staging = self.root / "retained-foreign-quarantine"
        foreign_staging.write_text("foreign quarantine\n", encoding="utf-8")
        plan = JjAdapter().materialization_plan(
            self.source / ".git", prepared["jj"]["base_commit_id"],
            exact_root=self.source,
        )
        entry = next(item for item in plan.entries if item.path == "retained-q/file.txt")
        original.rename(quarantine)
        parent_fd = os.open(quarantine.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            prepare_module._verify_plan_entry_at(parent_fd, quarantine.name, entry)
        finally:
            os.close(parent_fd)
        quarantine.rename(owned_away)
        foreign_staging.rename(quarantine)
        real_unlink = os.unlink
        real_rmdir = os.rmdir

        with mock.patch(
                "lib.control.prepare.os.unlink", side_effect=real_unlink,
        ) as unlink, mock.patch(
                "lib.control.prepare.os.rmdir", side_effect=real_rmdir,
        ) as rmdir, \
                self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id)

        self.assert_no_workspace_removal_calls(unlink, rmdir)
        self.assertEqual(owned_away.read_text(encoding="utf-8"), "owned bytes\n")
        self.assertEqual(
            quarantine.read_text(encoding="utf-8"), "foreign quarantine\n",
        )
        self.assertEqual(
            CreationJournalStore(self.config).read(request.task_id)["phase"],
            "preserved",
        )

    def test_v2_retention_never_deletes_a_replaced_workspace_root(self) -> None:
        request, prepared = self.prepare_nested_workspace(
            "root/file.txt", "retain-root",
        )
        workspace = Path(prepared["jj"]["workspace_path"])
        owned_root = self.root / "retained-owned-root"
        foreign_staging = self.root / "retained-foreign-root"
        foreign_staging.mkdir()
        foreign_staging.chmod(0o700)
        real_unlink = os.unlink
        real_rmdir = os.rmdir
        journal = CreationJournalStore(self.config).read(request.task_id)
        self.assertEqual(
            prepare_module._inode_fact(os.lstat(workspace)),
            journal["workspace"]["root_fact"],
        )
        workspace.rename(owned_root)
        foreign_staging.rename(workspace)

        with mock.patch(
                "lib.control.prepare.os.unlink", side_effect=real_unlink,
        ) as unlink, \
                mock.patch(
                    "lib.control.prepare.os.rmdir", side_effect=real_rmdir,
                ) as rmdir, \
                self.assertRaisesRegex(PreparationError, "automatic recovery retained"):
            rollback_prelaunch(self.config, request.task_id)

        self.assert_no_workspace_removal_calls(unlink, rmdir)
        self.assertEqual(
            (owned_root / "root" / "file.txt").read_text(encoding="utf-8"),
            "owned bytes\n",
        )
        self.assertTrue(workspace.is_dir())
        self.assertEqual(
            CreationJournalStore(self.config).read(request.task_id)["phase"],
            "preserved",
        )

    @staticmethod
    def _capture_error(target: list[BaseException], function, *args, **kwargs) -> None:
        try:
            function(*args, **kwargs)
        except BaseException as exc:  # recorded for the owning test thread
            target.append(exc)


if __name__ == "__main__":
    unittest.main()
