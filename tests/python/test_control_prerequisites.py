from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.config import load_config
from lib.control.cli import _parse_start, _start_new_task, main as control_main
from lib.control.doctor import _default_context_probe
from lib.control.jj import (
    ContextCompatibilityError, DefaultBaseResolution, JjAdapter, JjError,
    MaterializationPlan,
)
from lib.control.prerequisites import (
    CONTROL_IGNORE_BLOCK, CONTROL_IGNORE_RULE, CONTROL_IGNORE_RULES,
    StartPrerequisiteRefusal,
    apply_ignore_prerequisite, decode_worker_refusal,
    encode_worker_refusal,
)
from lib.control.prepare import PrepareRequest, preflight_plain_git_enablement
from lib.control.prepare import PreparationError, PreparationPrerequisiteError
from lib.control.sources import ValidatedPrRemote
from lib.control.store import StoreError, TaskStore
from lib.control.transaction import CreationJournalStore, JournalError
from lib.control.tui import _classify_start_worker_exit, _start_worker_argv
from lib.control.tui import (
    ModalCandidate, StartCandidateSnapshot, TuiModel,
    _TuiShutdown, _cell_width, _prerequisite_action_modal, _start_form, run_tui,
)
from tests.python.test_control_task_start_smoke_fixes import FakeCurses, ProgressScreen


TASK_ID = "12345678-1234-4234-8234-123456789abc"
PROJECT_ID = "12345678-9abc-4def-8123-456789abcdef"


class BoundedPrerequisiteScreen(ProgressScreen):
    """Retain actual bounded frames, not accumulated historical draw calls."""

    def __init__(self, keys, *, size=(24, 120), resized=None):
        super().__init__(keys)
        self.size = size
        self.resized = resized
        self.rows = {}
        self.frames = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.rows = {}

    def addnstr(self, y, x, value, limit, _attribute=0):
        height, width = self.size
        shown = value[:limit]
        if not (0 <= y < height and 0 <= x and x + _cell_width(shown) < width):
            raise AssertionError("modal draw exceeds the real screen bounds")
        self.rows[y] = shown

    def getch(self):
        self.frames.append("\n".join(self.rows[y] for y in sorted(self.rows)))
        if not self.keys:
            raise AssertionError("modal consumed all scripted keys without returning")
        key = super().getch()
        if key == FakeCurses.KEY_RESIZE and self.resized is not None:
            self.size = self.resized
        return key


class PrerequisiteRepository:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # jj discovers secure repository configuration from cwd even with -R.
        # Keep every fixture subprocess out of the enclosing worker checkout.
        previous_cwd = Path.cwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.root)
        self.repository = self.root / "repository"
        self.repository.mkdir(mode=0o700)
        subprocess.run(
            ["git", "init", "-q", "-b", "master", str(self.repository)],
            check=True,
        )
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        (self.repository / ".asha").mkdir()
        (self.repository / "Memory").mkdir()
        (self.repository / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": PROJECT_ID,
        }) + "\n", encoding="utf-8")
        (self.repository / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n",
            encoding="utf-8",
        )
        (self.repository / "Memory/decisions.md").write_text(
            "# Decisions\n\n- One.\n", encoding="utf-8",
        )
        (self.repository / ".gitignore").write_text(
            "/Work/session-state/\n/Work/memory-migration/\n", encoding="utf-8",
        )
        os.chmod(self.repository / ".gitignore", 0o644)
        self.commit("base")
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "HOME": str(home), "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)
        self.request = PrepareRequest(
            repository=self.repository, requested_base="master", task_id=TASK_ID,
            slug="prerequisite", label="Prerequisite", source={
                "kind": "ad-hoc", "number": None, "url": None,
            },
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *args], check=True,
            capture_output=True, text=True, env=self.git_env,
        ).stdout.strip()

    def commit(self, message: str) -> str:
        subprocess.run(
            ["git", "-C", str(self.repository), "add", "-A"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.repository), "commit", "-qm", message],
            check=True, env=self.git_env,
        )
        return self.git("rev-parse", "HEAD")

    def offer(self, *, base_explicit: bool = True):
        from lib.control.prerequisites import capture_prerequisite_offer
        try:
            preflight_plain_git_enablement(
                self.config, self.request, jj=JjAdapter(), base_explicit=base_explicit,
            )
        except Exception as exc:
            return capture_prerequisite_offer(self.config, exc)
        self.fail("preflight unexpectedly passed")


class TypedImmutableProofTests(PrerequisiteRepository, unittest.TestCase):
    def test_existing_jj_bookmark_probe_failure_is_advisory(self) -> None:
        resolved = self.git("rev-parse", "HEAD")
        adapter = mock.create_autospec(JjAdapter, instance=True)
        adapter.untracked_remote_bookmarks.side_effect = JjError(
            "bookmark inspection unavailable"
        )
        adapter.resolve_base.return_value = resolved
        adapter.materialization_plan.return_value = MaterializationPlan(
            resolved, "a" * 64, (), 0, 0, 0,
        )
        adapter.prove_context_compatibility.return_value = mock.sentinel.context_proof

        plan = preflight_plain_git_enablement(
            self.config, self.request, jj=adapter, base_explicit=True,
            existing_jj=True,
        )

        self.assertEqual(plan.resolved_base_commit_id, resolved)
        self.assertEqual(plan.diagnostics, (
            "untracked remote bookmark inspection unavailable: "
            "bookmark inspection unavailable",
        ))

    def test_missing_marker_ignore_has_exact_typed_evidence(self) -> None:
        plan = JjAdapter().materialization_plan(
            self.repository / ".git", self.git("rev-parse", "HEAD"),
            exact_root=self.repository,
        )
        with self.assertRaises(ContextCompatibilityError) as caught:
            JjAdapter().prove_context_compatibility(
                self.repository, self.repository / ".git", plan,
                project_id=PROJECT_ID,
                planned_context_paths=(
                    ".asha/config.json", ".asha/control-task.json",
                    "Memory/activeContext.md", "Memory/decisions.md",
                ), private_directory_paths=(
                    "Work/session-state/", "Work/memory-migration/",
                ),
            )
        evidence = caught.exception.evidence
        self.assertEqual(evidence.missing_paths, (".asha/control-task.json",))
        self.assertEqual(evidence.base_commit_id, self.git("rev-parse", "HEAD"))
        self.assertRegex(evidence.digest, r"^[0-9a-f]{64}$")

    def test_tracked_marker_remains_nonrepairable(self) -> None:
        marker = self.repository / ".asha/control-task.json"
        marker.write_text("{}\n", encoding="utf-8")
        self.commit("track marker")
        with self.assertRaises(Exception) as caught:
            preflight_plain_git_enablement(
                self.config, self.request, jj=JjAdapter(), base_explicit=True,
            )
        self.assertNotIsInstance(caught.exception, StartPrerequisiteRefusal)
        self.assertIn("tracks a controller-private", str(caught.exception))


