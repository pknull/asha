from __future__ import annotations

import copy
import json
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.control.orchestration.actions import (
    _parse_document, build_action_document, submit_action,
)
from lib.control.orchestration.cli import _create, propose_plan
from lib.control.orchestration.model import (
    ATTEMPT_CONTRACT,
    MAX_ARGV_ITEMS,
    MAX_ARG_BYTES,
    MAX_ATTESTATIONS,
    MAX_PATH_BYTES,
    MAX_SUMMARY_BYTES,
    MAX_ACCEPTANCE_BYTES,
    MAX_CRITERION_BYTES,
    MAX_GOAL_BYTES,
    MAX_OBJECTIVE_BYTES,
    MAX_DEPENDENCIES,
    MAX_PATH_ITEMS,
    MAX_SEAL_INPUTS,
    MAX_FINDINGS,
    MAX_FINDING_BYTES,
    validate_initiative,
    validate_node,
    record_digest,
)
from lib.control.orchestration.scheduler import (
    MAX_ASSIGNMENT_BYTES,
    SchedulerError,
    _goal,
    assignment_bytes,
    consecutive_failures,
    dispatch,
    pause_for_breaker,
    readiness,
    validate_goal_capacity,
)
from lib.control.orchestration.store import ObservationOnlyPlanError
from lib.control.orchestration.verification import DENIED_COMMAND_PROGRAMS
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_workspace_fixtures import WorkspaceFixture


