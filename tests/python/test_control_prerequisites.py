from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.cli import _parse_start, _start_new_task, main as control_main
from lib.control.doctor import _default_context_probe
from lib.control.jj import (
    ContextCompatibilityError, DefaultBaseResolution, JjAdapter, JjError,
    MaterializationPlan,
)
from lib.control.prerequisites import (
    CONTROL_IGNORE_RULE, StartPrerequisiteRefusal,
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
    _TuiShutdown, _prerequisite_action_modal, _start_form, run_tui,
)
from tests.python.test_control_task_start_smoke_fixes import FakeCurses, ProgressScreen


TASK_ID = "12345678-1234-4234-8234-123456789abc"
PROJECT_ID = "12345678-9abc-4def-8123-456789abcdef"


class PrerequisiteRepository:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
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
            (".asha/control-task.json",),
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
        path.write_text(path.read_text() + CONTROL_IGNORE_RULE + "\n", encoding="utf-8")
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
        self.assertEqual(path.read_text(),
                         "# Asha Control private context (managed)\n"
                         "/.asha/control-task.json\n")

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

    def test_apply_is_noop_when_worktree_already_covers_marker(self) -> None:
        offer = self.offer()
        path = self.repository / ".gitignore"
        path.write_text(path.read_text() + CONTROL_IGNORE_RULE + "\n", encoding="utf-8")
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
        path.write_text(path.read_text() + CONTROL_IGNORE_RULE + "\n", encoding="utf-8")
        os.chmod(path, 0o644)
        committed = self.commit("control readiness")
        with mock.patch("pathlib.Path.cwd", return_value=self.repository):
            ready = _default_context_probe(self.config)
        self.assertEqual(ready.outcome, "match")
        self.assertIn(committed, ready.detail)
        self.assertIn("resolved default only", ready.detail)


if __name__ == "__main__":
    unittest.main()