class WorkerRefusalContractTests(PrerequisiteRepository, unittest.TestCase):
    def _pr_request(self, commit_id: str) -> PrepareRequest:
        return PrepareRequest(
            repository=self.repository, requested_base=commit_id,
            task_id=TASK_ID, slug="pr-7", label="PR 7",
            source={"kind": "pr", "number": 7,
                    "url": "https://github.example/owner/repository/pull/7"},
            resolved_base_commit_id=commit_id,
            pr_remote=ValidatedPrRemote(
                "origin", "https://github.example/owner/repository.git",
                "https", "a" * 64,
            ),
        )

    def test_local_pr_head_gets_typed_proof_before_any_source_mutation(self) -> None:
        request = self._pr_request(self.git("rev-parse", "HEAD"))
        before_refs = self.git("show-ref")
        with self.assertRaises(PreparationPrerequisiteError) as caught:
            preflight_plain_git_enablement(
                self.config, request, jj=JjAdapter(), base_explicit=True,
            )
        self.assertEqual(
            caught.exception.evidence.missing_paths,
            (".asha/control-task.json", ".asha/outbox/", ".asha/result.json"),
        )
        self.assertEqual(self.git("show-ref"), before_refs)
        self.assertFalse((self.repository / ".jj").exists())

    def test_private_cli_local_pr_refusal_precedes_fetch_and_colocation(self) -> None:
        oid = self.git("rev-parse", "HEAD")
        metadata = {
            "number": 7, "title": "PR seven",
            "url": "https://github.example/owner/repository/pull/7",
            "headRefOid": oid, "state": "OPEN", "isDraft": False,
            "isCrossRepository": False,
        }
        remote = ValidatedPrRemote(
            "origin", "https://github.example/owner/repository.git",
            "https", "a" * 64,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli.GithubAdapter.preflight"), \
                mock.patch(
                    "lib.control.cli.GithubAdapter.pr_metadata",
                    return_value=metadata,
                ), mock.patch(
                    "lib.control.cli.GithubAdapter.pr_remote", return_value=remote,
                ), mock.patch(
                    "lib.control.cli.GithubAdapter.fetch_pr_head",
                    side_effect=AssertionError("source fetch crossed prerequisite refusal"),
                ) as fetch, contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.repository), "--pr", "7",
                "--harness", "codex", "--goal", "x", "--task-id", TASK_ID,
                "--detach", "--json", "--tui-worker",
            ], env=self.env)
        self.assertEqual(status, 2, stderr.getvalue())
        offer = decode_worker_refusal(stdout.getvalue().encode(), TASK_ID)
        self.assertEqual(offer.base_commit_id, oid)
        self.assertEqual(offer.requested_base, oid)
        fetch.assert_not_called()
        self.assertFalse((self.repository / ".jj").exists())

    def test_remote_only_pr_head_uses_quarantine_and_refuses_without_source_mutation(self) -> None:
        remote = self.root / "remote"
        subprocess.run(["git", "init", "-q", "-b", "master", str(remote)], check=True)
        for relative in (
            ".asha/config.json", "Memory/activeContext.md",
            "Memory/decisions.md", ".gitignore",
        ):
            target = remote / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.repository / relative).read_bytes())
        subprocess.run(["git", "-C", str(remote), "add", "-A"], check=True, env=self.git_env)
        subprocess.run(
            ["git", "-C", str(remote), "commit", "-qm", "remote PR"],
            check=True, env=self.git_env,
        )
        remote_oid = subprocess.run(
            ["git", "-C", str(remote), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(self.repository), "cat-file", "-e", remote_oid],
                check=False,
            ).returncode,
            0,
        )
        self.git(
            "remote", "add", "origin",
            "https://github.example/owner/repository.git",
        )
        remote_config = JjAdapter().git_remote_configuration(self.repository)
        request = replace(
            self._pr_request(remote_oid),
            pr_remote=ValidatedPrRemote(
                "origin", "https://github.example/owner/repository.git",
                "https", remote_config.config_digest,
            ),
        )
        before_refs = self.git("show-ref")

        @contextlib.contextmanager
        def quarantine(_adapter, _source, _url, _source_ref, *, transport,
                       config_digest, expected_commit_id):
            self.assertEqual(expected_commit_id, remote_oid)
            yield remote

        with mock.patch.object(JjAdapter, "prerequisite_pr_head", quarantine):
            with self.assertRaises(PreparationPrerequisiteError) as caught:
                preflight_plain_git_enablement(
                    self.config, request, jj=JjAdapter(), base_explicit=True,
                )
        self.assertEqual(caught.exception.evidence.base_commit_id, remote_oid)
        self.assertEqual(self.git("show-ref"), before_refs)
        self.assertFalse((self.repository / ".jj").exists())

        metadata = {
            "number": 7, "title": "Remote PR",
            "url": "https://github.example/owner/repository/pull/7",
            "headRefOid": remote_oid, "state": "OPEN", "isDraft": False,
            "isCrossRepository": False,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(JjAdapter, "prerequisite_pr_head", quarantine), \
                mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                mock.patch("lib.control.cli.GithubAdapter.preflight"), \
                mock.patch(
                    "lib.control.cli.GithubAdapter.pr_metadata", return_value=metadata,
                ), mock.patch(
                    "lib.control.cli.GithubAdapter.pr_remote",
                    return_value=request.pr_remote,
                ), mock.patch(
                    "lib.control.cli.GithubAdapter.fetch_pr_head",
                    side_effect=AssertionError("source fetch crossed remote proof refusal"),
                ) as fetch, contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.repository), "--pr", "7",
                "--harness", "codex", "--goal", "x", "--task-id", TASK_ID,
                "--detach", "--json", "--tui-worker",
            ], env=self.env)
        self.assertEqual(status, 2, stderr.getvalue())
        remote_offer = decode_worker_refusal(stdout.getvalue().encode(), TASK_ID)
        self.assertEqual(remote_offer.base_commit_id, remote_oid)
        fetch.assert_not_called()
        self.assertEqual(self.git("show-ref"), before_refs)
        self.assertFalse((self.repository / ".jj").exists())
        result = apply_ignore_prerequisite(self.config, remote_offer)
        self.assertIn("Patched .gitignore", result)
        self.assertFalse((self.repository / ".jj").exists())

    def test_hidden_worker_flag_requires_explicit_private_supervision_flags(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires explicit"):
            _parse_start(["--goal", "x", "--tui-worker"])
        parsed = _parse_start([
            "--goal", "x", "--task-id", TASK_ID, "--detach", "--json",
            "--tui-worker",
        ])
        self.assertTrue(parsed["tui_worker"])

    def test_private_cli_refusal_is_json_before_plain_git_colocation(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.repository), "--base", "master",
                "--harness", "codex", "--goal", "x", "--task-id", TASK_ID,
                "--detach", "--json", "--tui-worker",
            ], env=self.env)
        self.assertEqual(status, 2)
        offer = decode_worker_refusal(stdout.getvalue().encode(), TASK_ID)
        self.assertEqual(offer.root, self.repository)
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse((self.repository / ".jj").exists())
        self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())

    @unittest.skipUnless(__import__("shutil").which("jj"), "jj is required")
    def test_existing_jj_refusal_precedes_pending_git_import_and_preserves_dirty_bytes(self) -> None:
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.repository)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        dirty = self.repository / "dirty.txt"
        dirty.write_bytes(b"pending user bytes\n")
        subprocess.run(
            ["git", "-C", str(self.repository), "update-ref",
             "refs/remotes/origin/pending", self.git("rev-parse", "HEAD")],
            check=True,
        )
        adapter = JjAdapter()
        before_operation = adapter.pin_operation(self.repository)
        before_dirty = dirty.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", return_value="/usr/bin/codex"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.repository), "--base", "master",
                "--harness", "codex", "--goal", "x", "--task-id", TASK_ID,
                "--detach", "--json", "--tui-worker",
            ], env=self.env)
        self.assertEqual(status, 2, stderr.getvalue())
        decode_worker_refusal(stdout.getvalue().encode(), TASK_ID)
        self.assertEqual(adapter.pin_operation(self.repository), before_operation)
        self.assertEqual(dirty.read_bytes(), before_dirty)
        self.assertNotIn("git import", stderr.getvalue())

    def test_private_worker_round_trip_is_task_bound_and_closed(self) -> None:
        offer = self.offer()
        raw = encode_worker_refusal(offer, TASK_ID)
        decoded = decode_worker_refusal(raw, TASK_ID)
        self.assertEqual(decoded, offer)
        value = json.loads(raw)
        value["extra"] = True
        with self.assertRaisesRegex(ValueError, "exactly"):
            decode_worker_refusal(json.dumps(value).encode(), TASK_ID)
        with self.assertRaisesRegex(ValueError, "task identity"):
            decode_worker_refusal(raw, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        duplicate = raw.replace(b'"contract":', b'"contract":"duplicate","contract":', 1)
        with self.assertRaisesRegex(ValueError, "strict UTF-8 JSON"):
            decode_worker_refusal(duplicate, TASK_ID)
        with self.assertRaisesRegex(ValueError, "oversized"):
            decode_worker_refusal(b"x" * (64 * 1024 + 1), TASK_ID)
        value = json.loads(raw)
        value["repair"]["already_covered"] = 1
        with self.assertRaisesRegex(ValueError, "working ignore evidence"):
            decode_worker_refusal(json.dumps(value).encode(), TASK_ID)

    def test_classifier_uses_stdout_contract_and_never_stderr_prose(self) -> None:
        offer = self.offer()
        tasks = mock.Mock()
        tasks.transaction_lock.return_value = contextlib.nullcontext()
        tasks.read.side_effect = StoreError(f"task not found: {TASK_ID}")
        journals = mock.Mock()
        journals.read.side_effect = JournalError(
            f"creation journal not found: {TASK_ID}"
        )
        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), mock.patch(
            "lib.control.tui.CreationJournalStore", return_value=journals,
        ):
            with self.assertRaises(StartPrerequisiteRefusal) as caught:
                _classify_start_worker_exit(
                    self.config, TASK_ID, 2, encode_worker_refusal(offer, TASK_ID),
                    b"untrusted prose claiming another repair", cancelled=False,
                )
        self.assertEqual(caught.exception.offer, offer)
        with mock.patch("lib.control.tui.TaskStore", return_value=tasks), mock.patch(
            "lib.control.tui.CreationJournalStore", return_value=journals,
        ):
            with self.assertRaises(ValueError) as generic:
                _classify_start_worker_exit(
                    self.config, TASK_ID, 2, b"not-json",
                    b"missing-positive-ignore /.asha/control-task.json",
                    cancelled=False,
                )
        self.assertNotIsInstance(generic.exception, StartPrerequisiteRefusal)

    def test_hidden_worker_flag_is_always_added_by_tui_bootstrap(self) -> None:
        argv = _start_worker_argv(["--json", "--detach", "--task-id", TASK_ID], {
            "ASHA_ROOT": str(Path(__file__).resolve().parents[2]),
        })
        self.assertIn("--tui-worker", argv)

    def test_tui_cancel_retains_every_start_form_value_for_resubmit(self) -> None:
        offer = self.offer()
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),),
            roles=("implementer",),
        )
        # Complete the form, select Cancel in the repair modal, then accept the
        # retained Base/Harness/Role/Goal fields without typing them again.
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "retained goal"), 10,
            10,  # repair modal defaults to Cancel
            10, 10, 10, 10,
        ])
        calls: list[list[str]] = []

        def supervise(*args, **_kwargs):
            calls.append(args[4])
            if len(calls) == 1:
                raise StartPrerequisiteRefusal(offer, task_id=TASK_ID, tui_worker=True)
            return "started"

        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), mock.patch(
            "lib.control.tui._default_base_candidate",
            return_value=(ModalCandidate("", "default"), offer.base_commit_id),
        ), mock.patch("lib.control.tui._source_colocation_watch", return_value=(None, False)), mock.patch(
            "lib.control.tui._supervise_start_process", side_effect=supervise,
        ):
            result = _start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )

        self.assertEqual(result, "started")
        self.assertEqual(len(calls), 2)
        for flag in ("--repo", "--harness", "--role", "--goal"):
            self.assertEqual(
                calls[0][calls[0].index(flag) + 1],
                calls[1][calls[1].index(flag) + 1],
            )
        self.assertEqual(calls[1][calls[1].index("--goal") + 1], "retained goal")
        self.assertIn(
            "Prerequisite repair cancelled",
            "\n".join(screen.lines),
        )

    def test_tui_apply_calls_only_prerequisite_transaction_and_does_not_retry(self) -> None:
        offer = self.offer()
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),), roles=("implementer",),
        )
        # Modal defaults to Cancel; Up, Up selects Apply.
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "goal"), 10,
            -997, -997, 10, 27,
        ])
        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), mock.patch(
            "lib.control.tui._default_base_candidate",
            return_value=(ModalCandidate("", "default"), offer.base_commit_id),
        ), mock.patch("lib.control.tui._source_colocation_watch", return_value=(None, False)), mock.patch(
            "lib.control.tui._supervise_start_process",
            side_effect=StartPrerequisiteRefusal(offer, task_id=TASK_ID, tui_worker=True),
        ) as supervise, mock.patch(
            "lib.control.tui.apply_ignore_prerequisite", return_value="patched",
        ) as apply:
            model = TuiModel([])
            result = _start_form(screen, FakeCurses(), model, self.env, self.config)
        self.assertEqual(result, "task start cancelled")
        supervise.assert_called_once()
        apply.assert_called_once_with(self.config, offer)
        self.assertEqual(model.message, "patched")
        self.assertIn("Notice: patched", "\n".join(screen.lines))

    def test_tui_instructions_are_visible_and_escape_does_not_mutate(self) -> None:
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        # Cancel is index 2; Up selects Instructions, Enter displays it, then
        # Escape returns to the filled form.
        screen = ProgressScreen([-997, 10, 27])
        action = _prerequisite_action_modal(
            screen, FakeCurses(), TuiModel([]), offer,
        )
        self.assertEqual(action, "cancel")
        self.assertIn("Instructions: add", "\n".join(screen.lines))
        self.assertEqual(path.read_bytes(), before)

    def test_tui_apply_refusal_retains_draft_and_returns_to_base(self) -> None:
        offer = self.offer()
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),),
            roles=("implementer",),
        )
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "kept goal"), 10,
            -997, -997, 10,  # Apply
            10, 10, 10, 10,  # retained Base/Harness/Role/Goal
        ])
        calls: list[list[str]] = []

        def supervise(*args, **_kwargs):
            calls.append(args[4])
            if len(calls) == 1:
                raise StartPrerequisiteRefusal(offer, task_id=TASK_ID, tui_worker=True)
            return "started"

        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), \
                mock.patch(
                    "lib.control.tui._default_base_candidate",
                    return_value=(ModalCandidate("", "default"), offer.base_commit_id),
                ), mock.patch(
                    "lib.control.tui._source_colocation_watch", return_value=(None, False),
                ), mock.patch(
                    "lib.control.tui._supervise_start_process", side_effect=supervise,
                ), mock.patch(
                    "lib.control.tui.apply_ignore_prerequisite",
                    side_effect=ValueError(".gitignore changed after prerequisite review"),
                ):
            model = TuiModel([])
            result = _start_form(screen, FakeCurses(), model, self.env, self.config)
        self.assertEqual(result, "started")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][calls[1].index("--goal") + 1], "kept goal")
        self.assertIn(
            "Prerequisite repair refused",
            "\n".join(screen.lines),
        )

    def test_tui_indeterminate_warning_is_drawn_before_escape(self) -> None:
        from lib.control.prerequisites import PrerequisiteApplyIndeterminate
        offer = self.offer()
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),),
            roles=("implementer",),
        )
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "kept goal"), 10,
            -997, -997, 10,  # Apply
            27,
        ])
        warning = (
            "the .gitignore replacement became visible but durable verification "
            "is indeterminate; inspect .gitignore before retrying"
        )
        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), \
                mock.patch(
                    "lib.control.tui._default_base_candidate",
                    return_value=(ModalCandidate("", "default"), offer.base_commit_id),
                ), mock.patch(
                    "lib.control.tui._source_colocation_watch", return_value=(None, False),
                ), mock.patch(
                    "lib.control.tui._supervise_start_process",
                    side_effect=StartPrerequisiteRefusal(
                        offer, task_id=TASK_ID, tui_worker=True,
                    ),
                ), mock.patch(
                    "lib.control.tui.apply_ignore_prerequisite",
                    side_effect=PrerequisiteApplyIndeterminate(warning),
                ):
            result = _start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )
        self.assertEqual(result, "task start cancelled")
        rendered = "\n".join(screen.lines)
        self.assertIn("indeterminate", rendered)
        self.assertIn("inspect .gitignore", rendered)

    def test_generic_worker_refusal_is_drawn_and_retains_draft(self) -> None:
        offer = self.offer()
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),),
            roles=("implementer",),
        )
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10, 10, 10, 10, *map(ord, "kept goal"), 10,
            10, 10, 10, 10,
        ])
        calls = 0

        def supervise(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("default base changed after worker revalidation")
            return "started"

        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), \
                mock.patch(
                    "lib.control.tui._default_base_candidate",
                    return_value=(ModalCandidate("", "default"), offer.base_commit_id),
                ), mock.patch(
                    "lib.control.tui._source_colocation_watch", return_value=(None, False),
                ), mock.patch(
                    "lib.control.tui._supervise_start_process", side_effect=supervise,
                ):
            result = _start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )
        self.assertEqual(result, "started")
        self.assertEqual(calls, 2)
        self.assertIn(
            "Task start refused: default base changed",
            "\n".join(screen.lines),
        )

    def test_blank_default_is_refreshed_and_changed_oid_requires_second_acceptance(self) -> None:
        old_oid = self.git("rev-parse", "HEAD")
        new_oid = "a" * len(old_oid)
        snapshot = StartCandidateSnapshot(
            repositories=(ModalCandidate(str(self.repository), "test"),),
            bases={str(self.repository): (ModalCandidate("", "default"),)},
            harnesses=(ModalCandidate(self.config.default_harness, "installed"),),
            roles=("implementer",),
        )
        screen = ProgressScreen([
            FakeCurses.KEY_DOWN, 10,  # repository
            10, 10,  # changed Base, then explicitly accept refreshed Base
            10, 10, *map(ord, "goal"), 10,
        ])
        previews = [
            (ModalCandidate("", "default old"), old_oid),
            (ModalCandidate("", "default new"), new_oid),
            (ModalCandidate("", "default new"), new_oid),
        ]
        calls: list[list[str]] = []

        def supervise(*args, **_kwargs):
            calls.append(args[4])
            return "started"

        with mock.patch("lib.control.tui.freeze_start_candidates", return_value=snapshot), \
                mock.patch("lib.control.tui._default_base_candidate", side_effect=previews), \
                mock.patch("lib.control.tui._source_colocation_watch", return_value=(None, False)), \
                mock.patch("lib.control.tui._supervise_start_process", side_effect=supervise):
            result = _start_form(
                screen, FakeCurses(), TuiModel([]), self.env, self.config,
            )
        self.assertEqual(result, "started")
        self.assertEqual(calls[0][calls[0].index("--expected-default") + 1], new_oid)
        self.assertIn("Default base changed", "\n".join(screen.lines))

    @unittest.skipUnless(__import__("shutil").which("jj"), "jj is required")
    def test_post_fetch_pr_proof_mismatch_refuses_before_import_and_prepare(self) -> None:
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + CONTROL_IGNORE_BLOCK, encoding="utf-8")
        oid = self.commit("authorize PR context")
        request = self._pr_request(oid)
        plan = preflight_plain_git_enablement(
            self.config, request, jj=JjAdapter(), base_explicit=True,
        )
        assert plan.materialization_plan is not None
        tampered = replace(
            plan,
            materialization_plan=replace(
                plan.materialization_plan, digest="f" * 64,
            ),
        )
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.repository)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        events: list[str] = []

        class RecordingJj(JjAdapter):
            def materialization_plan(inner, *args, **kwargs):
                events.append("source-reproof")
                return super().materialization_plan(*args, **kwargs)

            def import_git(inner, source):
                events.append("import")
                raise AssertionError("jj import crossed a mismatched PR proof")

        adapter = RecordingJj()
        github = mock.Mock()

        def fetch(source, remote, number, *, git=None):
            events.append("fetch")
            subprocess.run([
                "git", "-C", str(source), "update-ref",
                f"refs/remotes/{remote.name}/asha-control-pr-{number}", oid,
            ], check=True)
            return ()

        github.fetch_pr_head.side_effect = fetch
        parsed = _parse_start([
            "--pr", "7", "--harness", "codex", "--goal", "PR proof",
            "--detach", "--json",
        ])
        with mock.patch("lib.control.cli.GithubAdapter", return_value=github), \
                mock.patch(
                    "lib.control.cli.prepare_task_workspace",
                    side_effect=AssertionError("prepare crossed a mismatched PR proof"),
                ):
            with self.assertRaisesRegex(
                PreparationError, "materialization differs",
            ):
                _start_new_task(
                    parsed, self.env, self.config, adapter, self.repository,
                    task_id=TASK_ID, selected_harness="codex",
                    selected_role="implementer", preflight_request=request,
                    pre_enable_plan=tampered,
                )
        self.assertEqual(events, ["fetch", "source-reproof"])
        self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(TASK_ID).exists())
        self.assertFalse(tampered.destination.exists())


