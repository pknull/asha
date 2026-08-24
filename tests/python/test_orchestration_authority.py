"""Standing authorities: the operator's pre-signed approval, matched fail-closed."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from lib.control.orchestration import cli
from lib.control.orchestration.authority import (
    AUTHORITY_CONTRACT,
    AuthorityError,
    add_authority,
    authorities_dir,
    list_authorities,
    match_plan,
    revoke_authority,
    validate_authority,
)
from lib.control.orchestration.model import ModelError
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_graph import valid_plan


def _grant(fixture, **overrides):
    from lib.control.jj import RepositoryFacts

    fake = mock.Mock()
    fake.preflight.side_effect = lambda root: RepositoryFacts(
        root=Path(root).resolve(), git_root=Path(root).resolve() / ".git",
    )
    values = dict(
        root=fixture.repo, label="small-fixes",
        scope_prefixes=["lib/control/orchestration"],
        max_nodes=5, harnesses=["codex", "claude"], max_attempts_per_node=2,
    )
    values.update(overrides)
    return add_authority(fixture.config, jj=fake, **values)


class AuthorityRecordTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def test_grant_list_revoke_round_trip_retains_the_record(self) -> None:
        record = _grant(self)
        self.assertEqual(record["contract"], AUTHORITY_CONTRACT)
        self.assertEqual([item["authority_id"] for item in list_authorities(self.config)], [record["authority_id"]])
        revoked = revoke_authority(self.config, record["authority_id"])
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertEqual(list_authorities(self.config), [])
        retained = list_authorities(self.config, include_revoked=True)
        self.assertEqual(len(retained), 1, "revocation retains, never deletes")
        self.assertEqual(revoke_authority(self.config, record["authority_id"]), retained[0])

    def test_hostile_records_are_refused(self) -> None:
        record = _grant(self)
        for mutation in (
            {"label": "Bad Name"},
            {"auto_activate": "yes"},
            {"constraints": dict(record["constraints"], scope_prefixes=["../escape"])},
            {"constraints": dict(record["constraints"], scope_prefixes=["/absolute"])},
            {"constraints": dict(record["constraints"], max_nodes=0)},
            {"constraints": dict(record["constraints"], harnesses=["emacs"])},
        ):
            hostile = {**copy.deepcopy(record), **mutation}
            with self.assertRaises(ModelError):
                validate_authority(hostile)


class AuthorityMatcherTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def plan(self) -> dict:
        return self.store.read_plan(self.initiative_id, self.plan["revision"])

    def test_the_fixture_plan_matches_and_every_deviation_fails_closed(self) -> None:
        authority = _grant(self)
        initiative = self.initiative()
        matched, reason = match_plan(authority, initiative, self.plan)
        self.assertTrue(matched, reason)

        cases = []
        escape = copy.deepcopy(self.plan)
        escape["nodes"][0]["hard_write_scope"] = ["lib/control"]
        cases.append((escape, "outside the authorized scope"))
        dotdot = copy.deepcopy(self.plan)
        dotdot["nodes"][0]["hard_write_scope"] = ["lib/control/orchestration/../../secrets"]
        cases.append((dotdot, "outside the authorized scope"))
        crowded = copy.deepcopy(self.plan)
        crowded["nodes"] = crowded["nodes"] * 3
        cases.append((crowded, "at most"))
        alien = copy.deepcopy(self.plan)
        alien["nodes"][0]["harness"] = "copilot"
        cases.append((alien, "harness is not authorized"))
        gateless = copy.deepcopy(self.plan)
        gateless["declared_gates"] = [item for item in gateless["declared_gates"] if item["kind"] != "verification"]
        cases.append((gateless, "review and verification gates"))
        hollow = copy.deepcopy(self.plan)
        for gate in hollow["declared_gates"]:
            if gate["kind"] == "verification":
                gate["commands"] = []
        cases.append((hollow, "real minimal-environment verification"))
        greedy = copy.deepcopy(self.plan)
        greedy["limits"] = dict(greedy["limits"], max_attempts_per_node=5)
        cases.append((greedy, "attempt ceiling exceeds"))
        for plan, expected in cases:
            matched, reason = match_plan(authority, initiative, plan)
            self.assertFalse(matched, expected)
            self.assertIn(expected, reason)

    def test_revocation_identity_drift_and_headless_requirements_refuse(self) -> None:
        authority = _grant(self)
        initiative = self.initiative()
        revoked = dict(authority, revoked_at="2026-08-24T00:00:00.000000Z")
        self.assertFalse(match_plan(revoked, initiative, self.plan)[0])
        drifted = copy.deepcopy(initiative)
        drifted["scope"]["repository"]["initial_identity_digest"] = "f" * 64
        matched, reason = match_plan(authority, drifted, self.plan)
        self.assertFalse(matched)
        self.assertIn("identity digest drifted", reason)
        strict = dict(authority, constraints=dict(authority["constraints"], require_headless=True))
        matched, reason = match_plan(strict, initiative, self.plan)
        self.assertFalse(matched)
        self.assertIn("must be headless", reason)


class AuthorityAutoApprovalTests(ExecutionFixture, unittest.TestCase):
    """Propose -> matched authority -> pre-signed approval -> activation.

    The fixture proposes without approving; each test grants an authority and
    re-proposes the identical plan (the retry path), which is exactly when the
    controller consults standing authorities.
    """

    start_running = False
    approve_in_setup = False

    def repropose(self) -> dict:
        raw = valid_plan()
        raw["initiative_id"] = self.initiative_id
        raw["repositories"] = [copy.deepcopy(self.initiative()["scope"]["repository"])]
        repository_id = raw["repositories"][0]["repository_id"]
        for node in raw["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        return raw

    def test_matched_proposal_is_approved_and_activated_under_the_authority(self) -> None:
        _grant(self)
        raw = self.repropose()
        with mock.patch(
            "lib.control.orchestration.actions.run_orchestration_doctor",
            return_value={"ok": True, "contract": "asha.orchestration-doctor.v1", "probes": []},
        ), mock.patch(
            "lib.control.orchestration.actions._repository_identity_matches",
        ):
            plan = cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        current = self.initiative()
        self.assertEqual(current["state"], "running", "auto-activate must reach running")
        approvals = self.store.list_approvals_snapshot(self.initiative_id)
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approvals[0]["actor_id"].startswith("standing-authority:"))
        events = self.store.list_events_snapshot(self.initiative_id)
        decided = [item for item in events if item["type"] == "approval-decided"]
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0]["payload"]["plan_digest"], plan["digest"])
        self.assertIn("standing_authority_id", decided[0]["payload"])
        activations = [
            item for item in self.store.list_actions_snapshot(self.initiative_id)
            if item["action_class"] == "activate-initiative"
        ]
        self.assertEqual(len(activations), 1)
        self.assertTrue(activations[0]["actor_id"].startswith("standing-authority:"))

    def test_unmatched_proposal_waits_for_the_operator(self) -> None:
        _grant(self, scope_prefixes=["somewhere/else"])
        raw = self.repropose()
        with contextlib.redirect_stderr(io.StringIO()):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        self.assertEqual(self.initiative()["state"], "awaiting-plan-approval")
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_approval_without_auto_activate_stops_at_approved(self) -> None:
        _grant(self, auto_activate=False)
        raw = self.repropose()
        cli.propose_plan(
            self.store, self.initiative(), raw, config=self.config, jj=self.jj,
            actor_kind="operator", actor_id="cli",
        )
        self.assertEqual(self.initiative()["state"], "approved")

    def test_refused_activation_leaves_the_approval_and_reports(self) -> None:
        _grant(self)
        raw = self.repropose()
        with mock.patch(
            "lib.control.orchestration.actions.run_orchestration_doctor",
            return_value={"ok": False, "contract": "asha.orchestration-doctor.v1", "probes": []},
        ):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        self.assertEqual(self.initiative()["state"], "approved", "approval stands; activation refused cleanly")


class AuthorityVerbRefusalTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def test_grant_and_revoke_refuse_coordinator_sessions_and_panes(self) -> None:
        env = {**self.env, "ASHA_ORCHESTRATION_COORDINATOR_ID": "x"}
        with self.assertRaisesRegex(ValueError, "operator's own"):
            cli._authority_command(["add", "n", "--repo", str(self.repo), "--scope", "src"], self.config, env, mock.Mock(), jj=self.jj)
        pane_tmux = mock.Mock()
        pane_tmux.pane_option.return_value = "some-coordinator"
        env_pane = {**self.env, "TMUX_PANE": "%7"}
        with self.assertRaisesRegex(ValueError, "coordinator's pane is refused"):
            cli._authority_command(["add", "n", "--repo", str(self.repo), "--scope", "src"], self.config, env_pane, pane_tmux, jj=self.jj)
        # list is a read and stays available everywhere.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._authority_command(["list"], self.config, env, mock.Mock()), 0)


class AuthorityStorageFailureTests(ExecutionFixture, unittest.TestCase):
    """A damaged authority store must never approve, and never break proposing."""

    start_running = False
    approve_in_setup = False

    def test_write_once_creation_refuses_a_second_record_at_the_same_id(self) -> None:
        record = _grant(self)
        path = authorities_dir(self.config) / f"{record['authority_id']}.json"
        with self.assertRaises(FileExistsError):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)

    def test_a_corrupt_record_refuses_every_authority_without_breaking_proposal(self) -> None:
        _grant(self)
        (authorities_dir(self.config) / "corrupt.json").write_text("{not json")
        with self.assertRaisesRegex(AuthorityError, "unreadable"):
            list_authorities(self.config)
        raw = AuthorityAutoApprovalTests.repropose(self)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        self.assertEqual(self.initiative()["state"], "awaiting-plan-approval")
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])
        self.assertIn("standing authority not applied", errors.getvalue())

    def test_an_unreadable_store_is_reported_not_silently_empty(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        _grant(self)
        directory = authorities_dir(self.config)
        directory.chmod(0o000)
        self.addCleanup(directory.chmod, 0o700)
        raw = AuthorityAutoApprovalTests.repropose(self)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        self.assertEqual(self.initiative()["state"], "awaiting-plan-approval")
        self.assertIn("standing authority not applied", errors.getvalue())

    def test_a_revoked_authority_no_longer_approves_end_to_end(self) -> None:
        record = _grant(self)
        revoke_authority(self.config, record["authority_id"])
        raw = AuthorityAutoApprovalTests.repropose(self)
        cli.propose_plan(
            self.store, self.initiative(), raw, config=self.config, jj=self.jj,
            actor_kind="operator", actor_id="cli",
        )
        self.assertEqual(self.initiative()["state"], "awaiting-plan-approval")
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_mismatch_reasons_reach_the_operator(self) -> None:
        _grant(self, scope_prefixes=["somewhere/else"], label="narrow-grant")
        raw = AuthorityAutoApprovalTests.repropose(self)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        message = errors.getvalue()
        self.assertIn("narrow-grant", message)
        self.assertIn("outside the authorized scope", message)

    def test_grants_for_other_repositories_are_not_reported_as_near_misses(self) -> None:
        from lib.control.jj import RepositoryFacts

        other = self.root / "other-repo"
        (other / ".asha").mkdir(parents=True)
        (other / ".git").mkdir()
        (other / "Memory").mkdir()
        (other / "Work/session-state").mkdir(parents=True)
        (other / ".asha/config.json").write_text(json.dumps(
            {"initialized": True, "memory_version": 2, "project_id": "somewhere-else"}) + "\n")
        (other / "Memory/activeContext.md").write_text(
            "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n")
        (other / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")
        elsewhere = mock.Mock()
        elsewhere.preflight.side_effect = lambda root: RepositoryFacts(
            root=Path(root).resolve(), git_root=Path(root).resolve() / ".git",
        )
        add_authority(
            self.config, root=other, label="other-repo-grant",
            scope_prefixes=["lib"], jj=elsewhere,
        )
        raw = AuthorityAutoApprovalTests.repropose(self)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            cli.propose_plan(
                self.store, self.initiative(), raw, config=self.config, jj=self.jj,
                actor_kind="operator", actor_id="cli",
            )
        self.assertNotIn("other-repo-grant", errors.getvalue())
        self.assertEqual(self.initiative()["state"], "awaiting-plan-approval")


class AuthorityScopeContainmentTests(ExecutionFixture, unittest.TestCase):
    """Prefix containment must be path-segment exact, not string-prefix loose."""

    start_running = False

    def test_a_sibling_directory_sharing_a_name_prefix_is_not_authorized(self) -> None:
        authority = _grant(self, scope_prefixes=["lib/control"])
        initiative = self.initiative()
        self.assertTrue(match_plan(authority, initiative, self.plan)[0])
        neighbour = copy.deepcopy(self.plan)
        neighbour["nodes"][0]["hard_write_scope"] = ["lib/controlware/thing.py"]
        matched, reason = match_plan(authority, initiative, neighbour)
        self.assertFalse(matched, "lib/control must not authorize lib/controlware")
        self.assertIn("outside the authorized scope", reason)

    def test_advisory_ownership_is_held_to_the_same_scope_as_hard_writes(self) -> None:
        authority = _grant(self)
        initiative = self.initiative()
        sneaky = copy.deepcopy(self.plan)
        sneaky["nodes"][0]["advisory_path_ownership"] = ["identity"]
        matched, reason = match_plan(authority, initiative, sneaky)
        self.assertFalse(matched)
        self.assertIn("outside the authorized scope", reason)

    def test_a_non_work_node_may_not_declare_any_write_scope(self) -> None:
        authority = _grant(self)
        initiative = self.initiative()
        for key in ("hard_write_scope", "advisory_path_ownership"):
            plan = copy.deepcopy(self.plan)
            reviewer = next(node for node in plan["nodes"] if node["type"] != "work")
            reviewer[key] = ["lib/control/orchestration"]
            matched, reason = match_plan(authority, initiative, plan)
            self.assertFalse(matched, key)
            self.assertIn("declares a write scope", reason)


class AuthorityCommandSurfaceTests(ExecutionFixture, unittest.TestCase):
    """Every documented flag must actually reach the record."""

    start_running = False

    def grant_via_cli(self, *extra: str) -> dict:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            cli._authority_command(
                ["add", "tight", "--repo", str(self.repo), "--scope", "lib",
                 "--scope", "docs", *extra, "--json"],
                self.config, self.env, mock.Mock(), jj=self.jj,
            )
        return json.loads(captured.getvalue())["authority"]

    def test_flags_round_trip_into_the_stored_record(self) -> None:
        record = self.grant_via_cli(
            "--max-nodes", "3", "--harness", "codex", "--max-attempts", "1",
            "--require-headless", "--no-auto-activate",
        )
        self.assertEqual(record["contract"], AUTHORITY_CONTRACT)
        self.assertEqual(record["constraints"]["scope_prefixes"], ["lib", "docs"])
        self.assertEqual(record["constraints"]["max_nodes"], 3)
        self.assertEqual(record["constraints"]["harnesses"], ["codex"])
        self.assertEqual(record["constraints"]["max_attempts_per_node"], 1)
        self.assertTrue(record["constraints"]["require_headless"])
        self.assertFalse(record["auto_activate"])
        stored = list_authorities(self.config)
        self.assertEqual(stored, [record])

    def test_defaults_are_the_documented_ones(self) -> None:
        record = self.grant_via_cli()
        self.assertEqual(record["constraints"]["max_nodes"], 5)
        self.assertEqual(record["constraints"]["harnesses"], ["claude", "codex"])
        self.assertEqual(record["constraints"]["max_attempts_per_node"], 2)
        self.assertFalse(record["constraints"]["require_headless"])
        self.assertTrue(record["auto_activate"])

    def test_hostile_names_and_missing_arguments_are_refused(self) -> None:
        for args, expected in (
            (["add", "--repo", str(self.repo), "--scope", "lib"], "NAME first"),
            (["add", "ok", "--scope", "lib"], "repo"),
            (["add", "ok", "--repo", str(self.repo)], "at least one --scope"),
            (["add", "Bad Name", "--repo", str(self.repo), "--scope", "lib"], "grammar"),
            (["add", "esc", "--repo", str(self.repo), "--scope", "../etc"], "clean repository-relative"),
            (["add", "esc", "--repo", str(self.repo), "--scope", "/etc"], "clean repository-relative"),
            (["revoke"], "AUTHORITY_ID first"),
            (["nonsense"], "add, list, or revoke"),
        ):
            with self.assertRaises(ValueError, msg=args) as caught:
                cli._authority_command(args, self.config, self.env, mock.Mock(), jj=self.jj)
            self.assertIn(expected, str(caught.exception), msg=args)


if __name__ == "__main__":
    unittest.main()
