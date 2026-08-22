from __future__ import annotations

import copy
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from lib.control.jj import (
    DEFAULT_BASE_REVSET, DefaultBaseResolution, ImmutableTree, JjAdapter,
    JjError, RepositoryFacts,
)
from lib.control.orchestration.cli import (
    _approve, _create, _plan, _reject, _repository_scope, _snapshot, main,
    reconcile_one_initiative,
)
from lib.control.orchestration.config import load_config
from lib.control.orchestration.store import InitiativeStore, StoreError
from lib.control.orchestration.scheduler import SchedulerError
from lib.control.orchestration.storage import storage_report
from tests.python.test_orchestration_graph import valid_plan


class OrchestrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {
            "HOME": str(self.root / "home"), "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"), "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        self.repo = self.root / "repo"
        (self.repo / ".asha").mkdir(parents=True)
        (self.repo / "Memory").mkdir()
        (self.repo / "Work/session-state").mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2, "project_id": "project-one",
        }) + "\n")
        (self.repo / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        )
        (self.repo / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")
        self.config = load_config(self.env)
        self.store = InitiativeStore(self.config)
        self.jj = mock.Mock()
        self.jj.preflight.return_value = RepositoryFacts(root=self.repo, git_root=self.repo / ".git")
        self.jj.working_copy_parent.return_value = "b" * 40
        self.jj.git_head.return_value = "b" * 40
        self.jj.git_head_exact.return_value = "b" * 40
        self.jj.resolve_base.return_value = "b" * 40
        self.jj.resolve_default_base.return_value = DefaultBaseResolution(
            ("refs/heads/master",), "b" * 40, "attached-local",
        )
        self.jj.require_visible_commit.return_value = None
        self.jj.immutable_tree.return_value = ImmutableTree(
            commit_id="b" * 40, digest="c" * 64, entries=(),
        )

    def test_shared_single_initiative_reconcile_uses_cli_order_and_never_dispatches(self) -> None:
        initiative_id = "12345678-1234-4234-8234-123456789abc"
        initiative = {"initiative_id": initiative_id}
        store = mock.Mock(spec=InitiativeStore)
        store.peek.return_value = initiative
        events: list[str] = []
        with mock.patch(
            "lib.control.orchestration.cli.reconcile_actions",
            side_effect=lambda *_: events.append("actions") or {"a": 1},
        ), mock.patch(
            "lib.control.orchestration.cli.reconcile_live",
            side_effect=lambda *_: events.append("live") or {"l": 1},
        ), mock.patch(
            "lib.control.orchestration.cli._snapshot",
            side_effect=lambda *_: events.append("snapshot") or {
                "nodes": [{"node_id": "node-a"}], "superseded_nodes": [],
            },
        ), mock.patch(
            "lib.control.orchestration.cli.reconcile_nodes",
            side_effect=lambda *_args, **_kwargs: events.append("nodes") or [{"state": "ready"}],
        ), mock.patch(
            "lib.control.orchestration.scheduler.dispatch",
        ) as dispatch:
            result = reconcile_one_initiative(store, initiative_id)

        self.assertEqual(events, ["actions", "live", "snapshot", "nodes"])
        self.assertEqual(result["initiative_id"], initiative_id)
        self.assertEqual(result["results"], [{"state": "ready"}])
        dispatch.assert_not_called()

    def create(self, slug: str = "demo") -> dict:
        return _create([
            "--repo", str(self.repo), "--slug", slug, "--label", "Demo",
            "--objective", "Build the bounded change.", "--json",
        ], self.config, self.store, self.jj)["initiative"]

    def write_plan(self, initiative: dict, name: str = "plan.json") -> Path:
        value = valid_plan()
        value["initiative_id"] = initiative["initiative_id"]
        value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
        repository_id = initiative["scope"]["repository"]["repository_id"]
        for node in value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def write_plan_value(self, value: dict, name: str) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def fail_after(self, method_name: str, ordinal: int):
        real = getattr(self.store, method_name)
        calls = 0

        def flaky(*args, **kwargs):
            nonlocal calls
            result = real(*args, **kwargs)
            calls += 1
            if calls == ordinal:
                raise OSError(f"injected post-commit failure in {method_name} #{ordinal}")
            return result

        return mock.patch.object(self.store, method_name, side_effect=flaky)

    def invoke(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "lib.control.orchestration.cli.JjAdapter", return_value=self.jj,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["initiative", *args], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_baseline_human_output_is_two_lines_and_resolution_is_read_only(self) -> None:
        status, stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo),
        ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            stdout,
            f"Commit: {'b' * 40}\nTree digest: {'c' * 64}\n",
        )
        self.jj.resolve_default_base.assert_called_once_with(self.repo)
        self.jj.resolve_base.assert_not_called()
        self.assertEqual(
            [call[0] for call in self.jj.method_calls],
            [
                "preflight", "working_copy_parent", "git_head_exact",
                "resolve_default_base", "require_visible_commit",
                "immutable_tree", "preflight",
            ],
        )
        self.jj.import_git.assert_not_called()

    def test_baseline_default_trunk_error_translates_only_supported_guidance(self) -> None:
        self.jj.resolve_default_base.side_effect = JjError(
            "Git has no attached local branch, remote default, or conventional "
            "local main/master/trunk; pass an explicit --base"
        )
        status, stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo),
        ])
        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("could not resolve revision the default base", stderr)
        self.assertIn("no attached local branch", stderr)
        self.assertIn("explicit --revision", stderr)
        self.assertNotIn("--base", stderr)
        self.assertNotIn("Pass an explicit --base", stderr)
        self.assertNotIn("for example --base main", stderr)
        self.assertIn("no import was attempted", stderr)
        self.assertIn("If Git knows the revision or bookmark", stderr)
        self.assertIn("`jj status`", stderr)
        self.jj.import_git.assert_not_called()

    def test_baseline_resolution_error_preserves_base_inside_revision_text(self) -> None:
        self.jj.resolve_base.side_effect = JjError(
            "revision 'feature--base' does not exist"
        )

        status, stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo),
            "--revision", "feature--base",
        ])

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("revision 'feature--base' does not exist", stderr)
        self.assertNotIn("feature--revision", stderr)

    def test_explicit_legacy_default_expression_is_not_reclassified_as_omitted(self) -> None:
        status, _stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo),
            "--revision", DEFAULT_BASE_REVSET,
        ])

        self.assertEqual((status, stderr), (0, ""))
        self.jj.resolve_base.assert_called_once_with(
            self.repo, DEFAULT_BASE_REVSET,
        )
        self.jj.resolve_default_base.assert_not_called()

    def test_baseline_refuses_colocated_divergence_before_resolution(self) -> None:
        self.jj.git_head_exact.return_value = "d" * 40

        status, stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo), "--revision", "main",
        ])

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("source working copy is out of sync with jj", stderr)
        self.assertIn(f"run `jj status` in {self.repo} to import it, then retry", stderr)
        self.jj.resolve_base.assert_not_called()
        self.jj.immutable_tree.assert_not_called()
        self.jj.import_git.assert_not_called()

    def test_top_level_help_lists_baseline_verb(self) -> None:
        root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            ["bash", str(root / "bin/asha"), "--help"],
            check=True, capture_output=True, text=True,
        )
        self.assertIn("asha initiative baseline|create|plan", completed.stdout)

    def test_create_plan_approve_list_show_events_snapshot_and_readonly_joins(self) -> None:
        initiative = self.create()
        self.assertEqual(initiative["acceptance_criteria"], [initiative["objective"]])
        self.assertEqual(initiative["last_event_sequence"], 1)
        plan_path = self.write_plan(initiative)
        plan, _ = _plan([initiative["initiative_id"], "--file", str(plan_path), "--json"], self.store, self.config, jj=self.jj)
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "awaiting-plan-approval")
        before = self.store.peek(initiative["initiative_id"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            _approve([initiative["initiative_id"], "--digest", "0" * 64], self.store)
        self.assertEqual(self.store.peek(initiative["initiative_id"]), before)
        approved, _ = _approve([initiative["initiative_id"], "--digest", plan["digest"], "--json"], self.store)
        self.assertEqual(approved["initiative"]["state"], "approved")
        self.assertEqual(approved["approval"]["state"], "consumed")
        self.assertEqual(approved["plan"]["status"], "proposed")
        self.assertTrue(all(node["state"] == "approved" for node in self.store.list_nodes_snapshot(initiative["initiative_id"])))
        snapshot = _snapshot(self.store, approved["initiative"])
        self.assertEqual(snapshot["active_plan"]["digest"], plan["digest"])
        self.assertEqual(snapshot["last_event_sequence"], 3)
        self.assertEqual([event["type"] for event in self.store.list_events_snapshot(initiative["initiative_id"])],
                         ["initiative-created", "plan-proposed", "plan-approved"])
        storage_jj = mock.Mock()
        storage_jj.workspace_identities.return_value = {}
        report = storage_report(approved["initiative"], store=self.store, control_store=mock.Mock(), jj=storage_jj)
        self.assertEqual(report["contract"], "asha.orchestration-storage-report.v1")
        self.assertFalse(report["workspaces"])
        self.jj.add_workspace.assert_not_called()
        self.jj.forget_workspace.assert_not_called()

    def test_list_all_includes_archived_initiatives(self) -> None:
        archived = self.create("archived-list")
        archived = copy.deepcopy(archived)
        archived["state"] = "archived"
        with mock.patch.object(
            InitiativeStore, "list_initiatives", return_value=[archived],
        ):
            status, stdout, stderr = self.invoke(["list", "--json"])
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(json.loads(stdout)["initiatives"], [])
            status, stdout, stderr = self.invoke(["list", "--all", "--json"])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout)["initiatives"][0]["initiative_id"],
            archived["initiative_id"],
        )

    def test_public_cli_routes_every_increment_one_verb(self) -> None:
        with mock.patch(
            "lib.control.orchestration.cli.JjAdapter", return_value=self.jj,
        ):
            status, stdout, stderr = self.invoke([
                "create", "--repo", str(self.repo), "--slug", "public-cli",
                "--label", "Public CLI", "--objective", "Exercise every route.",
                "--json",
            ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(stdout.count("\n"), 1)
        created = json.loads(stdout)
        self.assertEqual(
            created["contract"], "asha.orchestration-initiative-create.v1",
        )
        initiative = created["initiative"]
        self.assertEqual(
            self.store.peek(initiative["initiative_id"]), initiative,
        )
        plan_path = self.write_plan(initiative, "public-plan.json")
        status, stdout, stderr = self.invoke([
            "plan", initiative["initiative_id"], "--file", str(plan_path), "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        plan = json.loads(stdout)
        self.assertEqual(stdout.count("\n"), 1)

        for args, contract in (
            (["list", "--json"], "asha.orchestration-initiative-list.v1"),
            (["plan", initiative["initiative_id"], "--show", "--json"],
             "asha.orchestration-plan.v1"),
            (["show", initiative["initiative_id"], "--json"],
             "asha.orchestration-initiative-show.v1"),
            (["events", initiative["initiative_id"], "--after", "0", "--json"],
             "asha.orchestration-event-list.v1"),
            (["reconcile", initiative["initiative_id"], "--json"],
             "asha.orchestration-reconcile-list.v1"),
            (["snapshot", initiative["initiative_id"], "--json"],
             "asha.orchestration-snapshot.v1"),
        ):
            with self.subTest(args=args):
                status, stdout, stderr = self.invoke(args)
                self.assertEqual((status, stderr), (0, ""))
                value = json.loads(stdout)
                self.assertEqual(value["contract"], contract)
                if contract == "asha.orchestration-initiative-show.v1":
                    self.assertEqual(set(value["evidence_counts"]), {
                        "links", "result-publications", "results", "seals",
                        "seal-preparations",
                        "reviews", "verifications", "bundles", "approvals",
                        "actions", "evidence", "events",
                    })
                    self.assertEqual(value["evidence_counts"]["evidence"], 0)
                self.assertEqual(stdout.count("\n"), 1)

        with mock.patch(
            "lib.control.orchestration.storage.JjAdapter.workspace_identities",
            return_value={},
        ):
            status, stdout, stderr = self.invoke([
                "storage", initiative["initiative_id"], "--json",
            ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout)["contract"],
            "asha.orchestration-storage-report.v1",
        )

        status, stdout, stderr = self.invoke([
            "approve", initiative["initiative_id"], "--digest", plan["digest"],
            "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout)["contract"],
            "asha.orchestration-plan-approval.v1",
        )

        doctor_payload = {
            "contract": "asha.orchestration-doctor.v1", "ok": True,
            "probes": [{"name": "test", "outcome": "match", "detail": "ok"}],
            "limitations": [],
        }
        with mock.patch(
            "lib.control.orchestration.cli.run_orchestration_doctor",
            return_value=doctor_payload,
        ):
            status, stdout, stderr = self.invoke(["doctor", "--json"])
            self.assertEqual((status, stderr), (0, ""))
            self.assertEqual(json.loads(stdout), doctor_payload)
            status, stdout, stderr = self.invoke(["doctor"])
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("match       test: ok", stdout)

        rejected = self.create("public-reject")
        rejected_path = self.write_plan(rejected, "public-reject.json")
        status, stdout, _ = self.invoke([
            "plan", rejected["initiative_id"], "--file", str(rejected_path), "--json",
        ])
        rejected_plan = json.loads(stdout)
        self.assertEqual(status, 0)
        status, stdout, stderr = self.invoke([
            "reject", rejected["initiative_id"], "--digest", rejected_plan["digest"],
            "--reason", "Revise scope", "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(
            json.loads(stdout)["contract"],
            "asha.orchestration-plan-rejection.v1",
        )

    def test_repository_identity_is_stable_and_duplicate_plan_keys_refuse(self) -> None:
        first = _repository_scope(self.repo, self.jj)
        second = _repository_scope(self.repo, self.jj)
        self.assertEqual(first, second)

        initiative = self.create("duplicate-plan")
        path = self.root / "duplicate.json"
        path.write_text('{"contract":"first","contract":"second"}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            _plan([initiative["initiative_id"], "--file", str(path)], self.store, self.config, jj=self.jj)

        fifo = self.root / "plan.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            _plan([initiative["initiative_id"], "--file", str(fifo)], self.store, self.config, jj=self.jj)

    def test_plan_refuses_before_persistence_when_control_goal_cannot_fit(self) -> None:
        initiative = self.create("s" * 40)
        plan_path = self.write_plan(initiative, "overlong-goal.json")
        with mock.patch(
            "lib.control.orchestration.cli.validate_goal_capacity",
            side_effect=SchedulerError("absolute assignment path exceeds goal limit"),
        ), self.assertRaisesRegex(SchedulerError, "assignment path"):
            _plan(
                [initiative["initiative_id"], "--file", str(plan_path)],
                self.store, self.config, jj=self.jj,
            )
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "draft")
        self.assertEqual(self.store.list_plans_snapshot(initiative["initiative_id"]), [])

    def test_plan_refuses_invisible_approved_baseline_with_named_remediation(self) -> None:
        initiative = self.create("invisible-baseline")
        plan_path = self.write_plan(initiative, "invisible-baseline.json")
        commit = "b" * 40
        command = (
            f"asha initiative baseline --repo {self.repo} --revision {commit}"
        )
        self.jj.require_visible_commit.side_effect = JjError("not visible")

        status, stdout, stderr = self.invoke([
            "plan", initiative["initiative_id"], "--file", str(plan_path),
        ])

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("node implementation-a", stderr)
        self.assertIn(command, stderr)
        self.jj.immutable_tree.assert_not_called()
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "draft")
        self.assertEqual(self.store.list_plans_snapshot(initiative["initiative_id"]), [])

    def test_plan_scope_baseline_does_not_run_approved_baseline_probe(self) -> None:
        initiative = self.create("scope-baseline")
        value = json.loads(
            self.write_plan(initiative, "scope-baseline.json").read_text()
        )
        value["nodes"][0]["base"]["policy"] = "scope-baseline"
        path = self.write_plan_value(value, "scope-baseline.json")
        self.jj.reset_mock()

        plan, _ = _plan([
            initiative["initiative_id"], "--file", str(path),
        ], self.store, self.config, jj=self.jj)

        self.assertEqual(plan["nodes"][0]["base"]["policy"], "scope-baseline")
        self.jj.require_visible_commit.assert_not_called()
        self.jj.immutable_tree.assert_not_called()

    def test_indeterminate_operator_action_exits_three(self) -> None:
        with mock.patch(
            "lib.control.orchestration.cli._operator_action",
            return_value=({"state": "indeterminate"}, True),
        ):
            status, stdout, stderr = self.invoke([
                "dispatch", "ignored", "--node", "ignored", "--json",
            ])
        self.assertEqual((status, stderr), (3, ""))
        self.assertEqual(json.loads(stdout)["state"], "indeterminate")

    def test_storage_report_supports_required_positional_api(self) -> None:
        initiative = self.create("positional-storage")
        with mock.patch(
            "lib.control.orchestration.storage.load_config", return_value=self.config,
        ), mock.patch(
            "lib.control.orchestration.storage.JjAdapter.workspace_identities",
            return_value={},
        ):
            report = storage_report(initiative)
        self.assertEqual(report["contract"], "asha.orchestration-storage-report.v1")

    def test_storage_totals_count_one_workspace_once_across_attempt_links(self) -> None:
        initiative = self.create("deduplicated-storage")
        workspace = self.root / "retained-workspace"
        workspace.mkdir()
        fake_store = mock.Mock()
        fake_store.config = self.config
        fake_store.inventory.return_value = {
            "initiative": {"bytes": 5, "inodes": 1},
            "totals": {"bytes": 5, "inodes": 1},
        }
        fake_store.list_links_snapshot.return_value = [
            {"attempt_id": "one", "control_task_id": "task"},
            {"attempt_id": "two", "control_task_id": "task"},
        ]
        fake_store.list_verifications_snapshot.return_value = []
        control = mock.Mock()
        control.peek.return_value = {
            "jj": {"workspace_path": str(workspace), "workspace_name": "shared"},
        }
        jj = mock.Mock()
        jj.workspace_identities.return_value = {"shared": ("change", "commit")}
        with mock.patch(
            "lib.control.orchestration.storage._path_usage", return_value=(10, 2),
        ) as usage:
            report = storage_report(
                initiative, store=fake_store, control_store=control, jj=jj,
            )
        self.assertEqual(report["totals"], {"bytes": 15, "inodes": 3})
        self.assertEqual(usage.call_count, 1)

    def test_storage_counts_shared_verification_materialization_once_and_pauses(self) -> None:
        initiative = self.create("materialization-storage")
        materialization = self.root / "retained-materialization"
        materialization.mkdir()
        fake_store = mock.Mock()
        fake_store.config = replace(
            self.config,
            max_retained_bytes_before_pause=15,
            max_retained_inodes_before_pause=100,
        )
        fake_store.inventory.return_value = {
            "initiative": {"bytes": 5, "inodes": 1},
            "totals": {"bytes": 5, "inodes": 1},
        }
        fake_store.list_links_snapshot.return_value = []
        fake_store.list_verifications_snapshot.return_value = [
            {
                "verification_id": "one",
                "materialization_path": str(materialization),
            },
            {
                "verification_id": "two",
                "materialization_path": str(materialization),
            },
        ]
        jj = mock.Mock()
        jj.workspace_identities.return_value = {}
        with mock.patch(
            "lib.control.orchestration.storage._path_usage", return_value=(10, 2),
        ) as usage:
            report = storage_report(
                initiative, store=fake_store,
                control_store=mock.Mock(), jj=jj,
            )
        self.assertEqual(report["totals"], {"bytes": 15, "inodes": 3})
        self.assertTrue(report["pause_recommended"])
        self.assertEqual(usage.call_count, 1)
        self.assertEqual(len(report["materializations"]), 1)
        self.assertEqual(
            report["materializations"][0]["verification_ids"], ["one", "two"],
        )

    def test_reject_returns_to_planning_and_records_one_event(self) -> None:
        initiative = self.create("reject-demo")
        plan, _ = _plan([initiative["initiative_id"], "--file", str(self.write_plan(initiative, "reject.json"))], self.store, self.config, jj=self.jj)
        payload, _ = _reject([
            initiative["initiative_id"], "--digest", plan["digest"], "--reason", "Revise scope", "--json",
        ], self.store)
        self.assertEqual(payload["initiative"]["state"], "planning")
        self.assertEqual(self.store.list_events_snapshot(initiative["initiative_id"])[-1]["type"], "plan-rejected")

    def test_reject_replan_round_trip_supersedes_old_nodes(self) -> None:
        initiative = self.create("round-trip")
        first, _ = _plan([
            initiative["initiative_id"], "--file", str(self.write_plan(initiative, "round-1.json")),
        ], self.store, self.config, jj=self.jj)
        old_ids = sorted(node["node_id"] for node in first["nodes"])
        _reject([
            initiative["initiative_id"], "--digest", first["digest"],
            "--reason", "replace graph",
        ], self.store)
        rejected_snapshot = _snapshot(
            self.store, self.store.peek(initiative["initiative_id"]),
        )
        self.assertEqual(rejected_snapshot["nodes"], [])
        self.assertEqual(
            sorted(node["node_id"] for node in rejected_snapshot["superseded_nodes"]),
            old_ids,
        )

        same = json.loads(self.write_plan(initiative, "same-ids.json").read_text())
        same["revision"] = 2
        same_path = self.write_plan_value(same, "same-ids.json")
        with self.assertRaisesRegex(ValueError, ", ".join(old_ids)):
            _plan([initiative["initiative_id"], "--file", str(same_path)], self.store, self.config, jj=self.jj)

        renamed = copy.deepcopy(same)
        mapping = {node_id: f"{node_id}-v2" for node_id in old_ids}
        for node in renamed["nodes"]:
            node["node_id"] = mapping[node["node_id"]]
            node["dependencies"] = [mapping[item] for item in node["dependencies"]]
            if node["base"] is not None:
                node["base"]["upstream_node_ids"] = [
                    mapping[item] for item in node["base"]["upstream_node_ids"]
                ]
        for gate in renamed["declared_gates"]:
            gate["node_id"] = mapping[gate["node_id"]]
        renamed_path = self.write_plan_value(renamed, "renamed.json")
        second, _ = _plan([
            initiative["initiative_id"], "--file", str(renamed_path),
        ], self.store, self.config, jj=self.jj)
        _approve([
            initiative["initiative_id"], "--digest", second["digest"],
        ], self.store)
        snapshot = _snapshot(self.store, self.store.peek(initiative["initiative_id"]))
        self.assertEqual(
            sorted(node["node_id"] for node in snapshot["nodes"]),
            sorted(mapping.values()),
        )
        self.assertEqual(
            sorted(node["node_id"] for node in snapshot["superseded_nodes"]),
            old_ids,
        )
        status, stdout, stderr = self.invoke([
            "show", initiative["initiative_id"], "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        shown = json.loads(stdout)
        self.assertEqual(
            sorted(node["node_id"] for node in shown["graph"]["nodes"]),
            sorted(mapping.values()),
        )
        self.assertEqual(
            sorted(node["node_id"] for node in shown["superseded_nodes"]),
            old_ids,
        )
        status, stdout, stderr = self.invoke([
            "reconcile", initiative["initiative_id"], "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        reconciled = json.loads(stdout)
        self.assertEqual(
            sorted(item["node_id"] for item in reconciled["results"]),
            sorted(mapping.values()),
        )
        self.assertEqual(
            sorted(node["node_id"] for node in reconciled["superseded_nodes"]),
            old_ids,
        )

    def test_plan_and_approve_resume_after_each_post_commit_save_failure(self) -> None:
        plan_failures = [
            ("save_initiative", 1), ("save_plan", 1),
            ("save_node", 1), ("save_node", 2), ("save_node", 3),
            ("save_initiative", 2), ("append_event", 1),
        ]
        for index, (method_name, ordinal) in enumerate(plan_failures):
            with self.subTest(stage=f"plan:{method_name}:{ordinal}"):
                initiative = self.create(f"plan-retry-{index}")
                path = self.write_plan(initiative, f"plan-retry-{index}.json")
                with self.fail_after(method_name, ordinal), self.assertRaises((OSError, StoreError)):
                    _plan([initiative["initiative_id"], "--file", str(path)], self.store, self.config, jj=self.jj)
                plan, _ = _plan([
                    initiative["initiative_id"], "--file", str(path),
                ], self.store, self.config, jj=self.jj)
                self.assertEqual(len(self.store.list_plans_snapshot(initiative["initiative_id"])), 1)
                self.assertEqual(
                    sum(event["type"] == "plan-proposed" for event in self.store.list_events_snapshot(initiative["initiative_id"])),
                    1,
                )
                self.assertEqual(plan["revision"], 1)

        approve_failures = [
            ("save_approval", 1), ("save_node", 1), ("save_node", 2),
            ("save_node", 3), ("save_initiative", 1),
            ("save_approval", 2), ("append_event", 1),
        ]
        for index, (method_name, ordinal) in enumerate(approve_failures):
            with self.subTest(stage=f"approve:{method_name}:{ordinal}"):
                initiative = self.create(f"approve-retry-{index}")
                path = self.write_plan(initiative, f"approve-retry-{index}.json")
                plan, _ = _plan([
                    initiative["initiative_id"], "--file", str(path),
                ], self.store, self.config, jj=self.jj)
                with self.fail_after(method_name, ordinal), self.assertRaises((OSError, StoreError)):
                    _approve([initiative["initiative_id"], "--digest", plan["digest"]], self.store)
                payload, _ = _approve([
                    initiative["initiative_id"], "--digest", plan["digest"],
                ], self.store)
                self.assertEqual(payload["initiative"]["state"], "approved")
                self.assertEqual(payload["approval"]["state"], "consumed")
                self.assertEqual(len(self.store.list_approvals_snapshot(initiative["initiative_id"])), 1)
                self.assertEqual(
                    sum(event["type"] == "plan-approved" for event in self.store.list_events_snapshot(initiative["initiative_id"])),
                    1,
                )

    def test_expired_partial_approval_can_be_reapproved_or_rejected(self) -> None:
        for recovery in ("approve", "reject"):
            with self.subTest(recovery=recovery):
                initiative = self.create(f"expired-{recovery}")
                path = self.write_plan(initiative, f"expired-{recovery}.json")
                plan, _ = _plan([
                    initiative["initiative_id"], "--file", str(path),
                ], self.store, self.config, jj=self.jj)
                with self.fail_after("save_node", 1), self.assertRaises((OSError, StoreError)):
                    _approve([
                        initiative["initiative_id"], "--digest", plan["digest"],
                    ], self.store)
                first = self.store.list_approvals_snapshot(initiative["initiative_id"])
                self.assertEqual([item["state"] for item in first], ["approved"])
                self.assertIn(
                    "approved",
                    {node["state"] for node in self.store.list_nodes_snapshot(initiative["initiative_id"])},
                )
                expiry = datetime.fromisoformat(
                    first[0]["expires_at"][:-1] + "+00:00"
                )
                future = (expiry + timedelta(seconds=1)).isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
                with mock.patch("lib.control.orchestration.cli._now", return_value=future):
                    if recovery == "approve":
                        payload, _ = _approve([
                            initiative["initiative_id"], "--digest", plan["digest"],
                        ], self.store)
                        self.assertEqual(payload["initiative"]["state"], "approved")
                        self.assertEqual(
                            sorted(item["state"] for item in self.store.list_approvals_snapshot(initiative["initiative_id"])),
                            ["consumed", "expired"],
                        )
                    else:
                        payload, _ = _reject([
                            initiative["initiative_id"], "--digest", plan["digest"],
                            "--reason", "operator rejected after interrupted approval",
                        ], self.store)
                        self.assertEqual(payload["initiative"]["state"], "planning")
                        self.assertEqual(
                            [item["state"] for item in self.store.list_approvals_snapshot(initiative["initiative_id"])],
                            ["expired"],
                        )
                        self.assertTrue(all(
                            node["state"] == "superseded"
                            for node in self.store.list_nodes_snapshot(initiative["initiative_id"])
                        ))

    def test_pending_different_plan_and_nonplanning_state_messages_are_exact(self) -> None:
        initiative = self.create("pending-message")
        path = self.write_plan(initiative, "pending-message.json")
        plan, _ = _plan([
            initiative["initiative_id"], "--file", str(path),
        ], self.store, self.config, jj=self.jj)
        different = json.loads(path.read_text())
        different["nodes"][0]["goal"] += " Different."
        different_path = self.write_plan_value(different, "pending-different.json")
        with self.assertRaisesRegex(ValueError, "reject the pending revision first"):
            _plan([
                initiative["initiative_id"], "--file", str(different_path),
            ], self.store, self.config, jj=self.jj)

        _approve([
            initiative["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        with self.assertRaisesRegex(
            ValueError,
            "must be draft, planning, or awaiting plan approval",
        ):
            _plan([
                initiative["initiative_id"], "--file", str(path),
            ], self.store, self.config, jj=self.jj)

    def test_wrong_file_deep_json_long_objective_and_empty_route_refuse(self) -> None:
        initiative = self.create("wrong-file")
        base = json.loads(self.write_plan(initiative, "wrong-base.json").read_text())
        for field, value in (
            ("initiative_id", "11111111-1111-4111-8111-111111111111"),
            ("revision", 99), ("status", "approved"), ("digest", "0" * 64),
        ):
            wrong = copy.deepcopy(base)
            wrong[field] = value
            path = self.write_plan_value(wrong, f"wrong-{field}.json")
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                _plan([initiative["initiative_id"], "--file", str(path)], self.store, self.config, jj=self.jj)

        deep = self.root / "deep.json"
        deep.write_text("[" * 100000)
        status, _, stderr = self.invoke([
            "plan", initiative["initiative_id"], "--file", str(deep),
        ])
        self.assertEqual(status, 2)
        self.assertNotIn("internal error", stderr)

        status, _, stderr = self.invoke([
            "create", "--repo", str(self.repo), "--slug", "long-objective",
            "--label", "Long", "--objective", "x" * 2049,
        ])
        self.assertEqual(status, 2)
        self.assertIn("--acceptance is required", stderr)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["initiative"], env=self.env)
        self.assertEqual((status, stdout.getvalue()), (2, ""))
        self.assertIn("Usage:", stderr.getvalue())

    def test_activate_and_increment_two_task_verbs_refuse(self) -> None:
        self.assertEqual(main(["initiative", "activate", "missing"]), 2)
        for verb in ("report", "result", "seal"):
            self.assertEqual(main(["task", verb]), 2)


@unittest.skipUnless(shutil.which("jj") and shutil.which("git"), "jj and git are required")
class RealJjOrchestrationCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo with spaces"
        self.remote = self.root / "remote.git"
        self.repo.mkdir()
        self.git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "JJ_CONFIG": str(self.root / "jj-config.toml"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        Path(self.git_env["JJ_CONFIG"]).write_text(
            '[user]\nname = "t"\nemail = "t@t"\n'
        )
        subprocess.run(
            ["git", "init", "-q", str(self.repo)], check=True, env=self.git_env,
        )
        (self.repo / "tracked").write_text("base\n")
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "tracked"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "base"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "init", "--bare", "-q", str(self.remote)],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote)],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "push", "-qu", "origin", "HEAD:main"],
            check=True, env=self.git_env,
        )
        subprocess.run(
            ["jj", "git", "init", "--colocate", str(self.repo)],
            check=True, capture_output=True, env=self.git_env, cwd=self.root,
        )
        (self.repo / ".asha").mkdir()
        (self.repo / "Memory").mkdir()
        (self.repo / "Work/session-state").mkdir(parents=True)
        (self.repo / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "integration-project",
        }) + "\n")
        (self.repo / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
        )
        (self.repo / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")
        self.env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        self.config = load_config(self.env)
        self.store = InitiativeStore(self.config)
        self.jj = JjAdapter()

    def invoke(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["initiative", *args], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    def create(self, slug: str) -> dict:
        return _create([
            "--repo", str(self.repo), "--slug", slug, "--label", "Real",
            "--objective", "Read only repository preflight.",
        ], self.config, self.store, self.jj)["initiative"]

    def plan_value(self, initiative: dict, tree_digest: str) -> dict:
        commit = self.jj.resolve_base(self.repo, "@-")
        value = valid_plan()
        value["initiative_id"] = initiative["initiative_id"]
        value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
        repository_id = initiative["scope"]["repository"]["repository_id"]
        for node in value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
            if node["base"] is not None:
                node["base"]["scope_origin"] = {
                    "jj_commit_id": commit,
                    "tree_digest": tree_digest,
                }
        return value

    def test_create_uses_read_only_preflight_and_creates_no_workspace(self) -> None:
        before = subprocess.run(
            ["jj", "-R", str(self.repo), "--ignore-working-copy", "workspace", "list"],
            check=True, capture_output=True, text=True, env=self.git_env,
            cwd=self.root,
        ).stdout
        self.create("real-create")
        after = subprocess.run(
            ["jj", "-R", str(self.repo), "--ignore-working-copy", "workspace", "list"],
            check=True, capture_output=True, text=True, env=self.git_env,
            cwd=self.root,
        ).stdout
        self.assertEqual(before, after)

    def test_baseline_json_matches_the_adapters_immutable_tree(self) -> None:
        commit = self.jj.resolve_base(self.repo, "trunk()")
        tree = self.jj.immutable_tree(self.repo, commit)
        operation_before = self.jj.pin_operation(self.repo)
        status, stdout, stderr = self.invoke([
            "baseline", "--repo", str(self.repo), "--json",
        ])
        self.assertEqual((status, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["contract"], "asha.orchestration-baseline.v1")
        self.assertEqual(payload["jj_commit_id"], commit)
        self.assertEqual(payload["tree_digest"], tree.digest)
        self.assertEqual(payload["entry_count"], len(tree.entries))
        self.assertEqual(payload["repository"]["root"], str(self.repo))
        self.assertEqual(set(payload), {
            "contract", "repository", "jj_commit_id", "tree_digest",
            "entry_count",
        })
        self.assertEqual(
            set(payload["repository"]), {"root", "control_repository_id"},
        )
        self.assertEqual(self.jj.pin_operation(self.repo), operation_before)

    def test_plan_refuses_wrong_scope_origin_digest_before_persistence(self) -> None:
        initiative = self.create("wrong-baseline")
        value = self.plan_value(initiative, "0" * 64)
        path = self.root / "wrong-baseline.json"
        path.write_text(json.dumps(value))
        commit = value["nodes"][0]["base"]["scope_origin"]["jj_commit_id"]
        command = shlex.join([
            "asha", "initiative", "baseline", "--repo", str(self.repo),
            "--revision", commit,
        ])

        status, stdout, stderr = self.invoke([
            "plan", initiative["initiative_id"], "--file", str(path),
        ])

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("node implementation-a", stderr)
        self.assertIn(command, stderr)
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "draft")
        self.assertEqual(self.store.list_plans_snapshot(initiative["initiative_id"]), [])

    def test_plan_accepts_matching_scope_origin_digest(self) -> None:
        initiative = self.create("right-baseline")
        commit = self.jj.resolve_base(self.repo, "@-")
        tree = self.jj.immutable_tree(self.repo, commit)
        path = self.root / "right-baseline.json"
        path.write_text(json.dumps(self.plan_value(initiative, tree.digest)))

        status, stdout, stderr = self.invoke([
            "plan", initiative["initiative_id"], "--file", str(path), "--json",
        ])

        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["revision"], 1)
        self.assertEqual(
            self.store.peek(initiative["initiative_id"])["state"],
            "awaiting-plan-approval",
        )


if __name__ == "__main__":
    unittest.main()
