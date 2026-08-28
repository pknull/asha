"""Operator integration attestations and their durable lifecycle projection."""

from __future__ import annotations

import copy
import io
import json
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from lib.control.orchestration.actions import build_action_document, submit_action
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
        if "while_running" in self._testMethodName:
            original_save = self.store.save_initiative

            def stop_before_readiness(record, *, expected_digest=None):
                if record["state"] == "ready-for-integration":
                    raise RuntimeError("keep the compatible bundle in running state")
                return original_save(record, expected_digest=expected_digest)

            with mock.patch.object(
                self.store, "save_initiative", side_effect=stop_before_readiness,
            ), self.assertRaisesRegex(RuntimeError, "running state"):
                bind_readiness(self.store, self.initiative_id)
            self.bundle = self.store.list_bundles_snapshot(self.initiative_id)[0]
        else:
            self.bundle = bind_readiness(self.store, self.initiative_id)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["initiative", "record-integration", *args], env=self.env)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_compatible_bundle_records_every_member_and_advances_to_integrated(self) -> None:
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
        self.assertEqual(self.initiative()["state"], "integrated")
        self.assertEqual(record_digest(self.store.read_bundle(
            self.initiative_id, self.bundle["bundle_id"],
        )), before_bundle)
        transitions = [
            item for item in self.store.list_events_snapshot(self.initiative_id)
            if item["type"] == "initiative-state-changed"
            and item["payload"].get("to") == "integrated"
        ]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(
            (transitions[0]["actor_kind"], transitions[0]["actor_id"]),
            ("operator", "cli"),
        )
        self.assertEqual(transitions[0]["payload"], {
            "from": "ready-for-integration", "to": "integrated",
        })
        self.assertEqual(
            transitions[0]["subject_ids"],
            [self.initiative_id, self.bundle["bundle_id"]],
        )
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before_events + 2,
        )

        again_code, again_output, again_error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )
        self.assertEqual((again_code, again_error), (0, ""))
        self.assertEqual(json.loads(again_output)["event_id"], event["event_id"])
        self.assertEqual(self.initiative()["state"], "integrated")
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before_events + 2,
        )

    def test_integrated_initiative_archives_and_unarchives_to_integrated(self) -> None:
        code, _output, error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )
        self.assertEqual((code, error), (0, ""))

        archived = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "archive", {}),
        )
        self.assertEqual(archived["state"], "completed")
        self.assertEqual(self.initiative()["state"], "archived")

        restored = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "unarchive", {}),
        )
        self.assertEqual(restored["state"], "completed")
        self.assertEqual(self.initiative()["state"], "integrated")

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
        self.assertEqual(self.initiative()["state"], "ready-for-integration")

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
        state_before = self.initiative()
        events_before = len(self.store.list_events_snapshot(self.initiative_id))
        code, _output, error = self.run_cli(
            self.initiative_id, "--bundle", incompatible["bundle_id"],
        )
        self.assertEqual(code, 2)
        self.assertIn("compatible bundle", error)
        self.assertEqual(self.initiative(), state_before)
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), events_before,
        )

        other = self.create_initiative("other-initiative")
        other_before = self.store.peek(other["initiative_id"])
        code, _output, error = self.run_cli(
            other["initiative_id"], "--seal", self.first["seal_id"],
            "--abandoned", "--reason", "not this initiative",
        )
        self.assertEqual(code, 2)
        self.assertIn("does not belong to initiative", error)

        # Form A must reject a bundle of another initiative for the same reason
        # a foreign seal is rejected: ownership, not merely compatibility.
        before = len(self.store.list_events_snapshot(other["initiative_id"]))
        code, _output, error = self.run_cli(
            other["initiative_id"], "--bundle", self.bundle["bundle_id"],
        )
        self.assertEqual(code, 2)
        self.assertIn("does not belong to initiative", error)
        self.assertEqual(
            len(self.store.list_events_snapshot(other["initiative_id"])), before,
        )
        self.assertEqual(self.store.peek(other["initiative_id"]), other_before)

    def record_digests(self) -> dict[str, object]:
        return {
            "bundles": [
                record_digest(item)
                for item in self.store.list_bundles_snapshot(self.initiative_id)
            ],
            "seals": [
                record_digest(item)
                for item in self.store.list_seals_snapshot(self.initiative_id)
            ],
            "attempts": [
                record_digest(item)
                for item in self.store.list_attempts_snapshot(self.initiative_id)
            ],
            "nodes": [
                record_digest(item)
                for item in self.store.list_nodes_snapshot(self.initiative_id)
            ],
        }

    def test_neither_form_touches_a_repository_or_candidate_records(self) -> None:
        """Neither form runs a process or changes candidate records.

        Every repository mutation this codebase can make -- merge, rebase,
        bookmark move, push -- is an external command, so an attempted spawn is
        the observable proof that a verb stopped being an attestation.  Each
        form gets a fresh initiative because recording either disposition
        refuses the other for the same seal.
        """
        def spawned(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("record-integration must not run an external command")

        for form in ("bundle", "seal"):
            with self.subTest(form=form):
                self.setUp()
                tail = (
                    ["--bundle", self.bundle["bundle_id"]] if form == "bundle"
                    else [
                        "--seal", self.first["seal_id"], "--abandoned",
                        "--reason", "Recorded without integrating.",
                    ]
                )
                before = self.record_digests()
                before_events = len(self.store.list_events_snapshot(self.initiative_id))
                with mock.patch("subprocess.Popen", side_effect=spawned), mock.patch(
                    "subprocess.run", side_effect=spawned,
                ):
                    code, _output, error = self.run_cli(
                        self.initiative_id, *tail, "--json",
                    )
                self.assertEqual((code, error), (0, ""))
                # Candidate records stand exactly as they were. Bundle
                # integration also records the lifecycle transition; seal
                # abandonment remains a single state-neutral fact.
                self.assertEqual(self.record_digests(), before)
                expected_state = "integrated" if form == "bundle" else "ready-for-integration"
                self.assertEqual(self.initiative()["state"], expected_state)
                self.assertEqual(
                    len(self.store.list_events_snapshot(self.initiative_id)),
                    before_events + (2 if form == "bundle" else 1),
                )

    def test_bundle_fact_recorded_while_running_does_not_advance_state(self) -> None:
        self.assertEqual(self.initiative()["state"], "running")
        before_events = len(self.store.list_events_snapshot(self.initiative_id))

        code, output, error = self.run_cli(
            self.initiative_id, "--bundle", self.bundle["bundle_id"], "--json",
        )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["payload"]["disposition"], "integrated")
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), before_events + 1,
        )
        self.assertFalse(any(
            item["type"] == "initiative-state-changed"
            and item["payload"].get("to") == "integrated"
            for item in self.store.list_events_snapshot(self.initiative_id)
        ))

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