class ApplyOnlyTransactionTests(PrerequisiteRepository, unittest.TestCase):
    def test_apply_creates_absent_gitignore_as_mode_0644(self) -> None:
        path = self.repository / ".gitignore"
        # The selected commit retains its ignore blob, while the mutable source
        # working tree can independently lack the root file.
        path.unlink()
        offer = self.offer()
        self.assertEqual(offer.preimage.state, "absent")
        apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        self.assertEqual(path.read_text(), CONTROL_IGNORE_BLOCK)

    def test_apply_patches_only_gitignore_and_old_base_still_refuses(self) -> None:
        unrelated = self.repository / "dirty.txt"
        unrelated.write_bytes(b"unchanged dirty bytes\n")
        offer = self.offer()
        old_oid = offer.base_commit_id
        before_head = self.git("rev-parse", "HEAD")
        result = apply_ignore_prerequisite(self.config, offer)
        self.assertIn("Patched .gitignore", result)
        self.assertEqual(unrelated.read_bytes(), b"unchanged dirty bytes\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)
        self.assertEqual(old_oid, before_head)
        self.assertIn(CONTROL_IGNORE_RULE, (self.repository / ".gitignore").read_text())
        with self.assertRaises(Exception) as caught:
            preflight_plain_git_enablement(
                self.config, self.request, jj=JjAdapter(), base_explicit=True,
            )
        self.assertIn("not positively ignored", str(caught.exception))
        committed = self.commit("commit Control prerequisite")
        plan = preflight_plain_git_enablement(
            self.config, self.request, jj=JjAdapter(), base_explicit=True,
        )
        self.assertEqual(plan.resolved_base_commit_id, committed)
        self.assertIsNotNone(plan.context_compatibility)
        self.assertFalse((self.config.tasks_dir / f"{TASK_ID}.json").exists())
        self.assertFalse(CreationJournalStore(self.config).path(TASK_ID).exists())

    def test_apply_is_noop_when_worktree_already_covers_private_transport(self) -> None:
        offer = self.offer()
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + CONTROL_IGNORE_BLOCK, encoding="utf-8")
        # Capture a fresh offer bound to the already-covered worktree.
        offer = self.offer()
        before = path.read_bytes()
        result = apply_ignore_prerequisite(self.config, offer)
        self.assertIn("already ignores", result)
        self.assertEqual(path.read_bytes(), before)

    def test_apply_refuses_preimage_change_without_overwrite(self) -> None:
        offer = self.offer()
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + "# concurrent\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before)

    def test_source_proof_does_not_fallback_when_selected_object_disappears(self) -> None:
        offer = self.offer()
        self.assertEqual(offer.proof_origin, "source")
        oid = offer.base_commit_id
        object_path = self.repository / ".git/objects" / oid[:2] / oid[2:]
        self.assertTrue(object_path.is_file())
        object_path.unlink()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before)

    def test_nested_negation_is_refused_before_root_replacement(self) -> None:
        nested = self.repository / ".asha/.gitignore"
        nested.write_text("!control-task.json\n", encoding="utf-8")
        os.chmod(nested, 0o644)
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "nested|effectively ignore"):
            apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_exact_limit_preimage_refuses_oversized_intended_bytes_before_write(self) -> None:
        from lib.control.jj import MAX_TRACKED_BLOB_BYTES
        path = self.repository / ".gitignore"
        path.write_bytes(b"#" * MAX_TRACKED_BLOB_BYTES)
        os.chmod(path, 0o644)
        offer = self.offer()
        before = path.stat()
        with self.assertRaisesRegex(ValueError, "intended.*bounded|too large"):
            apply_ignore_prerequisite(self.config, offer)
        after = path.stat()
        self.assertEqual((after.st_size, after.st_ino), (before.st_size, before.st_ino))
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_second_preimage_race_refuses_and_removes_temporary_file(self) -> None:
        import lib.control.prerequisites as prerequisites
        offer = self.offer()
        path = self.repository / ".gitignore"
        real_read = prerequisites._read_ignore_preimage
        calls = 0

        def race(root: Path):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text(path.read_text() + "# external editor\n", encoding="utf-8")
                os.chmod(path, 0o644)
            return real_read(root)

        with mock.patch("lib.control.prerequisites._read_ignore_preimage", side_effect=race):
            with self.assertRaisesRegex(ValueError, "immediately before"):
                apply_ignore_prerequisite(self.config, offer)
        self.assertTrue(path.read_text().endswith("# external editor\n"))
        self.assertNotIn(CONTROL_IGNORE_RULE, path.read_text())
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_offer_rejects_symlink_gitignore(self) -> None:
        target = self.root / "outside"
        target.write_text("outside\n", encoding="utf-8")
        path = self.repository / ".gitignore"
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular file|symlink"):
            self.offer()
        self.assertEqual(target.read_text(), "outside\n")

    def test_offer_rejects_hardlinked_or_group_writable_gitignore(self) -> None:
        path = self.repository / ".gitignore"
        link = self.root / "hardlink"
        os.link(path, link)
        with self.assertRaisesRegex(ValueError, "one file"):
            self.offer()
        link.unlink()
        os.chmod(path, 0o664)
        with self.assertRaisesRegex(ValueError, "group/other writable"):
            self.offer()

    def test_apply_refuses_project_or_default_change_before_write(self) -> None:
        default_request = PrepareRequest(
            repository=self.repository, task_id=TASK_ID, slug="prerequisite",
            label="Prerequisite", source={"kind": "ad-hoc", "number": None, "url": None},
        )
        original = self.request
        self.request = default_request
        offer = self.offer(base_explicit=False)
        self.request = original
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        (self.repository / "tracked.txt").write_text("advance\n", encoding="utf-8")
        self.commit("move default")
        with self.assertRaisesRegex(ValueError, "default base changed"):
            apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before)

        explicit_offer = self.offer()
        config_path = self.repository / ".asha/config.json"
        config_value = json.loads(config_path.read_text())
        config_value["project_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        config_path.write_text(json.dumps(config_value) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "project identity changed"):
            apply_ignore_prerequisite(self.config, explicit_offer)
        self.assertEqual(path.read_bytes(), before)

    def test_post_replace_fsync_failure_is_reported_indeterminate(self) -> None:
        from lib.control.prerequisites import PrerequisiteApplyIndeterminate
        offer = self.offer()
        with mock.patch(
            "lib.control.prerequisites._fsync_directory",
            side_effect=OSError("forced directory fsync failure"),
        ):
            with self.assertRaises(PrerequisiteApplyIndeterminate):
                apply_ignore_prerequisite(self.config, offer)
        self.assertIn(CONTROL_IGNORE_RULE, (self.repository / ".gitignore").read_text())
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_visible_replace_then_error_is_reported_indeterminate(self) -> None:
        from lib.control.prerequisites import PrerequisiteApplyIndeterminate
        offer = self.offer()
        real_replace = os.replace

        def visible_then_error(*args, **kwargs):
            real_replace(*args, **kwargs)
            raise OSError("error after visible rename")

        with mock.patch("lib.control.prerequisites.os.replace", side_effect=visible_then_error):
            with self.assertRaises(PrerequisiteApplyIndeterminate):
                apply_ignore_prerequisite(self.config, offer)
        self.assertIn(CONTROL_IGNORE_RULE, (self.repository / ".gitignore").read_text())
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_keyboard_interrupt_and_system_exit_cross_rename_unchanged(self) -> None:
        path = self.repository / ".gitignore"
        real_replace = os.replace
        for raised_type, visible in (
            (KeyboardInterrupt, False), (KeyboardInterrupt, True),
            (lambda: SystemExit(17), False), (lambda: SystemExit(17), True),
        ):
            offer = self.offer()
            before = path.read_bytes()
            raised = raised_type()

            def interrupting_replace(*args, **kwargs):
                if visible:
                    real_replace(*args, **kwargs)
                raise raised

            with self.subTest(
                kind=type(raised).__name__, visible=visible,
            ), mock.patch(
                "lib.control.prerequisites.os.replace",
                side_effect=interrupting_replace,
            ):
                with self.assertRaises(BaseException) as caught:
                    apply_ignore_prerequisite(self.config, offer)
            self.assertIs(caught.exception, raised)
            self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])
            if visible:
                self.assertIn(CONTROL_IGNORE_RULE, path.read_text())
                path.write_bytes(before)
                os.chmod(path, 0o644)
            else:
                self.assertEqual(path.read_bytes(), before)

    def test_run_tui_preserves_signal_shutdown_and_reports_indeterminate(self) -> None:
        offer = self.offer()
        real_replace = os.replace

        class Tty(io.StringIO):
            def isatty(self):
                return True

        class SignalCurses:
            class error(Exception):
                pass

            @staticmethod
            def setupterm():
                return None

            @staticmethod
            def wrapper(*_args):
                return apply_ignore_prerequisite(self.config, offer)

        for signum, visible in (
            (__import__("signal").SIGTERM, False),
            (__import__("signal").SIGHUP, True),
        ):
            with self.subTest(signum=signum):
                # Each signal case needs a fresh offer/preimage. Restore the
                # original bytes after the visible case only after assertions.
                before = (self.repository / ".gitignore").read_bytes()
                shutdown = _TuiShutdown(signum)

                def interrupting_replace(*args, **kwargs):
                    if visible:
                        real_replace(*args, **kwargs)
                    raise shutdown

                errors = Tty()
                with mock.patch("lib.control.tui._load_rows", return_value=[]), \
                        mock.patch("lib.control.tui._surface_skipped"), \
                        mock.patch(
                            "lib.control.prerequisites.os.replace",
                            side_effect=interrupting_replace,
                        ):
                    status = run_tui(
                        self.env, stdin=Tty(), stdout=Tty(), stderr=errors,
                        curses_module=SignalCurses,
                    )
                self.assertEqual(status, 128 + signum)
                self.assertIn("indeterminate", errors.getvalue())
                self.assertIn("inspect .gitignore", errors.getvalue())
                self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])
                if visible:
                    self.assertIn(
                        CONTROL_IGNORE_RULE,
                        (self.repository / ".gitignore").read_text(),
                    )
                    # Restore only the disposable fixture for the second loop's
                    # cleanup; no live repository is involved.
                    (self.repository / ".gitignore").write_bytes(before)
                    os.chmod(self.repository / ".gitignore", 0o644)
                else:
                    self.assertEqual(
                        (self.repository / ".gitignore").read_bytes(), before,
                    )
                offer = self.offer()

    def test_temporary_is_dirfd_bound_and_cleaned_across_root_swap(self) -> None:
        import lib.control.prerequisites as prerequisites
        offer = self.offer()
        reviewed = self.repository
        moved = self.root / "reviewed-moved"
        replacement = self.root / "replacement"
        real_create = prerequisites._create_temporary_at

        def swap_then_create(directory_fd: int, mode: int):
            reviewed.rename(moved)
            replacement.mkdir(mode=0o700)
            replacement.rename(reviewed)
            try:
                return real_create(directory_fd, mode)
            finally:
                reviewed.rename(replacement)
                moved.rename(reviewed)

        with mock.patch(
            "lib.control.prerequisites._create_temporary_at",
            side_effect=swap_then_create,
        ), mock.patch(
            "lib.control.prerequisites._revalidate_offer_repository",
            wraps=prerequisites._revalidate_offer_repository,
        ):
            apply_ignore_prerequisite(self.config, offer)
        self.assertIn(CONTROL_IGNORE_RULE, (reviewed / ".gitignore").read_text())
        self.assertEqual(list(reviewed.glob(".gitignore.asha-control.*")), [])
        self.assertEqual(list(replacement.glob(".gitignore.asha-control.*")), [])


