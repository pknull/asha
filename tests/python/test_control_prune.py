"""asha task prune: reclaim dead tmux sessions and workspaces of archived tasks."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.config import load_config
from lib.control.doctor import DEFAULT_PROBES, run_doctor
from lib.control.jj import JjAdapter, JjError
from lib.control.prune import (
    PRUNE_CONTRACT, PruneError, PruneRecordStore, _remove_owned_tree,
    orchestration_bindings, prunable_summary, prune_task,
)
from lib.control.store import StoreError, TaskStore, task_digest
from lib.control.tmux import TmuxAdapter
from lib.control.transaction import CreationJournalStore
from lib.control.orchestration.actions import append_event, build_action_document, submit_action
from lib.control.orchestration.cli import main as initiative_main
from lib.control.orchestration.model import BUNDLE_CONTRACT, record_digest
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_graph import seal as graph_seal


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class FakeTmux:
    def __init__(self, *, present: bool = True, owner: str | None = None,
                 managed: str = "1", panes_dead=(True,)) -> None:
        self.present = present
        self.options = {"@asha_managed": managed}
        if owner is not None:
            self.options["@asha_task_id"] = owner
        self.panes_dead = list(panes_dead)
        self.killed: list[str] = []

    def has_session(self, name):
        return self.present

    def session_option(self, name, option):
        return self.options.get(option)

    def session_pane_states(self, name):
        return list(self.panes_dead)

    def kill_session(self, name):
        self.killed.append(name)
        self.present = False


class FakeJj:
    def __init__(self, registered: set[str] | None = None, *, fail: bool = False) -> None:
        self.registered = set(registered or ())
        self.fail = fail
        self.forgotten: list[tuple[Path, str]] = []

    def workspace_identities(self, repository):
        if self.fail:
            raise JjError("no jj repository")
        return {name: ("k" * 32, "c" * 40) for name in self.registered}

    def forget_workspace(self, source, name):
        self.forgotten.append((Path(source), name))
        self.registered.discard(name)


class PruneFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "home").mkdir(mode=0o700)
        self.env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.source = self.root / "source"
        self.source.mkdir(mode=0o700)
        self.tasks = TaskStore(self.config)
        self.journals = CreationJournalStore(self.config)

    def archived_task(self, index: int = 0, *, lifecycle: str = "archived",
                      root_fact: bool = True, populate: bool = True,
                      share_path_with: dict | None = None) -> dict:
        task_id = str(uuid.uuid4())
        slug = f"prune-{index}-{task_id[:6]}"
        if share_path_with is not None:
            slug = share_path_with["slug"]
            workspace = Path(share_path_with["jj"]["workspace_path"])
            populate = False
        else:
            workspace = self.config.workspace_root / "repo-key" / slug
            workspace.mkdir(parents=True)
        current = workspace
        while current != self.root:
            current.chmod(0o700)
            current = current.parent
        if populate:
            (workspace / "src" / "deep").mkdir(parents=True)
            (workspace / "src" / "deep" / "file.txt").write_text("x\n")
            (workspace / ".asha").mkdir()
            (workspace / ".asha" / "control-task.json").write_text(
                json.dumps({"task_id": task_id}) + "\n"
            )
            os.symlink("/nonexistent/target", workspace / "dangling")
        timestamp = _timestamp()
        task = {
            "contract": "asha.control-task.v1",
            "task_id": task_id,
            "slug": slug,
            "label": "Prune fixture",
            "created_at": timestamp,
            "updated_at": timestamp,
            "lifecycle": lifecycle,
            "repository": {"root": str(self.source), "identity": "repo:" + "a" * 64},
            "source": {"kind": "ad-hoc", "number": None, "url": None},
            "jj": {
                "workspace_name": f"asha-{slug}-{task_id[:8]}",
                "workspace_path": str(workspace),
                "requested_base": "trunk()",
                "base_commit_id": "b" * 40,
                "change_id": "k" * 32,
                "working_commit_id": "c" * 40,
            },
            "tmux": {
                "socket": "default",
                "session": f"asha-{slug}-{task_id[:8]}",
                "window": "work",
            },
            "runs": [{
                "contract": "asha.control-run.v1",
                "run_id": str(uuid.uuid4()),
                "harness": "codex",
                "role": "implementer",
                "pane_id": "%7",
                "pid": 4242,
                "process_start_identity": "boot:11111111-2222-4333-8444-555555555555:start:99",
                "harness_session_id": None,
                "state": "exited",
                "evidence": "process=missing",
                "evidence_at": timestamp,
            }],
        }
        self.tasks.save(task)
        metadata = os.stat(workspace)
        journal = {
            "contract": "asha.control-creation-journal.v1",
            "task_id": task_id,
            "invocation_id": "d" * 32,
            "phase": "intent",
            "launch_attempted": False,
            "config": {
                "workspace_root": str(self.config.workspace_root),
                "tasks_dir": str(self.config.tasks_dir),
                "runtime_dir": str(self.config.runtime_dir),
            },
            "repository": {
                "root": str(self.source),
                "identity": task["repository"]["identity"],
                "git_root": str(self.source),
                "repo_key": "repo-key",
            },
            "task": {
                "record_path": str(self.config.tasks_dir / f"{task_id}.json"),
                "slug": slug,
                "label": task["label"],
                "digest": task_digest(task),
                "failure": None,
            },
            "workspace": {
                "path": str(workspace),
                "name": task["jj"]["workspace_name"],
                "root_fact": {
                    "dev": metadata.st_dev, "ino": metadata.st_ino,
                    "mode": 0o700, "uid": metadata.st_uid,
                } if root_fact else None,
                "created_parents": [],
            },
            "jj": {
                "pinned_operation_id": "e" * 128,
                "base_commit_id": task["jj"]["base_commit_id"],
                "change_id": task["jj"]["change_id"],
                "working_commit_id": task["jj"]["working_commit_id"],
                "description": task["label"],
                "registration_state": "present",
                "last_registration": {
                    "change_id": task["jj"]["change_id"],
                    "working_commit_id": task["jj"]["working_commit_id"],
                },
            },
            "expected_materialization": {},
            "materialized_owned": None,
            "recovery_owned": None,
            "planned_context": None,
            "context_owned": {},
            "removal": {
                "entries_removed": 0, "root_removed": False, "parents_removed": 0,
            },
        }
        self.journals.save(journal)
        return task

    def prune(self, task, *, tmux=None, jj=None, **kwargs):
        tmux = tmux if tmux is not None else FakeTmux(owner=task["task_id"])
        jj = jj if jj is not None else FakeJj({task["jj"]["workspace_name"]})
        result = prune_task(
            self.config, task, tasks=self.tasks, journals=self.journals,
            tmux=tmux, jj=jj, bindings=kwargs.pop("bindings", {}), **kwargs,
        )
        return result, tmux, jj


class RemoveOwnedTreeTests(PruneFixture):
    def test_removes_nested_tree_without_following_symlinks(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (outside / "keep.txt").write_text("keep\n")
        os.symlink(outside, workspace / "escape")
        os.symlink(outside / "keep.txt", workspace / "escape-file")
        root_fact = self.journals.read(task["task_id"])["workspace"]["root_fact"]
        removed = _remove_owned_tree(workspace, root_fact)
        self.assertFalse(workspace.exists())
        self.assertTrue((outside / "keep.txt").exists())
        self.assertGreaterEqual(removed, 8)

    def test_refuses_root_identity_mismatch_and_preserves_tree(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        root_fact = dict(self.journals.read(task["task_id"])["workspace"]["root_fact"])
        root_fact["ino"] += 1
        with self.assertRaisesRegex(PruneError, "identity differs"):
            _remove_owned_tree(workspace, root_fact)
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())

    def test_refuses_symlinked_root_and_mount_points(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        root_fact = self.journals.read(task["task_id"])["workspace"]["root_fact"]
        link = workspace.parent / "link"
        os.symlink(workspace, link)
        with self.assertRaises(PruneError):
            _remove_owned_tree(link, root_fact)
        with mock.patch(
            "lib.control.prune._mount_points_below", return_value=[str(workspace / "m")],
        ):
            with self.assertRaisesRegex(PruneError, "mount point"):
                _remove_owned_tree(workspace, root_fact)
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())

    def test_refuses_cross_device_and_foreign_owned_subdirectories(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        root_fact = self.journals.read(task["task_id"])["workspace"]["root_fact"]
        real_scandir = os.scandir

        def fake_entries(delta_dev: int, delta_uid: int):
            class Entry:
                name = "src"

                def stat(self, *, follow_symlinks=True):
                    metadata = os.stat(workspace / "src", follow_symlinks=False)
                    return SimpleNamespace(
                        st_mode=metadata.st_mode, st_dev=metadata.st_dev + delta_dev,
                        st_ino=metadata.st_ino, st_uid=metadata.st_uid + delta_uid,
                    )

            @contextmanager
            def scandir(fd):
                yield [Entry()]

            return scandir

        with mock.patch("os.scandir", fake_entries(1, 0)):
            with self.assertRaisesRegex(PruneError, "device boundary"):
                _remove_owned_tree(workspace, root_fact)
        with mock.patch("os.scandir", fake_entries(0, 1)):
            with self.assertRaisesRegex(PruneError, "foreign-owned"):
                _remove_owned_tree(workspace, root_fact)
        self.assertIs(os.scandir, real_scandir)
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())


class PruneTaskTests(PruneFixture):
    def test_prunes_archived_task_without_touching_record(self) -> None:
        task = self.archived_task()
        before = task_digest(self.tasks.read(task["task_id"]))
        result, tmux, jj = self.prune(task)
        self.assertEqual(result.outcome, "pruned")
        self.assertEqual(result.session.action, "killed")
        self.assertEqual(result.workspace.action, "removed")
        self.assertIn("forgotten", result.workspace.detail)
        self.assertEqual(tmux.killed, [task["tmux"]["session"]])
        self.assertEqual(jj.forgotten, [(self.source, task["jj"]["workspace_name"])])
        self.assertFalse(Path(task["jj"]["workspace_path"]).exists())
        stored = self.tasks.read(task["task_id"])
        self.assertEqual(stored["lifecycle"], "archived")
        self.assertEqual(task_digest(stored), before)
        # Idempotent: a second pass finds nothing.
        again, tmux2, jj2 = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(again.outcome, "nothing-to-prune")
        self.assertEqual(again.session.action, "absent")
        self.assertEqual(again.workspace.action, "absent")
        self.assertEqual(jj2.forgotten, [])

    def test_repeat_prune_never_removes_a_successor_at_the_same_path(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        first, _tmux, _jj = self.prune(task)
        self.assertEqual(first.workspace.action, "removed")
        record = PruneRecordStore(self.config).read(task["task_id"])
        self.assertTrue(record["workspace_removed"])
        # A successor lands at the same path; on ext4 it commonly reuses the inode.
        successor_id = str(uuid.uuid4())
        workspace.mkdir(mode=0o700)
        (workspace / ".asha").mkdir()
        (workspace / ".asha" / "control-task.json").write_text(
            json.dumps({"task_id": successor_id}) + "\n"
        )
        (workspace / "live.txt").write_text("successor work\n")
        again, _tmux, jj = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(again.workspace.action, "absent")
        self.assertIn("already reclaimed", again.workspace.detail)
        self.assertTrue((workspace / "live.txt").exists())
        # Without the prune record, and even when the filesystem hands the
        # successor the very same inode (simulated by passing the identity
        # check), the marker still refuses the successor.
        PruneRecordStore(self.config).path(task["task_id"]).unlink()
        with mock.patch("lib.control.prune.verify_owned_root", return_value=None):
            third, _tmux, jj = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(third.workspace.action, "refused")
        self.assertIn(f"task {successor_id}", third.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue((workspace / "live.txt").exists())
        # A marker-less directory at the path is refused as well.
        (workspace / ".asha" / "control-task.json").unlink()
        with mock.patch("lib.control.prune.verify_owned_root", return_value=None):
            fourth, _tmux, _jj = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(fourth.workspace.action, "refused")
        self.assertIn("names no task", fourth.workspace.detail)
        self.assertTrue((workspace / "live.txt").exists())
        # Unpatched: either the inode differs (identity refuses) or the
        # filesystem reused it (the marker refuses); both keep the tree.
        fifth, _tmux, _jj = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(fifth.workspace.action, "refused")
        self.assertTrue(
            "identity differs" in fifth.workspace.detail
            or "names no task" in fifth.workspace.detail,
            fifth.workspace.detail,
        )
        self.assertTrue((workspace / "live.txt").exists())

    def test_shared_workspace_path_in_registry_refuses_removal(self) -> None:
        task = self.archived_task()
        successor = self.archived_task(2, lifecycle="ended", share_path_with=task)
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn(f"also recorded by task {successor['task_id']}", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        workspace = Path(task["jj"]["workspace_path"])
        self.assertTrue(workspace.exists())
        # Once the directory's own marker names the successor, the old task's
        # residue is simply gone: absent, not a permanent refusal.
        (workspace / ".asha" / "control-task.json").write_text(
            json.dumps({"task_id": successor["task_id"]}) + "\n"
        )
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "absent")
        self.assertIn(f"held by task {successor['task_id']}", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())

    def test_second_generation_workspace_at_a_pruned_path_is_prunable(self) -> None:
        predecessor = self.archived_task()
        workspace = Path(predecessor["jj"]["workspace_path"])
        first, _tmux, _jj = self.prune(predecessor)
        self.assertEqual(first.workspace.action, "removed")
        # Same slug re-run: successor at the same path with its own root inode.
        workspace.mkdir(mode=0o700)
        successor = self.archived_task(2, share_path_with=predecessor)
        (workspace / ".asha").mkdir()
        (workspace / ".asha" / "control-task.json").write_text(
            json.dumps({"task_id": successor["task_id"]}) + "\n"
        )
        (workspace / "work.txt").write_text("second generation\n")
        summary = prunable_summary(
            self.config, tasks=self.tasks, tmux_for_socket=lambda socket: _PresenceTmux(set()),
        )
        self.assertEqual(summary["workspaces"], 1)
        again, _tmux, _jj = self.prune(predecessor, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(again.workspace.action, "absent")
        self.assertTrue((workspace / "work.txt").exists())
        result, _tmux, jj = self.prune(successor)
        self.assertEqual(result.workspace.action, "removed", result.workspace.detail)
        self.assertFalse(workspace.exists())

    def test_interrupted_removal_is_finished_by_the_next_pass(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        real_remove = _remove_owned_tree

        def half_remove(path, root_fact):
            # Simulate a walk that unlinked .asha and then hit an entry it
            # could not remove (permissions, or the process was killed).
            (path / ".asha" / "control-task.json").unlink()
            (path / ".asha").rmdir()
            raise PruneError("workspace removal failed: simulated EACCES; preserved")

        with mock.patch("lib.control.prune._remove_owned_tree", side_effect=half_remove):
            first, _tmux, jj = self.prune(task)
        self.assertEqual(first.workspace.action, "refused")
        self.assertIn("simulated", first.workspace.detail)
        self.assertEqual(jj.forgotten, [(self.source, task["jj"]["workspace_name"])])
        intent = PruneRecordStore(self.config).read(task["task_id"])
        self.assertIs(intent["workspace_removed"], False)
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())
        second, _tmux, jj = self.prune(task, tmux=FakeTmux(present=False), jj=FakeJj())
        self.assertEqual(second.workspace.action, "removed", second.workspace.detail)
        self.assertFalse(workspace.exists())
        self.assertIs(PruneRecordStore(self.config).read(task["task_id"])["workspace_removed"], True)
        self.assertIs(real_remove, _remove_owned_tree)

    def test_hostile_markers_refuse_without_hanging_or_crashing(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        marker = workspace / ".asha" / "control-task.json"
        marker.unlink()
        os.mkfifo(marker)
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("regular file", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        marker.unlink()
        marker.write_text("[" * 60000)
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("malformed", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue((workspace / "src" / "deep" / "file.txt").exists())

    def test_refuses_non_archived_task(self) -> None:
        task = self.archived_task(lifecycle="ended")
        result, tmux, jj = self.prune(task)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(tmux.killed, [])
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_live_pane_blocks_everything(self) -> None:
        task = self.archived_task()
        tmux = FakeTmux(owner=task["task_id"], panes_dead=(True, False))
        result, tmux, jj = self.prune(task, tmux=tmux)
        self.assertEqual(result.outcome, "refused")
        self.assertIn("still live", result.session.detail)
        self.assertEqual(tmux.killed, [])
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_foreign_session_is_kept_but_workspace_still_pruned(self) -> None:
        task = self.archived_task()
        tmux = FakeTmux(owner=str(uuid.uuid4()))
        result, tmux, jj = self.prune(task, tmux=tmux)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.session.action, "refused")
        self.assertEqual(result.workspace.action, "removed")
        self.assertEqual(tmux.killed, [])
        self.assertFalse(Path(task["jj"]["workspace_path"]).exists())

    def test_non_terminal_orchestration_binding_keeps_workspace(self) -> None:
        task = self.archived_task()
        bindings = {task["task_id"]: [{
            "initiative_id": "i", "attempt_id": "a", "state": "indeterminate",
        }]}
        result, tmux, jj = self.prune(task, bindings=bindings)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.session.action, "killed")
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("indeterminate", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())
        self.assertEqual(result.bindings, bindings[task["task_id"]])

    def test_unintegrated_terminal_seal_refuses_before_forget_or_tree_removal(self) -> None:
        task = self.archived_task()
        initiative_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        seal_id = str(uuid.uuid4())
        bindings = {task["task_id"]: [{
            "initiative_id": initiative_id,
            "attempt_id": attempt_id,
            "state": "sealed-success",
            "seal_id": seal_id,
        }]}

        result, _tmux, jj = self.prune(task, bindings=bindings)

        self.assertEqual(result.workspace.action, "refused")
        for identity in (attempt_id, seal_id, initiative_id):
            self.assertIn(identity, result.workspace.detail)
        self.assertTrue(result.workspace.detail.endswith("; kept"))
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_unreadable_orchestration_state_keeps_workspace(self) -> None:
        task = self.archived_task()
        result, _tmux, jj = self.prune(task, bindings_error="orchestration state unreadable: x")
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("unreadable", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_dry_run_changes_nothing(self) -> None:
        task = self.archived_task()
        result, tmux, jj = self.prune(task, dry_run=True)
        self.assertEqual(result.outcome, "planned")
        self.assertEqual(result.session.action, "would-kill")
        self.assertEqual(result.workspace.action, "would-remove")
        self.assertEqual(tmux.killed, [])
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_missing_root_fact_keeps_workspace(self) -> None:
        task = self.archived_task(root_fact=False)
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("does not own", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_source_repository_unavailable_keeps_workspace(self) -> None:
        task = self.archived_task()
        result, _tmux, jj = self.prune(task, jj=FakeJj(fail=True))
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("source repository unavailable", result.workspace.detail)
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_root_identity_mismatch_refuses_before_forget(self) -> None:
        task = self.archived_task()
        workspace = Path(task["jj"]["workspace_path"])
        # Same path, different inode: the journal no longer owns this root.
        moved = workspace.parent / "moved-aside"
        workspace.rename(moved)
        workspace.mkdir(mode=0o700)
        (workspace / "stray.txt").write_text("not ours\n")
        result, _tmux, jj = self.prune(task)
        self.assertEqual(result.workspace.action, "refused")
        self.assertIn("identity differs", result.workspace.detail)
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_symlinked_workspace_path_is_refused(self) -> None:
        task = self.archived_task(populate=False)
        workspace = Path(task["jj"]["workspace_path"])
        workspace.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (elsewhere / "keep.txt").write_text("keep\n")
        os.symlink(elsewhere, workspace)
        # The record itself is refused on read once its workspace path resolves
        # through a symlink, so prune cannot even reach the workspace step.
        tmux = FakeTmux(owner=task["task_id"])
        jj = FakeJj({task["jj"]["workspace_name"]})
        with self.assertRaises(StoreError):
            prune_task(
                self.config, task, tasks=self.tasks, journals=self.journals,
                tmux=tmux, jj=jj, bindings={},
            )
        self.assertEqual(tmux.killed, [])
        self.assertEqual(jj.forgotten, [])
        self.assertTrue((elsewhere / "keep.txt").exists())

    def test_keep_workspace_only_kills_session(self) -> None:
        task = self.archived_task()
        result, tmux, jj = self.prune(task, remove_workspace=False)
        self.assertEqual(result.outcome, "pruned")
        self.assertEqual(result.workspace.action, "kept")
        self.assertEqual(tmux.killed, [task["tmux"]["session"]])
        self.assertEqual(jj.forgotten, [])
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_absent_directory_still_forgets_registration(self) -> None:
        task = self.archived_task(populate=False)
        Path(task["jj"]["workspace_path"]).rmdir()
        result, _tmux, jj = self.prune(task, tmux=FakeTmux(present=False))
        self.assertEqual(result.outcome, "pruned")
        self.assertEqual(result.workspace.action, "forgotten")
        self.assertEqual(jj.forgotten, [(self.source, task["jj"]["workspace_name"])])


class PrunableSummaryAndDoctorTests(PruneFixture):
    def test_summary_counts_only_archived_residue(self) -> None:
        held = self.archived_task(1)
        self.archived_task(2, lifecycle="ended")
        gone = self.archived_task(3, populate=False)
        Path(gone["jj"]["workspace_path"]).rmdir()
        present = {held["tmux"]["session"]}
        summary = prunable_summary(
            self.config, tasks=self.tasks,
            tmux_for_socket=lambda socket: _PresenceTmux(set())
            if socket != "default" else _PresenceTmux(present),
        )
        self.assertEqual(summary, {"tasks": 1, "sessions": 1, "workspaces": 1})
        # A reclaimed root with a successor at the same path is not residue.
        PruneRecordStore(self.config).write(held["task_id"], {"workspace_removed": True})
        summary = prunable_summary(
            self.config, tasks=self.tasks,
            tmux_for_socket=lambda socket: _PresenceTmux(set()),
        )
        self.assertEqual(summary, {"tasks": 0, "sessions": 0, "workspaces": 0})

    def test_doctor_probe_reports_residue_without_failing(self) -> None:
        self.archived_task()
        with mock.patch("lib.control.doctor.TmuxAdapter") as adapter:
            adapter.return_value.session_names.return_value = []
            report = run_doctor(self.config, {"prunable": DEFAULT_PROBES["prunable"]})
        probe = report["probes"][0]
        self.assertEqual(probe["outcome"], "match")
        self.assertIn("asha task prune --all", probe["detail"])
        self.assertTrue(report["ok"])


class _PresenceTmux:
    def __init__(self, present: set[str]) -> None:
        self.present = present

    def has_session(self, name):
        return name in self.present

    def session_names(self):
        return sorted(self.present)


class PruneCliTests(PruneFixture):
    def run_cli(self, argv, *, stdin_tty: bool = False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch(
            "sys.stdin",
        ) as stdin:
            stdin.isatty.return_value = stdin_tty
            code = control_main(["task", *argv], env=self.env)
        return code, out.getvalue(), err.getvalue()

    def test_grammar_refusals(self) -> None:
        code, _out, err = self.run_cli(["prune"])
        self.assertEqual(code, 2)
        self.assertIn("task selectors or --all", err)
        code, _out, err = self.run_cli(["prune", "--all", "some-slug"])
        self.assertEqual(code, 2)
        code, _out, err = self.run_cli(["prune", "--all", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("unknown task prune argument", err)

    def test_non_tty_requires_yes_when_removing_workspaces(self) -> None:
        self.archived_task()
        with mock.patch("lib.control.cli.TmuxAdapter") as adapter:
            adapter.return_value.has_session.return_value = False
            code, _out, err = self.run_cli(["prune", "--all"])
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)

    def test_json_on_a_tty_still_needs_yes(self) -> None:
        self.archived_task()
        with mock.patch("lib.control.cli.TmuxAdapter") as adapter:
            adapter.return_value.has_session.return_value = False
            code, out, err = self.run_cli(["prune", "--all", "--json"], stdin_tty=True)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--yes", err)

    def test_dry_run_json_payload(self) -> None:
        task = self.archived_task()
        with mock.patch("lib.control.cli.TmuxAdapter") as adapter, mock.patch(
            "lib.control.cli.JjAdapter",
        ) as jj:
            adapter.return_value.has_session.return_value = False
            jj.return_value.workspace_identities.return_value = {}
            code, out, _err = self.run_cli(["prune", "--all", "--dry-run", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["contract"], PRUNE_CONTRACT)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["results"][0]["task_id"], task["task_id"])
        self.assertEqual(payload["results"][0]["outcome"], "planned")
        self.assertEqual(payload["results"][0]["workspace"]["action"], "would-remove")
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())

    def test_yes_removes_and_reports_exit_two_on_partial(self) -> None:
        task = self.archived_task(1)
        other = self.archived_task(2, root_fact=False)
        with mock.patch("lib.control.cli.TmuxAdapter") as adapter, mock.patch(
            "lib.control.cli.JjAdapter",
        ) as jj:
            adapter.return_value.has_session.return_value = False
            jj.return_value.workspace_identities.return_value = {}
            code, out, _err = self.run_cli(["prune", "--all", "--yes"])
        self.assertEqual(code, 2)
        self.assertFalse(Path(task["jj"]["workspace_path"]).exists())
        self.assertTrue(Path(other["jj"]["workspace_path"]).exists())
        self.assertIn("refused", out)
        self.assertIn("pruned", out)

    def test_keep_workspace_needs_no_confirmation(self) -> None:
        task = self.archived_task()
        with mock.patch("lib.control.cli.TmuxAdapter") as adapter:
            adapter.return_value.has_session.return_value = True
            adapter.return_value.session_option.side_effect = (
                lambda _s, key: {"@asha_managed": "1", "@asha_task_id": task["task_id"]}[key]
            )
            adapter.return_value.session_pane_states.return_value = [True]
            code, out, _err = self.run_cli(["prune", task["task_id"], "--keep-workspace"])
        self.assertEqual(code, 0)
        adapter.return_value.kill_session.assert_called_once_with(task["tmux"]["session"])
        self.assertIn("workspace kept", out)
        self.assertTrue(Path(task["jj"]["workspace_path"]).exists())


class RealTmuxPruneTests(PruneFixture):
    """One private, no-config tmux server: real dead-pane detection and kill."""

    def setUp(self) -> None:
        super().setUp()
        self.socket = f"asha-prune-test-{os.getpid()}"
        capability = subprocess.run(
            ["tmux", "-L", self.socket, "-f", "/dev/null", "list-commands", "kill-session"],
            capture_output=True, text=True, check=False,
        )
        if capability.returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        self.addCleanup(
            subprocess.run, ["tmux", "-L", self.socket, "kill-server"],
            capture_output=True, check=False,
        )
        self.adapter = TmuxAdapter(socket=self.socket)

    def session_for(self, task: dict, *, holder: list[str]) -> str:
        pane = self.adapter.create_task_session(
            session=task["tmux"]["session"], window="work",
            start_directory=self.root, environment={},
            holder_argv=holder,
            session_options={"@asha_managed": "1", "@asha_task_id": task["task_id"]},
            pane_options={"@asha_run_id": task["runs"][0]["run_id"]},
            pane_title="asha:codex:implementer",
        )
        return pane

    def wait_dead(self, session: str) -> None:
        for _ in range(100):
            if all(self.adapter.session_pane_states(session)):
                return
            time.sleep(0.05)
        self.fail("pane did not die")

    def test_dead_pane_session_is_killed_and_live_pane_blocks(self) -> None:
        dead = self.archived_task(1)
        self.session_for(dead, holder=["sh", "-c", "exit 0"])
        self.wait_dead(dead["tmux"]["session"])
        live = self.archived_task(2)
        self.session_for(live, holder=["sh", "-c", "sleep 30"])
        self.assertEqual(self.adapter.session_pane_states(live["tmux"]["session"]), [False])
        result = prune_task(
            self.config, dead, tasks=self.tasks, journals=self.journals,
            tmux=self.adapter, jj=FakeJj(), bindings={},
        )
        self.assertEqual(result.session.action, "killed")
        self.assertFalse(self.adapter.has_session(dead["tmux"]["session"]))
        blocked = prune_task(
            self.config, live, tasks=self.tasks, journals=self.journals,
            tmux=self.adapter, jj=FakeJj(), bindings={},
        )
        self.assertEqual(blocked.outcome, "refused")
        self.assertIn("still live", blocked.session.detail)
        self.assertTrue(self.adapter.has_session(live["tmux"]["session"]))
        self.assertTrue(Path(live["jj"]["workspace_path"]).exists())


class RealJjForgetTests(PruneFixture):
    """A colocated jj repository: forget leaves the Git checkout untouched."""

    def setUp(self) -> None:
        super().setUp()
        if shutil.which("jj") is None or shutil.which("git") is None:
            self.skipTest("jj and git are required")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        }
        run = lambda *argv, **kw: subprocess.run(argv, check=True, capture_output=True, env=env, **kw)
        run("git", "init", "-q", "-b", "master", str(self.source))
        (self.source / "a.txt").write_text("a\n")
        run("git", "-C", str(self.source), "add", "a.txt")
        run("git", "-C", str(self.source), "commit", "-qm", "one")
        run("git", "-C", str(self.source), "checkout", "-q", "-b", "feature")
        run("jj", "git", "init", "--colocate", str(self.source))
        self.head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_forget_and_remove_leave_head_and_change_intact(self) -> None:
        task = self.archived_task(populate=False, root_fact=False)
        workspace = Path(task["jj"]["workspace_path"])
        workspace.rmdir()
        subprocess.run(
            ["jj", "-R", str(self.source), "workspace", "add", "--name",
             task["jj"]["workspace_name"], str(workspace), "--revision", "master"],
            check=True, capture_output=True,
        )
        (workspace / "work.txt").write_text("real work\n")
        (workspace / ".asha").mkdir()
        (workspace / ".asha" / "control-task.json").write_text(
            json.dumps({"task_id": task["task_id"]}) + "\n"
        )
        subprocess.run(["jj", "-R", str(workspace), "status"], check=True, capture_output=True)
        change = subprocess.run(
            ["jj", "-R", str(workspace), "log", "-r", "@", "--no-graph", "-T", "change_id"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        # Re-journal the real root inode created by jj.
        journal = self.journals.read(task["task_id"])
        metadata = os.stat(workspace)
        journal["workspace"]["root_fact"] = {
            "dev": metadata.st_dev, "ino": metadata.st_ino,
            "mode": 0o700, "uid": metadata.st_uid,
        }
        self.journals.save(journal, expected_phase=journal["phase"])
        workspace.chmod(0o700)
        result = prune_task(
            self.config, task, tasks=self.tasks, journals=self.journals,
            tmux=FakeTmux(present=False), jj=JjAdapter(), bindings={},
        )
        self.assertEqual(result.workspace.action, "removed", result.workspace.detail)
        self.assertFalse(workspace.exists())
        listed = subprocess.run(
            ["jj", "-R", str(self.source), "workspace", "list"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertNotIn(task["jj"]["workspace_name"], listed)
        head = subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        ref = subprocess.run(
            ["git", "-C", str(self.source), "symbolic-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual((head, ref), (self.head, "refs/heads/feature"))
        described = subprocess.run(
            ["jj", "-R", str(self.source), "log", "-r", change, "--no-graph",
             "-T", "change_id"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(described, change)


class OrchestrationBindingTests(unittest.TestCase):
    def test_no_initiatives_directory_yields_no_bindings(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / "home").mkdir(mode=0o700)
        env = {
            "HOME": str(root / "home"),
            "ASHA_CONFIG": str(root / "missing.json"),
            "ASHA_HOME": str(root / "asha"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.assertEqual(orchestration_bindings(env), {})


class LinkedAttemptBindingTests(ExecutionFixture, unittest.TestCase):
    """A dispatched (running) attempt binds its Control task; prune keeps it."""

    def dispatched_attempt(self) -> tuple[dict, dict]:
        captured: dict[str, dict] = {}

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            captured["task"] = payload["task"]
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
        return captured["task"], self.store.list_attempts_snapshot(self.initiative_id)[0]

    def sealed_attempt(self) -> tuple[dict, dict, dict]:
        task, attempt = self.dispatched_attempt()
        seal = graph_seal(str(uuid.uuid4()))
        seal.update({
            "initiative_id": self.initiative_id,
            "node_id": attempt["node_id"],
            "attempt_id": attempt["attempt_id"],
            "task_id": task["task_id"],
            "run_id": task["runs"][0]["run_id"],
            "repository_id": self.initiative()["scope"]["repository"]["repository_id"],
            "scope_origin": copy.deepcopy(attempt["base"]["scope_origin"]),
            "sealed_at": _timestamp(),
        })
        self.store.save_seal(self.initiative_id, seal)
        for state in (
            "reported", "awaiting-exit", "success-seal-ready", "sealing", "sealed-success",
        ):
            current = self.store.read_attempt(self.initiative_id, attempt["attempt_id"])
            changed = copy.deepcopy(current)
            changed.update({"state": state, "updated_at": _timestamp()})
            if state in {"success-seal-ready", "sealing", "sealed-success"}:
                changed["seal_id"] = seal["seal_id"]
                changed["result_id"] = seal["result_id"]
            self.store.save_attempt(
                self.initiative_id, changed, expected_digest=record_digest(current),
            )
        return task, self.store.read_attempt(self.initiative_id, attempt["attempt_id"]), seal

    def compatible_bundle(self, seal: dict) -> dict:
        bundle = {
            "contract": BUNDLE_CONTRACT,
            "bundle_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "aggregate_spec_digest": "1" * 64,
            "active_plan_digest": self.plan["digest"],
            "state": "compatible",
            "members": [{
                "repository_id": seal["repository_id"],
                "seal_id": seal["seal_id"],
                "jj_commit_id": seal["jj_commit_id"],
                "tree_digest": seal["tree_digest"],
                "diff_digest": seal["diff_digest"],
                "materialization_id": str(uuid.uuid4()),
                "review_id": str(uuid.uuid4()),
                "verification_id": str(uuid.uuid4()),
            }],
            "controller_evidence_ids": [],
            "outcome": "compatible",
            "bound_at": _timestamp(),
        }
        self.store.save_bundle(self.initiative_id, bundle)
        return bundle

    def test_running_attempt_blocks_workspace_removal(self) -> None:
        task, attempt = self.dispatched_attempt()
        task_id = task["task_id"]
        bindings = orchestration_bindings(self.env)
        self.assertIn(task_id, bindings)
        self.assertEqual(bindings[task_id][0]["initiative_id"], self.initiative_id)
        self.assertEqual(bindings[task_id][0]["state"], "running")
        self.assertEqual(len(bindings[task_id]), 1)
        # A dispatch interrupted before the link write leaves a link-less
        # non-terminal attempt that still reserves the task: it must bind too.
        link_path = (
            self.config.initiatives_dir / self.initiative_id / "links"
            / f"{attempt['attempt_id']}.json"
        )
        link_path.unlink()
        bindings = orchestration_bindings(self.env)
        self.assertEqual(
            [entry["attempt_id"] for entry in bindings.get(task_id, [])],
            [attempt["attempt_id"]],
        )

    def test_terminal_seal_binds_until_bundle_integration_is_recorded(self) -> None:
        task, attempt, seal = self.sealed_attempt()
        bindings = orchestration_bindings(self.env)
        self.assertEqual(bindings[task["task_id"]], [{
            "initiative_id": self.initiative_id,
            "attempt_id": attempt["attempt_id"],
            "state": "sealed-success",
            "seal_id": seal["seal_id"],
        }])

        bundle = self.compatible_bundle(seal)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = initiative_main([
                "initiative", "record-integration", self.initiative_id,
                "--bundle", bundle["bundle_id"], "--json",
            ], env=self.env)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(orchestration_bindings(self.env), {})

    def test_abandoned_terminal_seal_no_longer_binds(self) -> None:
        task, attempt, seal = self.sealed_attempt()
        self.assertIn(task["task_id"], orchestration_bindings(self.env))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = initiative_main([
                "initiative", "record-integration", self.initiative_id,
                "--seal", seal["seal_id"], "--abandoned", "--reason",
                "Operator intentionally discarded this candidate.", "--json",
            ], env=self.env)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(orchestration_bindings(self.env), {})

    def test_malformed_integration_fact_makes_the_binding_scan_fail_closed(self) -> None:
        _task, _attempt, seal = self.sealed_attempt()
        append_event(
            self.store, self.initiative_id, "seal-integration-recorded",
            [seal["seal_id"]], {"disposition": "integrated", "members": []},
            actor_kind="operator", actor_id="cli",
        )
        with self.assertRaisesRegex(PruneError, "orchestration state unreadable"):
            orchestration_bindings(self.env)


if __name__ == "__main__":
    unittest.main()