class OrchestrationSchedulerTests(ExecutionFixture, unittest.TestCase):
    def attempt(self, state: str = "running", ordinal: int = 1) -> dict:
        node = self.store.read_node(self.initiative_id, "implementation-a")
        at = now_text()
        return {
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node["node_id"],
            "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()),
            "ordinal": ordinal,
            "base": copy.deepcopy(node["base"]),
            "state": state,
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": at,
            "updated_at": at,
        }

    def update_limits(self, **changes) -> None:
        initiative = self.initiative()
        updated = copy.deepcopy(initiative)
        updated["limits"].update(changes)
        updated["state_revision"] += 1
        updated["updated_at"] = now_text()
        self.store.save_initiative(
            updated, expected_digest=record_digest(initiative),
        )

    def test_readiness_is_dependency_and_limit_deterministic(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            first = readiness(self.store, self.initiative())
            second = readiness(self.store, self.initiative())
        self.assertEqual(first, second)
        self.assertEqual(first, {
            "implementation-a": "ready",
            "review-a": "blocked",
            "verify-a": "blocked",
        })

    def test_direct_work_and_review_dispatch_refuse_historical_plan_before_preparation(self) -> None:
        self.install_historical_active_plan()
        before_attempts = self.store.list_attempts_snapshot(self.initiative_id)

        for node_id in ("implementation-a", "review-a"):
            with self.subTest(node_id=node_id):
                document = build_action_document(
                    self.initiative(), "dispatch-node", {"node_id": node_id},
                )
                action, _ = _parse_document(document)
                with self.assertRaises(ObservationOnlyPlanError), mock.patch(
                    "lib.control.orchestration.scheduler.capture_bytes",
                ) as launch, mock.patch.object(
                    self.store, "save_attempt",
                ) as save_attempt:
                    dispatch(
                        self.store, self.config, self.initiative_id, node_id,
                        action=action,
                    )
                launch.assert_not_called()
                save_attempt.assert_not_called()
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id), before_attempts,
        )

    def test_assignment_keeps_all_accepted_text_in_full(self):
        initiative = self.initiative()
        initiative["objective"] = "objective:" + "é" * 2500
        initiative["acceptance_criteria"] = [f"criterion {i}:" + "c" * 1800 for i in range(3)]
        node = self.store.read_node(self.initiative_id, "implementation-a")
        node["goal"] = "g" * 3500
        node["acceptance"] = "a" * 3500
        attempt = self.attempt()
        rendered = assignment_bytes(
            initiative, self.plan, node, attempt, attempt["base"]["scope_origin"]["jj_commit_id"],
        ).decode()
        for value in [initiative["objective"], *initiative["acceptance_criteria"], node["goal"], node["acceptance"]]:
            self.assertIn(value, rendered)
        self.assertNotIn("[truncated", rendered)

    def size_proposal(self, *, oversized):
        initiative = _create([
            "--repo", str(self.repo), "--slug", "size-probe", "--label", "Size probe",
            "--objective", "Check assignment capacity.",
        ], self.config, self.store, self.jj)["initiative"]
        updated = copy.deepcopy(initiative)
        updated["acceptance_criteria"] = [
            f"criterion {i}:" + "é" * 1000 for i in range(16 if oversized else 1)
        ]
        updated["state_revision"] += 1
        self.store.save_initiative(updated, expected_digest=record_digest(initiative))
        plan = copy.deepcopy(self.plan)
        plan["initiative_id"] = initiative["initiative_id"]
        plan["digest"] = None
        plan["nodes"][0]["goal"] = "g" * 3500
        return self.store.peek(initiative["initiative_id"]), plan

    def test_oversize_assignment_is_refused_before_proposal_with_node_and_byte_counts(self):
        initiative, plan = self.size_proposal(oversized=True)
        node = plan["nodes"][0]
        attempt = {"attempt_id": "00000000-0000-4000-8000-000000000000", "base": node["base"]}
        with self.assertRaises(SchedulerError) as capacity:
            validate_goal_capacity(self.config, initiative, plan)
        required_bytes = int(re.search(r"is (\d+) bytes", str(capacity.exception))[1])
        self.assertGreater(required_bytes, MAX_ASSIGNMENT_BYTES)
        with self.assertRaises(SchedulerError) as refused:
            propose_plan(self.store, initiative, plan, config=self.config, jj=self.jj)
        self.assertEqual(str(refused.exception), str(capacity.exception))
        for detail in (node["node_id"], str(required_bytes), str(MAX_ASSIGNMENT_BYTES)):
            self.assertIn(detail, str(refused.exception))
        # Retained model-valid text remains readable, but cannot be dispatched.
        validate_initiative(initiative)
        validate_node(node)
        with self.assertRaisesRegex(SchedulerError, rf"{node['node_id']}.*bytes.*{MAX_ASSIGNMENT_BYTES}"):
            assignment_bytes(
                initiative, plan, node, attempt, node["base"]["scope_origin"]["jj_commit_id"],
            )
        self.assertIn("shorten", str(refused.exception))
        self.assertEqual(self.store.list_plans_snapshot(initiative["initiative_id"]), [])
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "draft")

    def test_goal_capacity_uses_the_selected_store_artifact_path(self):
        path = Path("/tmp") / ("x" * 180) / "00000000-0000-4000-8000-000000000000.md"
        with mock.patch.object(self.store, "assignment_path", return_value=path):
            with self.assertRaisesRegex(SchedulerError, "200-character"):
                validate_goal_capacity(self.config, self.initiative(), self.plan, store=self.store)

    def capacity_case(self, node_type="work", *, interactive=True):
        """Find the actual public validation boundary, without template copies."""
        initiative = self.initiative()
        initiative["objective"] = "O" * MAX_OBJECTIVE_BYTES
        plan = copy.deepcopy(self.plan)
        node = copy.deepcopy(plan["nodes"][0 if node_type != "review" else 1])
        node.update(type=node_type, goal="G" * (MAX_GOAL_BYTES - 1),
                    acceptance="A" * MAX_ACCEPTANCE_BYTES, interactive=interactive)
        if node_type == "compose":
            node["conflict_policy"] = "fail-on-conflict"
        if node_type == "research":
            node["base"] = None
            node["node_id"] = "research-a"
        plan["nodes"] = [n for n in plan["nodes"] if n["node_id"] != node["node_id"]] + [node]

        def criteria(size):
            initiative["acceptance_criteria"] = [
                str(index) + "C" * (min(MAX_CRITERION_BYTES, size - offset) - 1)
                for index, offset in enumerate(range(0, size, MAX_CRITERION_BYTES))
            ]

        low, high = 1, 8 * MAX_CRITERION_BYTES
        while low < high:
            middle = (low + high + 1) // 2
            criteria(middle)
            try:
                validate_goal_capacity(self.config, initiative, plan)
            except SchedulerError:
                high = middle - 1
            else:
                low = middle
        criteria(low)
        validate_initiative(initiative)
        validate_node(node)
        validate_goal_capacity(self.config, initiative, plan)
        return initiative, plan, node

    def render_case(self, initiative, plan, node, seals=None, findings=None):
        attempt = self.attempt()
        # Research and review resolve this same approved repository at dispatch.
        if node["base"] is not None:
            attempt["base"] = copy.deepcopy(node["base"])
        return assignment_bytes(
            initiative, plan, node, attempt, "d" * 64, seals, findings,
        )

    def large_seals(self, *, read_only=False):
        # Saturate every auxiliary cap, with maximum list counts and maximum
        # individual text/path sizes. Exact review binding lists also reach
        # their model maximum and must NOT be truncated with the evidence.
        paths = ["p" * MAX_PATH_BYTES] + [f"path-{i}" for i in range(MAX_PATH_ITEMS - 1)]
        origin = self.attempt()["base"]["scope_origin"]
        return [{
            "seal_id": str(uuid.UUID(int=index + 1)), "read_only": read_only,
            "outcome": "failure" if read_only else "success",
            "scope_origin": origin,
            "jj_commit_id": "d" * 64, "diff_digest": "e" * 64,
            "tree_digest": "f" * 64,
            "base_seal_ids": [str(uuid.UUID(int=i + 1000)) for i in range(MAX_SEAL_INPUTS)],
            "changed_paths": paths, "cumulative_changed_paths": paths,
            "result": {"summary": "é" * (MAX_SUMMARY_BYTES // 2)},
        } for index in range(MAX_SEAL_INPUTS)]

    def assert_required_text(self, raw, initiative, node):
        self.assertLessEqual(len(raw), MAX_ASSIGNMENT_BYTES)
        text = raw.decode("utf-8")
        for value in (initiative["objective"], node["goal"], node["acceptance"]):
            self.assertIn(value, text)
        for criterion in initiative["acceptance_criteria"]:
            self.assertIn(json.dumps(criterion, ensure_ascii=False), text)

    def test_every_assignment_type_just_fits_and_one_byte_over_is_refused(self):
        for node_type in ("work", "research", "compose", "review"):
            for interactive in (True, False):
                with self.subTest(node_type=node_type, interactive=interactive):
                    initiative, plan, node = self.capacity_case(node_type, interactive=interactive)
                    seals = self.large_seals() if node_type in {"review", "compose"} else []
                    raw = self.render_case(initiative, plan, node, seals)
                    self.assert_required_text(raw, initiative, node)
                    if node_type == "review":
                        target = next(json.loads(line) for line in raw.decode().splitlines()
                                      if line.startswith('{"active_plan_digest"'))
                        self.assertEqual(target["base_seal_ids"], seals[0]["base_seal_ids"])
                        self.assertEqual(target["jj_commit_id"], seals[0]["jj_commit_id"])
                    elif node_type == "compose":
                        self.assertIn(json.dumps([seal["seal_id"] for seal in seals]), raw.decode())
                    node["goal"] += "G"
                    validate_node(node)
                    with self.assertRaises(SchedulerError) as refused:
                        validate_goal_capacity(self.config, initiative, plan)
                    for detail in (node["node_id"], str(MAX_ASSIGNMENT_BYTES + 1), str(MAX_ASSIGNMENT_BYTES)):
                        self.assertIn(detail, str(refused.exception))

    def test_maximum_auxiliary_sections_fit_in_ordinary_read_only_and_repair_layouts(self):
        findings = [{
            "severity": "high", "location": f"file-{i}",
            "summary": "é" * (MAX_FINDING_BYTES // 2),
        } for i in range(MAX_FINDINGS)]
        for layout in ("ordinary", "read-only", "repair"):
            with self.subTest(layout=layout):
                initiative, plan, node = self.capacity_case()
                # Auxiliary stress data is not a replacement plan graph.
                node = copy.deepcopy(node)
                node["hard_write_scope"] = ["p" * MAX_PATH_BYTES] + [f"p{i}" for i in range(MAX_PATH_ITEMS - 1)]
                node["advisory_path_ownership"] = list(node["hard_write_scope"])
                node["dependencies"] = [f"d{i:02}" + "x" * 61 for i in range(MAX_DEPENDENCIES)]
                validate_node(node)
                validate_goal_capacity(self.config, initiative, plan)
                raw = self.render_case(
                    initiative, plan, node, self.large_seals(read_only=layout == "read-only"),
                    findings if layout == "repair" else None,
                )
                self.assert_required_text(raw, initiative, node)
                # The real dispatch render spends the tiny remaining budget,
                # rather than reserving thousands of bytes per evidence list.
                self.assertGreater(len(raw), MAX_ASSIGNMENT_BYTES - 100)

    def test_utf8_capacity_and_dispatch_required_only_boundary(self):
        initiative, plan, node = self.capacity_case()
        ascii_goal = node["goal"]
        node["goal"] = "é" + ascii_goal[2:]
        validate_goal_capacity(self.config, initiative, plan)
        self.assert_required_text(self.render_case(initiative, plan, node), initiative, node)
        node["goal"] = "é" + ascii_goal[1:]
        with self.assertRaisesRegex(SchedulerError, rf"{node['node_id']}.*{MAX_ASSIGNMENT_BYTES + 1} bytes"):
            validate_goal_capacity(self.config, initiative, plan)

        # Approval reserves future framing. A retained dispatch uses its actual
        # framing: every required byte still fits even beyond that reservation.
        node["goal"] = ascii_goal
        criterion = initiative["acceptance_criteria"][-1]
        last_fitting = None
        for extra in range(1, 100):
            initiative["acceptance_criteria"][-1] = criterion + "Z" * extra
            try:
                last_fitting = self.render_case(initiative, plan, node)
            except SchedulerError as exc:
                self.assertIsNotNone(last_fitting)
                self.assertEqual(len(last_fitting), MAX_ASSIGNMENT_BYTES)
                self.assertIn(str(MAX_ASSIGNMENT_BYTES + 1), str(exc))
                initiative["acceptance_criteria"][-1] = criterion + "Z" * (extra - 1)
                validate_initiative(initiative)
                self.assert_required_text(last_fitting, initiative, node)
                break
        else:
            self.fail("dispatch did not enforce its required-text byte boundary")

    def test_retained_model_maxima_remain_readable_when_dispatch_cannot_fit(self):
        original = self.initiative()
        retained = copy.deepcopy(original)
        retained["objective"] = "é" * (MAX_OBJECTIVE_BYTES // 2)
        retained["acceptance_criteria"] = [
            f"{i:02}" + "c" * (MAX_CRITERION_BYTES - 2) for i in range(16)
        ]
        retained["state_revision"] += 1
        self.store.save_initiative(retained, expected_digest=record_digest(original))
        node = self.store.read_node(self.initiative_id, "implementation-a")
        updated = copy.deepcopy(node)
        updated["node_id"] = "retained-maxima"
        updated["goal"] = "g" * MAX_GOAL_BYTES
        updated["acceptance"] = "a" * MAX_ACCEPTANCE_BYTES
        self.store.save_node(self.initiative_id, updated)
        retained = self.store.peek(self.initiative_id)
        updated = self.store.read_node(self.initiative_id, updated["node_id"])
        self.assertEqual(len(retained["objective"].encode()), MAX_OBJECTIVE_BYTES)
        self.assertEqual(updated["goal"], "g" * MAX_GOAL_BYTES)
        self.assertEqual(updated["acceptance"], "a" * MAX_ACCEPTANCE_BYTES)
        with self.assertRaisesRegex(SchedulerError, rf"{updated['node_id']}.*bytes.*{MAX_ASSIGNMENT_BYTES}"):
            self.render_case(retained, self.plan, updated)
        # Refusing a render does not mutate or invalidate the retained record.
        self.assertEqual(self.store.peek(self.initiative_id), retained)
        self.assertEqual(self.store.read_node(self.initiative_id, updated["node_id"]), updated)

    def test_gate_commands_keep_unicode_escaping_order_and_byte_boundary(self):
        initiative, plan, node = self.capacity_case()
        gate = plan["declared_gates"][1]
        gate["commands"] = [
            {"argv": ["python3", "-c", "print('雪')", 'quoted"\\é'],
             "cwd": "checks/é space", "timeout_seconds": 899},
            {"argv": ["./tests/check"], "cwd": ".", "timeout_seconds": 17},
        ]
        # Measure only through the production renderer; no copied template math.
        with self.assertRaises(SchedulerError) as refused:
            validate_goal_capacity(self.config, initiative, plan)
        required = int(re.search(r"is (\d+) bytes", str(refused.exception))[1])
        excess = required - MAX_ASSIGNMENT_BYTES
        initiative["acceptance_criteria"][-1] = initiative["acceptance_criteria"][-1][:-excess]
        validate_goal_capacity(self.config, initiative, plan)
        raw = self.render_case(initiative, plan, node).decode()
        facts = next(json.loads(line[2:]) for line in raw.splitlines()
                     if line.startswith('- {') and '"commands"' in line)
        self.assertEqual(facts["commands"], gate["commands"])
        self.assertEqual(facts["node_id"], gate["node_id"])
        gate["commands"][0]["argv"][-1] += "x"
        with self.assertRaisesRegex(SchedulerError, f"{MAX_ASSIGNMENT_BYTES + 1} bytes"):
            validate_goal_capacity(self.config, initiative, plan)

    def test_disconnected_gate_is_not_delivered_or_budgeted(self):
        initiative, plan, node = self.capacity_case()
        unrelated = copy.deepcopy(plan["nodes"][-1])
        unrelated.update(node_id="unrelated-gate", type="verify", dependencies=[], base=None)
        plan["nodes"].append(unrelated)
        unrelated_gate = copy.deepcopy(plan["declared_gates"][1])
        unrelated_gate.update(node_id=unrelated["node_id"], required=False)
        unrelated_gate["commands"][0]["argv"].append("é" * 2000)
        plan["declared_gates"].append(unrelated_gate)
        validate_goal_capacity(self.config, initiative, plan)
        raw = self.render_case(initiative, plan, node).decode()
        self.assertNotIn("unrelated-gate", raw)
        self.assertIn('"node_id": "verify-a"', raw)

    def test_fitting_assignment_still_proposes(self):
        initiative, plan = self.size_proposal(oversized=False)
        proposed = propose_plan(self.store, initiative, plan, config=self.config, jj=self.jj)
        self.assertEqual(proposed["nodes"][0]["goal"], "g" * 3500)
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "awaiting-plan-approval")

    def test_assignment_embeds_bounded_upstream_result_summary(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")
        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
            [{
                "seal_id": str(uuid.uuid4()), "outcome": "success",
                "read_only": False, "scope_origin": attempt["base"]["scope_origin"],
                "jj_commit_id": "d" * 40, "tree_digest": "e" * 64,
                "changed_paths": ["lib/change.py"],
                "cumulative_changed_paths": ["lib/change.py"],
                "result": {
                    "result_id": str(uuid.uuid4()), "payload_digest": "f" * 64,
                    "claim_status": "completed", "summary": "exact upstream work",
                    "concerns": [], "follow_up": [],
                },
            }],
        ).decode()
        self.assertIn("exact upstream work", rendered)
        self.assertIn("f" * 64, rendered)
        self.assertIn(
            "Do not run `jj status` or any other jj command that snapshots",
            rendered,
        )
        self.assertIn("The report receipt phase is `staged`", rendered)

    def test_assignment_documents_closed_verification_attestation_schema(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")

        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
        ).decode()

        key_line = next(
            line for line in rendered.splitlines()
            if line.startswith("Exact required element keys:")
        )
        self.assertEqual(
            re.findall(r"`([^`]+)`", key_line),
            ["argv", "cwd", "exit_code", "finished_at", "output_digest", "summary"],
        )
        self.assertIn(f"at most {MAX_ATTESTATIONS} elements", rendered)
        self.assertIn(f"at most {MAX_ARGV_ITEMS} unique text arguments", rendered)
        self.assertIn(f"1-{MAX_ARG_BYTES} UTF-8 bytes", rendered)
        self.assertIn(f"1-{MAX_PATH_BYTES} UTF-8 bytes", rendered)
        self.assertIn(f"1-{MAX_SUMMARY_BYTES} UTF-8 bytes", rendered)

    def test_assignment_states_controller_ingestion_rules_for_work_and_review(self) -> None:
        initiative = self.initiative()
        work = self.store.read_node(self.initiative_id, "implementation-a")
        review = self.store.read_node(self.initiative_id, "review-a")
        work_attempt = self.attempt(state="allocated")
        review_attempt = copy.deepcopy(work_attempt)
        review_attempt["node_id"] = review["node_id"]
        seal = {
            "seal_id": str(uuid.uuid4()),
            "outcome": "success",
            "read_only": False,
            "scope_origin": work_attempt["base"]["scope_origin"],
            "jj_commit_id": "d" * 40,
            "tree_digest": "e" * 64,
            "diff_digest": "f" * 64,
            "base_seal_ids": [],
        }

        rendered = (
            assignment_bytes(
                initiative, self.plan, work, work_attempt,
                work_attempt["base"]["scope_origin"]["jj_commit_id"],
            ).decode(),
            assignment_bytes(
                initiative, self.plan, review, review_attempt,
                seal["jj_commit_id"], [seal],
            ).decode(),
        )

        for assignment in rendered:
            with self.subTest(node="review" if "Independent review" in assignment else "work"):
                self.assertEqual(
                    assignment.count("## Controller-enforced result-ingestion rules"), 1,
                )
                section = assignment.split(
                    "## Controller-enforced result-ingestion rules", 1,
                )[1].split("The client document is", 1)[0]
                self.assertIn("run it from the repository root", section)
                self.assertIn("repository-relative executable", section)
                self.assertIn("interpreter plus a script", section)
                self.assertIn("no Unicode control, format, or surrogate", section)
                self.assertIn("Review-result contract", section)
                self.assertIn("A `pass` has no findings", section)
                for program in DENIED_COMMAND_PROGRAMS:
                    self.assertIn(f"`{program}`", section)

    def test_repair_assignment_explains_attempt_local_supersession(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")

        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
            accepted_findings=[{
                "severity": "high", "location": "results.py",
                "summary": "Repair the accepted result lineage defect.",
            }],
        ).decode()

        self.assertIn("## Accepted review findings to fix", rendered)
        self.assertIn(
            "supersedes_result_id MUST be null for the first result of this "
            "attempt, including a repair or salvage attempt that follows an "
            "earlier attempt; set it only to the result_id this same attempt "
            "already had accepted when publishing a correction.",
            rendered,
        )

    def test_parallel_total_deadline_pause_and_storage_limits_block(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.store.save_attempt(self.initiative_id, self.attempt())
            self.update_limits(max_parallel=1)
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

        # A pause state is itself a hard readiness gate.
        pause_for_breaker(self.store, self.initiative_id, "test breaker")
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.assertTrue(all(
                state != "ready" for state in readiness(self.store, self.initiative()).values()
            ))

    def test_storage_and_deadline_are_hard_gates(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": True},
        ):
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

    def test_total_and_per_node_attempt_caps_block_new_reservations(self) -> None:
        self.store.save_attempt(
            self.initiative_id, self.attempt("launch-failed"),
        )
        self.update_limits(max_total_tasks=1, max_attempts_per_node=1)
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            effective = readiness(self.store, self.initiative())
        self.assertEqual(effective["implementation-a"], "blocked")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        self.update_limits(deadline=past)
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

    def test_consecutive_failure_breaker_count_is_trailing_only(self) -> None:
        failures = [self.attempt("launch-failed", ordinal=index) for index in (1, 2, 3)]
        for index, attempt in enumerate(failures):
            attempt["created_at"] = attempt["updated_at"] = (
                datetime.now(timezone.utc) + timedelta(microseconds=index)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(consecutive_failures(failures), 3)
        self.assertEqual(
            consecutive_failures([*failures, self.attempt("allocated", ordinal=4)]),
            0,
        )

    def test_control_goal_elides_long_slug_and_preserves_absolute_assignment_path(self) -> None:
        initiative = {"slug": "s" * 40}
        node = {"node_id": "n" * 40}
        attempt_id = "11111111-1111-4111-8111-111111111111"
        assignment = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempt_id}.md"
        )
        goal = _goal(initiative, node, assignment)
        self.assertLessEqual(len(goal), 200)
        self.assertTrue(goal.startswith("orch "))
        self.assertIn(attempt_id, goal)
        self.assertTrue(goal.endswith(str(assignment)))
        slug_part = goal.removeprefix("orch ").split(" ", 1)[0]
        self.assertLessEqual(len(slug_part), 24)
        too_long = Path("/") / ("a" * 170) / f"{attempt_id}.md"
        with self.assertRaisesRegex(SchedulerError, "absolute assignment path"):
            _goal(initiative, node, too_long)

    def test_successful_dispatch_surfaces_delivery_preflight_stderr(self) -> None:
        diagnostic = (
            "Delivery preflight: untracked remote bookmarks at origin: release; "
            "remediate with: jj bookmark track NAME --remote=origin"
        )

        def capture(argv, **_kwargs):
            return 0, json.dumps(self.control_payload(argv)).encode(), diagnostic.encode()

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
        self.assertEqual(json.loads(action["outcome"])["diagnostic"], diagnostic)

    def test_storage_breaker_refuses_dispatch_and_pauses(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        capture = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": True},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(self.initiative()["state"], "paused")
        capture.assert_not_called()
        self.assertIn(
            "storage-threshold-reached",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

    def test_deadline_breaker_refuses_dispatch_and_pauses(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        self.update_limits(deadline=past)
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        capture = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(self.initiative()["state"], "paused")
        capture.assert_not_called()


class AssignmentWorkspaceGateTests(WorkspaceFixture, unittest.TestCase):
    def test_aggregate_gate_reaches_both_members_and_reviews(self):
        initiative = self.create_initiative()
        plan = self.approve_and_run(initiative, self.two_member_plan(initiative))
        for node in plan["nodes"]:
            if node["type"] == "verify":
                continue
            base = next(n["base"] for n in plan["nodes"]
                        if n["base"] is not None and n["repository_id"] == node["repository_id"])
            seals = [{"seal_id": str(uuid.uuid4()), "read_only": False,
                      "jj_commit_id": "a" * 40, "diff_digest": "b" * 64}]
            raw = assignment_bytes(initiative, plan, node,
                                   {"attempt_id": str(uuid.uuid4()), "base": base}, "c" * 40, seals).decode()
            fact = next(json.loads(line[2:]) for line in raw.splitlines()
                        if line.startswith('- {') and '"commands"' in line)
            self.assertEqual(fact["repository_id"], node["repository_id"])
            self.assertEqual(fact["gate_node_repository_id"], plan["nodes"][-1]["repository_id"])
            self.assertEqual(fact["commands"], plan["declared_gates"][-1]["commands"])


if __name__ == "__main__":
    unittest.main()
