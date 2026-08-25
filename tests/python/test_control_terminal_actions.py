from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.model import retry_task_slug
from lib.control.prune import assemble_prune_context, prune_one_task
from lib.control.prune import ArtifactOutcome, PruneContext, PruneResult
from lib.control.store import TaskStore, task_digest
from lib.control.transaction import CreationJournalStore
from lib.control import tui
from tests.python.test_control_config_model import task_record


class TerminalTaskActionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        root.chmod(0o700)
        home = root / "home"
        home.mkdir()
        home.chmod(0o700)
        self.env = {
            "HOME": str(home),
            "ASHA_CONFIG": str(root / "missing.json"),
            "ASHA_HOME": str(root / "asha"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        self.config = load_config(self.env)
        self.tasks = TaskStore(self.config)
        self.journals = CreationJournalStore(self.config)

    def record(self, slug: str) -> dict:
        value = task_record(
            slug=slug,
            repository_root=str(self.config.home / f"source-{slug}"),
            workspace_path=str(self.config.workspace_root / "repo" / slug),
        )
        return value

    def test_active_load_never_reads_archived_and_all_uses_lifecycle_projection(self) -> None:
        active = self.record("active")
        archived = copy.deepcopy(self.record("archived"))
        archived["task_id"] = "22222222-2222-4222-8222-222222222222"
        archived["lifecycle"] = "archived"
        archived["runs"][0]["state"] = "exited"
        self.tasks.save(active)
        self.tasks.save(archived)
        active_row = tui.TuiRow.from_records(
            active,
            {
                "contract": "asha.control-reconciliation.v1",
                "task_id": active["task_id"], "state": "working",
                "blocker": None, "evidence": [], "runs": [],
            },
            tui.StateObservation(
                "working", active["runs"][0]["run_id"], "test", None,
                "fresh", "active",
            ),
        )
        with mock.patch.object(tui, "_read_row", return_value=active_row) as read, \
                mock.patch.object(tui.view, "publish_server_summary"):
            active_rows = tui._load_rows(
                self.config, self.tasks, self.journals, mock.Mock(),
            )
            all_rows = tui._load_rows(
                self.config, self.tasks, self.journals, mock.Mock(),
                include_archived=True,
            )

        self.assertEqual([item.task["slug"] for item in active_rows], ["active"])
        self.assertEqual({item.task["slug"] for item in all_rows}, {"active", "archived"})
        self.assertEqual(
            next(item for item in all_rows if item.task["slug"] == "archived").display_state,
            "archived",
        )
        self.assertEqual([call.args[3]["slug"] for call in read.call_args_list], ["active", "active"])

    def test_stale_active_snapshot_is_rechecked_under_lock_before_live_reconciliation(self) -> None:
        stale = self.record("archive-race-load")
        stale["lifecycle"] = "ended"
        stale["runs"][0]["state"] = "exited"
        self.tasks.save(stale)
        archived = copy.deepcopy(stale)
        archived["lifecycle"] = "archived"
        self.tasks.save(archived, expected_digest=task_digest(stale))

        with mock.patch.object(self.tasks, "list", return_value=[stale]), \
                mock.patch.object(
                    tui.view, "reconcile_with_creation_observation",
                    side_effect=AssertionError("archived task reached live adapters"),
                ) as reconcile, \
                mock.patch.object(tui.view, "publish_server_summary") as publish:
            active = tui._load_rows(
                self.config, self.tasks, self.journals, mock.Mock(),
            )
            history = tui._load_rows(
                self.config, self.tasks, self.journals, mock.Mock(),
                include_archived=True,
            )

        self.assertEqual(active, [])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].display_state, "archived")
        reconcile.assert_not_called()
        publish.assert_called()

    def test_direct_refresh_rechecks_current_archived_lifecycle_under_task_lock(self) -> None:
        stale = self.record("archive-race-refresh")
        stale["lifecycle"] = "ended"
        stale["runs"][0]["state"] = "exited"
        self.tasks.save(stale)
        row = tui.TuiRow.from_records(stale, {
            "contract": "asha.control-reconciliation.v1",
            "task_id": stale["task_id"], "state": "ended",
            "blocker": None, "evidence": [], "runs": [],
        })
        archived = copy.deepcopy(stale)
        archived["lifecycle"] = "archived"
        self.tasks.save(archived, expected_digest=task_digest(stale))
        model = tui.TuiModel([row])

        with mock.patch.object(
            tui.view, "reconcile_with_creation_observation",
            side_effect=AssertionError("archived task reached live adapters"),
        ) as reconcile:
            self.execute(
                model,
                tui.TuiIntent(tui.IntentKind.RECONCILE, stale["task_id"]),
            )

        reconcile.assert_not_called()
        self.assertEqual(model.selected_row.display_state, "archived")
        self.assertEqual(
            model.message,
            "archived lifecycle refreshed; live resources were not reconciled",
        )

    def test_retry_slug_is_bounded_deterministic_and_arguments_reconstruct_request(self) -> None:
        task = task_record(slug="s" * 64)
        task["label"] = "exact joined label"
        task["source"] = {"kind": "pr", "number": 42, "url": "https://example/pr/42"}
        task["jj"]["requested_base"] = "main@origin"
        task_id = "87654321-4321-4321-8321-cba987654321"

        slug = retry_task_slug(task["slug"], task_id)
        arguments = tui._retry_arguments(task, task_id)

        self.assertLessEqual(len(slug), 64)
        self.assertEqual(slug, retry_task_slug(task["slug"], task_id))
        self.assertTrue(
            slug.endswith("-retry-87654321432143218321cba987654321"),
        )
        self.assertEqual(arguments, [
            "--repo", task["repository"]["root"],
            "--pr", "42",
            "--harness", task["runs"][0]["harness"],
            "--role", task["runs"][0]["role"],
            "--goal", "exact joined label", "--slug", slug,
            "--task-id", task_id, "--detach", "--json",
        ])

    def test_retry_slug_encodes_the_complete_uuid_and_never_aliases_an_id8_peer(self) -> None:
        first = "12345678-0000-4000-8000-000000000001"
        second = "12345678-ffff-4fff-bfff-ffffffffffff"

        first_slug = retry_task_slug("old", first)
        second_slug = retry_task_slug("old", second)

        self.assertNotEqual(first_slug, second_slug)
        self.assertTrue(first_slug.endswith(first.replace("-", "")))
        self.assertTrue(second_slug.endswith(second.replace("-", "")))
        self.assertLessEqual(max(len(first_slug), len(second_slug)), 64)

    def test_shared_prune_assembly_is_reused_by_one_task_controller(self) -> None:
        task = self.record("prune")
        task["lifecycle"] = "archived"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        with mock.patch("lib.control.prune.orchestration_bindings", return_value={}) as bindings, \
                mock.patch("lib.control.prune.workspace_path_owners", return_value={}) as owners, \
                mock.patch("lib.control.prune.prune_task") as controller:
            controller.return_value = mock.sentinel.result
            context = assemble_prune_context(
                self.config, tasks=self.tasks, env=self.env,
            )
            result = prune_one_task(
                self.config, task, tasks=self.tasks, journals=self.journals,
                jj=mock.Mock(), context=context, dry_run=True,
            )

        self.assertIs(result, mock.sentinel.result)
        bindings.assert_called_once_with(dict(self.env))
        owners.assert_called_once_with(self.tasks)
        kwargs = controller.call_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertIs(kwargs["records"], context.records)
        self.assertIs(kwargs["path_owners"], context.path_owners)

    def execute(self, model: tui.TuiModel, intent: tui.TuiIntent) -> bool:
        return tui._execute_intent(
            intent, stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
            config=self.config, env=self.env, store=self.tasks,
            journals=self.journals, jj=mock.Mock(),
        )

    def test_context_retry_uses_supervised_worker_and_selects_fresh_task(self) -> None:
        old = self.record("old-task")
        old["lifecycle"] = "ended"
        old["runs"][0]["state"] = "exited"
        old["source"] = {
            "kind": "pr", "number": 42, "url": "https://example.test/pr/42",
        }
        self.tasks.save(old)
        old_row = tui.lifecycle_row({**old, "lifecycle": "archived"})
        old_row.task["lifecycle"] = "ended"
        old_row = tui.TuiRow.from_records(
            old, {**old_row.reconciliation, "state": "ended"},
            tui.StateObservation("ended", old["runs"][0]["run_id"], "test", None, "fresh", "ended"),
        )
        model = tui.TuiModel([old_row], include_archived=True)
        fresh_id = "87654321-4321-4321-8321-cba987654321"
        fresh = copy.deepcopy(old)
        fresh["task_id"] = fresh_id
        fresh["slug"] = retry_task_slug(old["slug"], fresh_id)
        fresh["lifecycle"] = "archived"
        fresh_row = tui.lifecycle_row(fresh)

        with mock.patch.object(tui, "_prompt_line", side_effect=["r", "yes"]) as prompt, \
                mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "new_uuid", return_value=fresh_id), \
                mock.patch.object(tui, "_supervise_start_process", return_value="task started") as supervise, \
                mock.patch.object(tui, "_source_colocation_watch", return_value=(None, False)), \
                mock.patch.object(tui, "_load_rows", return_value=[old_row, fresh_row]):
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, old["task_id"]))

        self.assertIn("current PR head", prompt.call_args_list[1].kwargs["context"])
        argv = supervise.call_args.args[4]
        self.assertIn("--slug", argv)
        self.assertIn(fresh["slug"], argv)
        self.assertIn(old["label"], argv)
        self.assertEqual(model.selected_row.task["task_id"], fresh_id)
        self.assertEqual(old["lifecycle"], self.tasks.read(old["task_id"])["lifecycle"])

    def test_context_archive_then_prune_are_distinct_exact_confirmed_controller_calls(self) -> None:
        task = self.record("terminal")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        terminal_row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1", "task_id": task["task_id"],
             "state": "ended", "blocker": None, "evidence": [],
             "runs": [{"run_id": task["runs"][0]["run_id"], "state": "exited", "blocker": None, "evidence": []}]},
            tui.StateObservation("ended", task["runs"][0]["run_id"], "test", None, "fresh", "ended"),
        )
        model = tui.TuiModel([terminal_row], include_archived=True)
        archived = copy.deepcopy(task)
        archived["lifecycle"] = "archived"
        archived_row = tui.lifecycle_row(archived)
        preview = PruneResult(
            task["task_id"], task["slug"], "planned",
            ArtifactOutcome("would-kill", "dead owned session"),
            ArtifactOutcome("would-remove", "owned workspace"),
        )
        result = PruneResult(
            task["task_id"], task["slug"], "pruned",
            ArtifactOutcome("killed", "dead owned session"),
            ArtifactOutcome("removed", "owned workspace"),
        )
        contexts = [mock.Mock(spec=PruneContext), mock.Mock(spec=PruneContext)]
        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_prompt_line", side_effect=["a", "yes"]), \
                mock.patch.object(tui, "archive_task", return_value=archived) as archive, \
                mock.patch.object(tui, "_load_rows", return_value=[archived_row]):
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        archive.assert_called_once()
        self.assertEqual(model.selected_row.display_state, "archived")
        self.tasks.save(archived, expected_digest=task_digest(task))

        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_archived_resources_remain", return_value=True), \
                mock.patch.object(tui, "_prompt_line", side_effect=["p", "yes"]), \
                mock.patch.object(tui, "assemble_prune_context", side_effect=contexts) as assemble, \
                mock.patch.object(tui, "prune_one_task", side_effect=[preview, result]) as prune, \
                mock.patch.object(tui, "_load_rows", return_value=[archived_row]):
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        self.assertEqual(assemble.call_count, 2)
        self.assertEqual([call.kwargs["dry_run"] for call in prune.call_args_list], [True, False])
        self.assertIn("pruned", model.message)
        self.assertEqual(model.selected_row.task["task_id"], task["task_id"])

    def test_owned_context_hides_retry_and_reconciles_without_dispatch(self) -> None:
        task = self.record("owned")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1", "task_id": task["task_id"],
             "state": "ended", "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("ended", task["runs"][0]["run_id"], "test", None, "fresh", "ended"),
        )
        model = tui.TuiModel([row])
        initiative_store = mock.Mock()
        initiative = {"initiative_id": "22222222-2222-4222-8222-222222222222", "slug": "initiative"}
        link = {"node_id": "node-a", "attempt_id": "33333333-3333-4333-8333-333333333333"}
        attempt = {"state": "reported"}
        binding = (initiative_store, initiative, link, attempt, {"state": "evaluating"}, task)
        with mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch.object(tui, "_prompt_line", return_value="c") as prompt, \
                mock.patch.object(tui, "_reconcile_task_initiative", return_value="initiative reconciled") as reconcile:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))

        menu = prompt.call_args.kwargs["context"]
        self.assertIn("reconcile", menu)
        self.assertNotIn("retry", menu)
        reconcile.assert_called_once()
        self.assertEqual(model.message, "initiative reconciled")

    def test_initiative_reconcile_reports_binding_and_allocated_ready_outcomes(self) -> None:
        task = self.record("owned-report")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        initiative_store = mock.Mock()
        initiative_store.read_node.return_value = {"state": "ready"}
        initiative = {
            "initiative_id": "22222222-2222-4222-8222-222222222222",
            "slug": "initiative-report",
        }
        link = {
            "node_id": "node-a",
            "attempt_id": "33333333-3333-4333-8333-333333333333",
        }
        binding = (
            initiative_store, initiative, link, {"state": "reported"},
            {"state": "evaluating"}, task,
        )
        payload = {
            "live_reconciliation": {"retries": [{"state": "allocated"}]},
            "results": [],
        }
        with mock.patch.object(tui, "_lookup_task_binding", return_value=binding), \
                mock.patch(
                    "lib.control.orchestration.cli.reconcile_one_initiative",
                    return_value=payload,
                ) as reconcile:
            message = tui._reconcile_task_initiative(
                env=self.env, store=self.tasks, task_id=task["task_id"],
            )
        reconcile.assert_called_once_with(initiative_store, initiative["initiative_id"])
        self.assertIn("initiative-report", message)
        self.assertIn("node node-a", message)
        self.assertIn(link["attempt_id"], message)
        self.assertIn("allocated 1", message)
        self.assertIn("ready 1", message)
        self.assertIn("action evidence: no pending action records", message)
        self.assertNotIn("nothing dispatched", message)

    def test_failed_runless_task_has_archive_shortcut(self) -> None:
        task = self.record("runless")
        task["lifecycle"] = "failed"
        task["runs"] = []
        task["jj"]["change_id"] = None
        task["jj"]["working_commit_id"] = None
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1", "task_id": task["task_id"],
             "state": "failed", "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("failed", None, "test", None, "fresh", "failed creation"),
        )
        self.assertIs(tui.TuiModel([row]).dispatch_key("a").kind, tui.IntentKind.ARCHIVE)

    def test_runless_archived_task_does_not_offer_an_unreconstructable_retry(self) -> None:
        task = self.record("runless-history")
        task["lifecycle"] = "archived"
        task["runs"] = []
        task["jj"]["change_id"] = None
        task["jj"]["working_commit_id"] = None
        self.tasks.save(task)
        model = tui.TuiModel([tui.lifecycle_row(task)], include_archived=True)
        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_archived_resources_remain", return_value=False), \
                mock.patch.object(tui, "_prompt_line", return_value=None) as prompt:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        menu = prompt.call_args.kwargs["context"]
        self.assertNotIn("retry", menu)
        self.assertIn("prune", menu)

    def test_scope_toggle_reloads_archived_and_preserves_selected_row(self) -> None:
        active = self.record("active-scope")
        archived = self.record("archived-scope")
        archived["task_id"] = "22222222-2222-4222-8222-222222222222"
        archived["lifecycle"] = "archived"
        archived["runs"][0]["state"] = "exited"
        active_row = tui.TuiRow.from_records(
            active,
            {"contract": "asha.control-reconciliation.v1", "task_id": active["task_id"],
             "state": "working", "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("working", active["runs"][0]["run_id"], "test", None, "fresh", "working"),
        )
        model = tui.TuiModel([active_row])
        with mock.patch.object(
            tui, "_load_rows", return_value=[active_row, tui.lifecycle_row(archived)],
        ) as load:
            self.execute(model, tui.TuiIntent(tui.IntentKind.TOGGLE_SCOPE))

        self.assertTrue(model.include_archived)
        self.assertEqual(model.selected_row.task["task_id"], active["task_id"])
        self.assertEqual(load.call_args.kwargs["include_archived"], True)
        self.assertEqual(
            next(item for item in model.rows if item.task["task_id"] == archived["task_id"]).display_state,
            "archived",
        )

    def test_explicit_refresh_keeps_archived_lifecycle_without_live_reconciliation(self) -> None:
        task = self.record("archived-refresh")
        task["lifecycle"] = "archived"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        model = tui.TuiModel([tui.lifecycle_row(task)], include_archived=True)
        with mock.patch.object(
            tui.view, "reconcile_with_creation_observation",
            side_effect=AssertionError("archived task must not reconcile live resources"),
        ):
            self.execute(model, tui.TuiIntent(tui.IntentKind.RECONCILE, task["task_id"]))
        self.assertEqual(model.selected_row.display_state, "archived")
        self.assertIn("archived", model.message)

    def test_context_refuses_ambiguous_ownership_before_showing_actions(self) -> None:
        task = self.record("ambiguous")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1", "task_id": task["task_id"],
             "state": "ended", "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("ended", task["runs"][0]["run_id"], "test", None, "fresh", "ended"),
        )
        model = tui.TuiModel([row])
        with mock.patch.object(
            tui, "_lookup_task_binding",
            side_effect=ValueError("Control task is linked to more than one initiative"),
        ), mock.patch.object(tui, "_prompt_line") as prompt:
            with self.assertRaisesRegex(ValueError, "more than one"):
                self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        prompt.assert_not_called()

    def test_runless_retained_inspect_surfaces_adoption_guidance_without_popup(self) -> None:
        task = self.record("retained-guidance")
        task["lifecycle"] = "failed"
        task["runs"] = []
        self.tasks.save(task)
        row = tui.TuiRow.from_records(task, {
            "contract": "asha.control-reconciliation.v1",
            "task_id": task["task_id"], "state": "failed",
            "blocker": "retained", "evidence": [], "runs": [],
        })
        model = tui.TuiModel([row])
        guidance = (
            "retained creation is eligible for explicit authenticated "
            "forward-adoption; run: asha task recover candidate --adopt --yes"
        )

        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_prompt_line", return_value="i"), \
                mock.patch.object(self.journals, "read", return_value={"phase": "preserved"}), \
                mock.patch.object(
                    tui, "retained_recovery_guidance", return_value=guidance,
                ) as classify, \
                mock.patch.object(
                    tui, "_open_popup",
                    side_effect=AssertionError("retained runless task must not open a popup"),
                ) as popup:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))

        self.assertEqual(model.message, guidance)
        classify.assert_called_once_with(task, {"phase": "preserved"})
        popup.assert_not_called()

    def test_real_nonblocking_lookup_treats_absent_orchestration_registry_as_unowned(self) -> None:
        task = self.record("plain-control")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        self.assertIsNone(
            tui._lookup_task_binding(self.env, task["task_id"], self.tasks),
        )

    def test_fully_pruned_archived_context_keeps_row_and_hides_inspect(self) -> None:
        task = self.record("pruned-history")
        task["lifecycle"] = "archived"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        model = tui.TuiModel([tui.lifecycle_row(task)], include_archived=True)
        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_archived_resources_remain", return_value=False), \
                mock.patch.object(tui, "_prompt_line", return_value=None) as prompt:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        menu = prompt.call_args.kwargs["context"]
        self.assertNotIn("inspect", menu)
        self.assertIn("retry", menu)
        self.assertIn("prune", menu)
        self.assertEqual(model.selected_row.display_state, "archived")

    def test_archived_inspect_requires_a_retained_owned_session(self) -> None:
        task = self.record("no-session")
        task["lifecycle"] = "archived"
        task["runs"][0]["state"] = "exited"
        adapter = mock.Mock()
        adapter.has_session.return_value = False
        with mock.patch.object(tui, "_adapter_for_task", return_value=adapter):
            self.assertFalse(tui._archived_resources_remain(self.config, task))
        adapter.has_session.assert_called_once_with(task["tmux"]["session"])

    def test_archive_retry_and_prune_require_exact_lowercase_yes(self) -> None:
        task = self.record("exact-confirmation")
        task["lifecycle"] = "ended"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        row = tui.TuiRow.from_records(
            task,
            {"contract": "asha.control-reconciliation.v1", "task_id": task["task_id"],
             "state": "ended", "blocker": None, "evidence": [], "runs": []},
            tui.StateObservation("ended", task["runs"][0]["run_id"], "test", None, "fresh", "ended"),
        )
        model = tui.TuiModel([row], include_archived=True)
        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_prompt_line", side_effect=["r", "y"]) as retry_prompt, \
                mock.patch.object(tui, "_supervise_start_process") as supervise:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        supervise.assert_not_called()
        self.assertGreaterEqual(retry_prompt.call_args_list[1].kwargs["maximum"], 4)
        self.assertEqual(model.message, "retry cancelled")

        with mock.patch.object(tui, "_prompt_line", return_value="YES") as archive_prompt, \
                mock.patch.object(tui, "archive_task") as archive:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ARCHIVE, task["task_id"]))
        archive.assert_not_called()
        self.assertGreaterEqual(archive_prompt.call_args.kwargs["maximum"], 4)
        self.assertEqual(model.message, "archive cancelled")

        archived = copy.deepcopy(task)
        archived["lifecycle"] = "archived"
        self.tasks.save(archived, expected_digest=task_digest(task))
        model.replace_rows([tui.lifecycle_row(archived)])
        preview = PruneResult(
            task["task_id"], task["slug"], "planned",
            ArtifactOutcome("would-kill", "dead session"),
            ArtifactOutcome("would-remove", "owned workspace"),
        )
        with mock.patch.object(tui, "_lookup_task_binding", return_value=None), \
                mock.patch.object(tui, "_archived_resources_remain", return_value=True), \
                mock.patch.object(tui, "_prompt_line", side_effect=["p", "y"]) as prune_prompt, \
                mock.patch.object(tui, "assemble_prune_context"), \
                mock.patch.object(tui, "prune_one_task", return_value=preview) as prune:
            self.execute(model, tui.TuiIntent(tui.IntentKind.ACTIONS, task["task_id"]))
        self.assertEqual(prune.call_count, 1)
        self.assertGreaterEqual(prune_prompt.call_args_list[1].kwargs["maximum"], 4)
        self.assertEqual(model.message, "prune cancelled")

    def test_real_driver_surfaces_every_unreadable_prune_record_class(self) -> None:
        task = self.record("bad-prune-record")
        task["lifecycle"] = "archived"
        task["runs"][0]["state"] = "exited"
        self.tasks.save(task)
        record_path = tui.PruneRecordStore(self.config).path(task["task_id"])
        record_path.parent.mkdir(mode=0o700, parents=True)

        class Screen:
            def __init__(self) -> None:
                self.keys = [ord("x"), ord("q")]

            def timeout(self, _value) -> None:
                pass

            def getch(self) -> int:
                return self.keys.pop(0)

        class Curses:
            KEY_RESIZE = -10
            KEY_UP = -11
            KEY_DOWN = -12
            KEY_ENTER = -13

        cases = {
            "malformed": lambda path: path.write_bytes(b"{"),
            "oversized": lambda path: path.write_bytes(b"x" * (70 * 1024)),
            "nonregular": lambda path: path.mkdir(),
            "unreadable": lambda path: path.symlink_to(path.with_name("missing")),
        }
        for name, create in cases.items():
            with self.subTest(name=name):
                if record_path.is_symlink() or record_path.is_file():
                    record_path.unlink()
                elif record_path.is_dir():
                    record_path.rmdir()
                create(record_path)
                model = tui.TuiModel([tui.lifecycle_row(task)], include_archived=True)
                with mock.patch.object(tui, "_paint"):
                    status = tui._curses_loop(
                        Screen(), Curses(), model, self.config, self.env,
                        self.tasks, self.journals, mock.Mock(),
                    )
                self.assertEqual(status, 0)
                self.assertIsNotNone(model.message)
                self.assertIn("prune record", model.message)
                self.assertLessEqual(len(model.message), 1200)


if __name__ == "__main__":
    unittest.main()