class PrivateTransportProducerTests(PrerequisiteRepository, unittest.TestCase):
    def _run_private_cli(self, selected: str):
        import shutil
        which = shutil.which
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("lib.control.cli.shutil.which", side_effect=lambda name, *a, **kw:
                        "/bin/python3" if name == "codex" else which(name, *a, **kw)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = control_main([
                "task", "start", "--repo", str(self.repository), "--base", selected,
                "--harness", "codex", "--goal", "producer E2E", "--task-id", TASK_ID,
                "--headless", "--detach", "--json", "--tui-worker",
            ], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_every_finite_missing_set_round_trips_and_repairs_only_authorized_rules(self):
        path = self.repository / ".gitignore"
        for count in range(1, 4):
            for missing_rules in itertools.combinations(CONTROL_IGNORE_RULES, count):
                with self.subTest(missing_rules=missing_rules):
                    existing = ("# user policy\n*.cache\n!important.cache\n"
                                "/Work/session-state/\n/Work/memory-migration/\n")
                    existing += "".join(rule + "\n" for rule in CONTROL_IGNORE_RULES
                                        if rule not in missing_rules)
                    path.write_text(existing)
                    old = self.commit("partial private policy")
                    self.request = replace(self.request, requested_base=old)
                    offer = self.offer()
                    self.assertEqual(offer.rules, missing_rules)
                    self.assertEqual(offer.evidence.missing_paths,
                                     tuple(sorted(rule[1:] for rule in missing_rules)))
                    decoded = decode_worker_refusal(encode_worker_refusal(offer, TASK_ID), TASK_ID)
                    self.assertEqual(decoded, offer)
                    apply_ignore_prerequisite(self.config, decoded)
                    self.assertEqual(path.read_text(), existing +
                                     "# Asha Control private context (managed)\n" +
                                     "".join(rule + "\n" for rule in missing_rules))
                    self.assertEqual(self.git("rev-parse", "HEAD"), old)
                    self.assertEqual(self.offer().base_commit_id, old)
                    new = self.commit("explicitly authorize private policy")
                    # Even after a commit, an explicitly selected old base is
                    # unauthorized until the user selects the new commit.
                    self.assertEqual(self.offer().base_commit_id, old)
                    self.request = replace(self.request, requested_base=new)
                    proof = preflight_plain_git_enablement(
                        self.config, self.request, jj=JjAdapter(), base_explicit=True,
                    )
                    self.assertEqual(proof.resolved_base_commit_id, new)
                    self.assertFalse((self.repository / ".jj").exists())

    def test_stored_legacy_singleton_keeps_marker_only_authorization(self):
        from lib.control.prerequisites import _working_ignore_state
        path = self.repository / ".gitignore"
        recovery = path.read_text()
        path.write_text(recovery + "/.asha/result.json\n/.asha/outbox/\n")
        old = self.commit("base whose only missing path is legacy marker")
        self.request = replace(self.request, requested_base=old)
        # A legacy offer binds the exact mutable preimage, not an implied
        # authorization to recreate newer rules removed by the user.
        path.write_text(recovery)
        offer = self.offer()
        self.assertEqual(offer.rules, (CONTROL_IGNORE_RULE,))
        stored = encode_worker_refusal(offer, TASK_ID)
        legacy_digest = hashlib.sha256(b"asha-control-working-ignore-v1\0")
        legacy_digest.update(b".gitignore\0" + hashlib.sha256(path.read_bytes()).digest())
        legacy_digest.update(b".asha/.gitignore\0absent\0")
        self.assertEqual(offer.working_ignore_digest, legacy_digest.hexdigest())
        apply_ignore_prerequisite(self.config, decode_worker_refusal(stored, TASK_ID))
        self.assertEqual(path.read_text(), recovery +
                         "# Asha Control private context (managed)\n/.asha/control-task.json\n")
        self.assertFalse(_working_ignore_state(self.repository)[1])
        self.assertTrue(_working_ignore_state(self.repository, rules=(CONTROL_IGNORE_RULE,))[1])
        self.assertEqual(self.offer().base_commit_id, old)
        current = self.commit("only the authorized legacy patch")
        self.request = replace(self.request, requested_base=current)
        self.assertEqual(self.offer().rules, ("/.asha/result.json", "/.asha/outbox/"))

    def test_legacy_terminal_marker_block_upgrades_without_losing_comments(self):
        path = self.repository / ".gitignore"
        before = (path.read_bytes() + b"# user notes\r\n*.cache\r\n" +
                  b"# Asha Control private context (managed)\n/.asha/control-task.json\n")
        path.write_bytes(before)
        old = self.commit("legacy initialization")
        self.request = replace(self.request, requested_base=old)
        offer = self.offer()
        self.assertEqual(offer.rules, ("/.asha/result.json", "/.asha/outbox/"))
        apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before + b"/.asha/result.json\n/.asha/outbox/\n")
        repaired = path.read_bytes()
        self.assertTrue(self.offer().already_covered)
        apply_ignore_prerequisite(self.config, self.offer())
        self.assertEqual(path.read_bytes(), repaired)

    def test_forged_rule_arrays_and_direct_offers_never_broaden_patch(self):
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        for rules in ([], [CONTROL_IGNORE_RULE], list(reversed(offer.rules)),
                      list(offer.rules) + [CONTROL_IGNORE_RULE], ["/.asha/"],
                      ["/.asha/outbox"], ["/.asha/outbox/leaf"], ["/Work/session-state/"],
                      ["/.asha/../result.json"], [1], "not an array"):
            with self.subTest(rules=rules):
                value = json.loads(encode_worker_refusal(offer, TASK_ID))
                value["repair"]["rules"] = rules
                with self.assertRaises(ValueError):
                    decode_worker_refusal(json.dumps(value).encode(), TASK_ID)
                if isinstance(rules, list):
                    with self.assertRaises(ValueError):
                        apply_ignore_prerequisite(self.config, replace(offer, rules=tuple(rules)))
                self.assertEqual(path.read_bytes(), before)
        value = json.loads(encode_worker_refusal(offer, TASK_ID))
        for missing in ([".asha/outbox"], [".asha/outbox/leaf"], ["Work/session-state/"],
                        [".asha/outbox//"], [".asha/outbox/", ".asha/outbox/"]):
            forged = copy.deepcopy(value)
            forged["proof"]["missing_paths"] = missing
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                decode_worker_refusal(json.dumps(forged).encode(), TASK_ID)

    def test_singleton_wire_cannot_be_broadened_to_new_rules(self):
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + "/.asha/result.json\n/.asha/outbox/\n")
        self.commit("only marker missing")
        offer = self.offer()
        value = json.loads(encode_worker_refusal(offer, TASK_ID))
        value["repair"]["rules"] = list(CONTROL_IGNORE_RULES)
        with self.assertRaisesRegex(ValueError, "exact patch"):
            decode_worker_refusal(json.dumps(value).encode(), TASK_ID)
        with self.assertRaisesRegex(ValueError, "exact patch"):
            apply_ignore_prerequisite(self.config, replace(offer, rules=CONTROL_IGNORE_RULES))

    def test_repair_modal_discloses_every_rule_before_authorizing_multi_path_patch(self):
        offer = self.offer()
        self.assertEqual(offer.rules, CONTROL_IGNORE_RULES)
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        for keys in ([27], [-997, 10, 27]):
            with self.subTest(show_instructions=len(keys) > 1):
                screen = ProgressScreen(keys)
                action = _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer)
                self.assertEqual(action, "cancel")
                self.assertEqual(path.read_bytes(), before)
                rendered = "\n".join(screen.lines)
                self.assertEqual([rule for rule in offer.rules if rule not in rendered], [],
                                 "repair preview/instructions must disclose the whole authorized patch")

    def test_repair_modal_apply_requires_the_complete_current_frame(self):
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        screen = BoundedPrerequisiteScreen([-997, -997, 10])
        action = _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer)
        self.assertEqual(action, "apply")
        # The modal returns authority to its caller; it cannot write itself.
        self.assertEqual(path.read_bytes(), before)
        for frame in (screen.frames[0], screen.frames[-1]):
            for rule in offer.rules:
                self.assertIn(f"Add: {rule}", frame)
            self.assertIn(str(offer.root), frame)
            self.assertIn(offer.base_commit_id, frame)
            self.assertIn(offer.target, frame)
        self.assertIn("Action: Cancel", screen.frames[0])
        apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before + CONTROL_IGNORE_BLOCK.encode())
        self.assertEqual(self.git("rev-parse", "HEAD"), offer.base_commit_id)
        # Applying never authorizes the selected immutable base.
        self.assertEqual(self.offer().base_commit_id, offer.base_commit_id)

    def test_repair_modal_instructions_disclose_all_rules_in_one_bounded_frame(self):
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        screen = BoundedPrerequisiteScreen([-997, 10, 27])
        self.assertEqual(
            _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer), "cancel",
        )
        instructions = screen.frames[-1].split("Instructions: add", 1)[1]
        for rule in offer.rules:
            self.assertIn(rule, instructions)
        self.assertIn("select a containing commit", instructions)
        self.assertIn(offer.base_commit_id, instructions)
        self.assertIn("remains unauthorized", instructions)
        self.assertEqual(path.read_bytes(), before)

    def test_repair_modal_default_cancel_and_legacy_singleton_stay_narrow(self):
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + "/.asha/result.json\n/.asha/outbox/\n")
        self.commit("only legacy marker missing")
        offer = self.offer()
        self.assertEqual(offer.rules, (CONTROL_IGNORE_RULE,))
        before = path.read_bytes()
        for keys, expected in (([10], "cancel"), ([-997, -997, 10], "apply"),
                               ([-997, 10, 27], "cancel")):
            with self.subTest(keys=keys):
                screen = BoundedPrerequisiteScreen(keys)
                self.assertEqual(
                    _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer),
                    expected,
                )
                self.assertIn(CONTROL_IGNORE_RULE, screen.frames[-1])
                self.assertNotIn("/.asha/result.json", screen.frames[-1])
                self.assertNotIn("/.asha/outbox/", screen.frames[-1])
                self.assertEqual(path.read_bytes(), before)

    def test_repair_modal_refuses_apply_when_disclosure_is_clipped(self):
        offer = self.offer()
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        for size in ((0, 0), (1, 1), (8, 120), (12, 40), (80, 20)):
            for instructions in (False, True):
                with self.subTest(size=size, instructions=instructions):
                    keys = [-997, 10, -997, 10, 27] if instructions else [-997, -997, 10, 27]
                    screen = BoundedPrerequisiteScreen(keys, size=size)
                    self.assertEqual(
                        _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer),
                        "cancel",
                    )
                    self.assertEqual(path.read_bytes(), before)

    def test_repair_modal_resize_rechecks_visibility_before_apply(self):
        offer = self.offer()
        for initial, resized, expected in (
            ((8, 80), (24, 120), "apply"),
            ((24, 120), (8, 80), "cancel"),
        ):
            with self.subTest(initial=initial):
                screen = BoundedPrerequisiteScreen(
                    [-997, -997, FakeCurses.KEY_RESIZE, 10, 27],
                    size=initial, resized=resized,
                )
                self.assertEqual(
                    _prerequisite_action_modal(screen, FakeCurses(), TuiModel([]), offer),
                    expected,
                )
                if expected == "apply":
                    for rule in offer.rules:
                        self.assertIn(f"Add: {rule}", screen.frames[-1])
                else:
                    self.assertIn("Apply is unavailable", screen.frames[-1])

    def test_each_nested_negation_refuses_before_any_root_write(self):
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        nested = self.repository / ".asha/.gitignore"
        for negation in ("!control-task.json", "!result.json", "!outbox/"):
            with self.subTest(negation=negation):
                nested.write_text(negation + "\n")
                nested.chmod(0o644)
                offer = self.offer()
                with self.assertRaisesRegex(ValueError, "nested"):
                    apply_ignore_prerequisite(self.config, offer)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_global_excludes_never_authorize_selected_or_working_policy(self):
        global_ignore = self.root / "global-ignore"
        global_ignore.write_text(CONTROL_IGNORE_BLOCK)
        self.git("config", "core.excludesFile", str(global_ignore))
        offer = self.offer()
        self.assertFalse(offer.already_covered)
        self.assertEqual(offer.rules, CONTROL_IGNORE_RULES)
        apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(self.offer().base_commit_id, offer.base_commit_id)

    def test_root_negations_are_preserved_then_overridden_only_for_private_paths(self):
        path = self.repository / ".gitignore"
        before = path.read_bytes() + CONTROL_IGNORE_BLOCK.encode() + b"# user overrides\n" + b"".join(
            ("!" + rule + "\n").encode() for rule in CONTROL_IGNORE_RULES
        )
        path.write_bytes(before)
        apply_ignore_prerequisite(self.config, self.offer())
        self.assertEqual(path.read_bytes(), before + CONTROL_IGNORE_BLOCK.encode())
        for rule in CONTROL_IGNORE_RULES:
            checked = subprocess.run(["git", "-C", str(self.repository), "check-ignore",
                                      "--no-index", "--quiet", "--", rule[1:]])
            self.assertEqual(checked.returncode, 0, rule)
        self.assertTrue(self.offer().already_covered)
        repaired = path.read_bytes()
        apply_ignore_prerequisite(self.config, self.offer())
        self.assertEqual(path.read_bytes(), repaired)

    def test_new_nested_negation_after_temporary_write_refuses_before_replacement(self):
        import lib.control.prerequisites as prerequisites
        path = self.repository / ".gitignore"
        before = path.read_bytes()
        offer = self.offer()
        create = prerequisites._create_temporary_at

        def race(*args):
            result = create(*args)
            nested = self.repository / ".asha/.gitignore"
            nested.write_text("!result.json\n")
            nested.chmod(0o644)
            return result

        with mock.patch("lib.control.prerequisites._create_temporary_at", side_effect=race):
            with self.assertRaisesRegex(ValueError, "working ignore policy changed"):
                apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.repository.glob(".gitignore.asha-control.*")), [])

    def test_tracked_private_transport_refuses_without_provider_launch(self):
        for relative in (".asha/result.json", ".asha/outbox/candidate.json"):
            with self.subTest(relative=relative):
                path = self.repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
                old = self.commit("tracked private transport")
                with mock.patch("lib.control.cli.launch_task") as launch:
                    status, stdout, stderr = self._run_private_cli(old)
                    self.assertEqual(status, 2, stdout + stderr)
                    self.assertIn("tracks a controller-private", stderr)
                    launch.assert_not_called()
                self.assertFalse((self.repository / ".jj").exists())
                self.assertFalse(CreationJournalStore(self.config).path(TASK_ID).exists())
                path.unlink()
                self.commit("remove tracked transport fixture")

    def _start_and_stage_fake_result(self, selected: str):
        from lib.control.orchestration.cli import task_main
        from lib.control.orchestration.ingestion import result_ingestion_id
        from tests.python.orchestration_execution_fixtures import now_text
        before = {p: ((self.repository / p).read_bytes(), (self.repository / p).stat().st_mode)
                  for p in (".gitignore", ".asha/config.json", "Memory/activeContext.md", "Memory/decisions.md")}
        receipts = []

        def fake_launch(_config, prepared, **kwargs):
            self.assertEqual(kwargs["harness"], "codex")
            workspace = Path(prepared["jj"]["workspace_path"])
            # The fake provider observes the real launch boundary. It neither
            # makes the transport directory nor repairs any permission.
            output = subprocess.check_output([
                "/bin/python3", "-c",
                "import os,json,stat; from pathlib import Path; "
                "print(json.dumps({'umask':os.umask(2),'modes':{p:stat.S_IMODE(Path(p).stat().st_mode) "
                "for p in ['.asha','.asha/outbox']}}))",
            ], cwd=workspace, text=True)
            self.assertEqual(json.loads(output), {
                "umask": 0o002, "modes": {".asha": 0o700, ".asha/outbox": 0o700},
            })
            selected_bytes = {p: ((workspace / p).read_bytes(), (workspace / p).stat().st_mode)
                              for p in before}
            attempt, run = str(uuid.uuid4()), str(uuid.uuid4())
            ingestion = result_ingestion_id(attempt)
            outbox = workspace / ".asha/outbox" / f"{ingestion}.json"
            body = {
                "contract": "asha.orchestration-result.v1",
                "publication_id": str(uuid.uuid4()), "supersedes_result_id": None,
                "initiative_id": str(uuid.uuid4()), "node_id": "implementation-a",
                "attempt_id": attempt, "task_id": TASK_ID, "run_id": run,
                "claim_status": "completed", "summary": "initialized private transport",
                "files_changed": [], "verification_attestations": [], "concerns": [],
                "follow_up": [], "published_at": now_text(),
            }
            result_file = workspace / ".asha/result.json"
            fd = os.open(result_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(body, stream)
            env = {**self.env, "ASHA_CONTROL_MANAGED": "1", "ASHA_CONTROL_TASK_ID": TASK_ID,
                   "ASHA_CONTROL_RUN_ID": run, "ASHA_CONTROL_RESULT_INGESTION_ID": ingestion,
                   "ASHA_CONTROL_RESULT_OUTBOX": str(outbox), "TMUX_PANE": "%71"}
            tmux = mock.Mock()
            tmux.pane_facts.return_value = SimpleNamespace(
                dead=False, pane_pid=os.getpid(), session="fake-codex",
            )
            tmux.session_option.side_effect = lambda session, key: {
                "@asha_managed": "1", "@asha_task_id": TASK_ID,
            }[key]
            tmux.pane_option.side_effect = lambda pane, key: {
                "@asha_run_id": run, "@asha_result_ingestion": ingestion,
                "@asha_result_outbox_digest": hashlib.sha256(str(outbox).encode()).hexdigest(),
            }[key]
            with mock.patch("lib.control.orchestration.ingestion.TmuxAdapter", return_value=tmux), \
                    mock.patch("lib.control.orchestration.ingestion.caller_descends_from", return_value=True):
                for _ in range(2):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(task_main(["report", "--file", str(result_file), "--json"], env=env), 0)
                    receipts.append(json.loads(output.getvalue()))
            self.assertEqual(receipts[0], receipts[1])
            self.assertEqual(receipts[0]["phase"], "staged")
            self.assertEqual(json.loads(outbox.read_text())["body"], body)
            self.assertEqual(stat.S_IMODE(outbox.stat().st_mode), 0o600)
            self.assertEqual(selected_bytes, {p: ((workspace / p).read_bytes(), (workspace / p).stat().st_mode)
                                              for p in before})
            journal = CreationJournalStore(self.config).read(TASK_ID)
            self.assertEqual(journal["phase"], "ready-for-launch")
            self.assertEqual(journal["planned_context"][".asha/outbox"]["mode"], 0o700)
            self.assertIn(".asha/outbox", journal["context_owned"])
            return prepared

        with mock.patch("lib.control.cli.launch_task", side_effect=fake_launch) as launch, \
                mock.patch("lib.control.cli._emit_start_result", return_value=0):
            status, stdout, stderr = self._run_private_cli(selected)
            self.assertEqual(status, 0, stdout + stderr)
            launch.assert_called_once()
        self.assertEqual(before, {p: ((self.repository / p).read_bytes(), (self.repository / p).stat().st_mode)
                                  for p in before})
        self.assertEqual(len(receipts), 2)

    def test_fresh_initialize_commit_prepare_and_private_staging(self):
        from memory_v2 import initialize
        # Start from an actually empty project, not the hand-authored legacy
        # config/publications used by the prerequisite-refusal fixtures.
        self.repository = self.root / "fresh-repository"
        self.repository.mkdir(mode=0o700)
        self.git("init", "-q", "-b", "master")
        initialize(self.repository)
        selected = self.commit("canonical initialization")
        self._start_and_stage_fake_result(selected)

    def test_legacy_init_offer_apply_old_base_refusal_then_new_base_staging(self):
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() +
                        "# Asha Control private context (managed)\n/.asha/control-task.json\n")
        old = self.commit("legacy initialized base")
        self.request = replace(self.request, requested_base=old)
        offer = decode_worker_refusal(encode_worker_refusal(self.offer(), TASK_ID), TASK_ID)
        before = path.read_bytes()
        apply_ignore_prerequisite(self.config, offer)
        self.assertEqual(path.read_bytes(), before + b"/.asha/result.json\n/.asha/outbox/\n")
        with mock.patch("lib.control.cli.launch_task") as launch:
            status, stdout, stderr = self._run_private_cli(old)
            self.assertEqual(status, 2, stdout + stderr)
            refused = decode_worker_refusal(stdout.encode(), TASK_ID)
            self.assertEqual(refused.base_commit_id, old)
            self.assertEqual(refused.rules, offer.rules)
            launch.assert_not_called()
        self.assertFalse((self.repository / ".jj").exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), old)
        selected = self.commit("explicitly commit producer repair")
        self._start_and_stage_fake_result(selected)


class DefaultContextDoctorTests(PrerequisiteRepository, unittest.TestCase):
    def test_doctor_names_default_ref_oid_and_is_read_only(self) -> None:
        before_git = (self.repository / ".git").stat().st_mtime_ns
        with mock.patch("pathlib.Path.cwd", return_value=self.repository):
            probe = _default_context_probe(self.config)
        self.assertEqual(probe.outcome, "mismatch")
        self.assertIn("refs/heads/master", probe.detail)
        self.assertIn(self.git("rev-parse", "HEAD"), probe.detail)
        self.assertIn("/session:init", probe.detail)
        self.assertFalse((self.repository / ".jj").exists())
        self.assertEqual((self.repository / ".git").stat().st_mtime_ns, before_git)

        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + CONTROL_IGNORE_BLOCK, encoding="utf-8")
        os.chmod(path, 0o644)
        committed = self.commit("control readiness")
        with mock.patch("pathlib.Path.cwd", return_value=self.repository):
            ready = _default_context_probe(self.config)
        self.assertEqual(ready.outcome, "match")
        self.assertIn(committed, ready.detail)
        self.assertIn("resolved default only", ready.detail)


if __name__ == "__main__":
    unittest.main()
