"""Operator integration attestations are durable facts, not integration machinery."""

from __future__ import annotations

import copy
import io
import json
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout

from lib.control.orchestration.cli import main
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.readiness import bind_readiness
from tests.python.orchestration_increment3_fixtures import (
    advance_node,
    save_passed_verification,
)
from tests.python.orchestration_workspace_fixtures import WorkspaceFixture


class IntegrationRecordingTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        created = self.create_initiative()
        self.approve_and_run(created, self.two_member_plan(created))
        members = self.initiative()["scope"]["workspace"]["repositories"]
        for node_id in (
            "impl-first", "impl-second", "review-first", "review-second", "verify-a",
        ):
            advance_node(
                self, node_id,
                ["ready", "dispatching", "running", "evaluating", "succeeded"],
            )
        self.first = self.save_member_candidate("impl-first", members[0]["repository_id"])
        self.second = self.save_member_candidate(
            "impl-second", members[1]["repository_id"], tree_digest="9" * 64,
        )
        self.save_member_review("review-first")
        self.save_member_review("review-second")
        save_passed_verification(self, [self.first, self.second])
        self.bundle = bind_readiness(self.store, self.initiative_id)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["initiative", "record-integration", *args], env=self.env)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_compatible_bundle_records_every_member_once_without_integrating(self) -> None:
        before_bundle = record_digest(self.bundle)
        before_events = len(self.store.list_events_snapshot(self.initiative_id))

        code, output, error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )

        self.assertEqual((code, error), (0, ""))
        event = json.loads(output)
        self.assertEqual(event["contract"], "asha.orchestration-event.v1")
        self.assertEqual(event["type"], "seal-integration-recorded")
        self.assertEqual((event["actor_kind"], event["actor_id"]), ("operator", "cli"))
        self.assertEqual(
            event["subject_ids"],
            [self.bundle["bundle_id"], self.first["seal_id"], self.second["seal_id"]],
        )
        self.assertEqual(event["payload"], {
            "disposition": "integrated",
            "members": [
                {"seal_id": self.first["seal_id"], "jj_commit_id": self.first["jj_commit_id"]},
                {"seal_id": self.second["seal_id"], "jj_commit_id": self.second["jj_commit_id"]},
            ],
        })
        self.assertEqual(self.initiative()["state"], "ready-for-integration")
        self.assertEqual(record_digest(self.store.read_bundle(
            self.initiative_id, self.bundle["bundle_id"],
        )), before_bundle)
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before_events + 1,
        )

        again_code, again_output, again_error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )
        self.assertEqual((again_code, again_error), (0, ""))
        self.assertEqual(json.loads(again_output)["event_id"], event["event_id"])
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before_events + 1,
        )

    def test_abandoned_seal_records_reason_and_conflicts_with_bundle_integration(self) -> None:
        reason = "Operator rejected this candidate after external review."
        code, output, error = self.run_cli(
            self.initiative_id, "--seal", self.first["seal_id"],
            "--abandoned", "--reason", reason, "--json",
        )
        self.assertEqual((code, error), (0, ""))
        event = json.loads(output)
        self.assertEqual(event["type"], "seal-integration-recorded")
        self.assertEqual(event["subject_ids"], [self.first["seal_id"]])
        self.assertEqual(event["payload"], {
            "disposition": "abandoned",
            "members": [{
                "seal_id": self.first["seal_id"],
                "jj_commit_id": self.first["jj_commit_id"],
            }],
            "reason": reason,
        })

        refused, _output, refused_error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )
        self.assertEqual(refused, 2)
        self.assertIn("already recorded as abandoned", refused_error)

    def test_forms_are_mutually_exclusive_and_validate_ownership_and_compatibility(self) -> None:
        cases = (
            ((), "requires exactly one of --bundle or --seal"),
            (("--bundle", self.bundle["bundle_id"], "--seal", self.first["seal_id"]),
             "requires exactly one of --bundle or --seal"),
            (("--seal", self.first["seal_id"], "--reason", "x"), "requires --abandoned"),
            (("--seal", self.first["seal_id"], "--abandoned"), "missing required option"),
            (("--bundle", self.bundle["bundle_id"], "--reason", "x"),
             "does not accept --reason"),
        )
        for tail, expected in cases:
            with self.subTest(tail=tail):
                code, _output, error = self.run_cli(self.initiative_id, *tail)
                self.assertEqual(code, 2)
                self.assertIn(expected, error)

        incompatible = copy.deepcopy(self.bundle)
        incompatible.update({
            "bundle_id": str(uuid.uuid4()),
            "state": "incompatible",
            "outcome": "incompatible",
        })
        self.store.save_bundle(self.initiative_id, incompatible)
        code, _output, error = self.run_cli(
            self.initiative_id, "--bundle", incompatible["bundle_id"],
        )
        self.assertEqual(code, 2)
        self.assertIn("compatible bundle", error)

        other = self.create_initiative("other-initiative")
        code, _output, error = self.run_cli(
            other["initiative_id"], "--seal", self.first["seal_id"],
            "--abandoned", "--reason", "not this initiative",
        )
        self.assertEqual(code, 2)
        self.assertIn("does not belong to initiative", error)

    def test_coordinator_session_cannot_record_an_operator_attestation(self) -> None:
        before = len(self.store.list_events_snapshot(self.initiative_id))
        fenced_env = {
            **self.env,
            "ASHA_ORCHESTRATION_COORDINATOR_ID": str(uuid.uuid4()),
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([
                "initiative", "record-integration", self.initiative_id,
                "--bundle", self.bundle["bundle_id"], "--json",
            ], env=fenced_env)
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("refused inside a coordinator session", stderr.getvalue())
        self.assertEqual(len(self.store.list_events_snapshot(self.initiative_id)), before)


if __name__ == "__main__":
    unittest.main()
