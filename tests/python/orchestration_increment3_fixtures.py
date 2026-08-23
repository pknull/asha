from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from lib.control.orchestration.model import (
    EVIDENCE_CONTRACT,
    RESULT_CONTRACT,
    REVIEW_CONTRACT,
    VERIFICATION_CONTRACT,
    record_digest,
)
from lib.control.orchestration.review import review_target
from lib.control.orchestration.verification import candidate_bundle_digest
from tests.python.orchestration_execution_fixtures import now_text
from tests.python.test_orchestration_graph import seal as graph_seal


def advance_node(fixture, node_id: str, states: list[str]) -> dict:
    current = fixture.store.read_node(fixture.initiative_id, node_id)
    for state in states:
        changed = copy.deepcopy(current)
        changed["state"] = state
        fixture.store.save_node(
            fixture.initiative_id, changed,
            expected_digest=record_digest(current),
        )
        current = changed
    return current


def save_candidate(
    fixture, *, seal_id: str | None = None, outcome: str = "success",
) -> dict:
    candidate = graph_seal(seal_id or str(uuid.uuid4()))
    candidate.update({
        "initiative_id": fixture.initiative_id,
        "repository_id": fixture.initiative()["scope"]["repository"]["repository_id"],
        "node_id": "implementation-a",
        "sealed_at": now_text(),
        "outcome": outcome,
    })
    result = {
        "contract": RESULT_CONTRACT,
        "publication_id": str(uuid.uuid4()),
        "result_id": candidate["result_id"],
        "payload_digest": "a" * 64,
        "supersedes_result_id": None,
        "initiative_id": fixture.initiative_id,
        "node_id": candidate["node_id"],
        "attempt_id": candidate["attempt_id"],
        "task_id": candidate["task_id"],
        "run_id": candidate["run_id"],
        "claim_status": "completed",
        "summary": "Candidate completed.",
        "files_changed": list(candidate["changed_paths"]),
        "verification_attestations": [],
        "concerns": [], "follow_up": [],
        "published_at": now_text(),
    }
    fixture.store.save_result(fixture.initiative_id, result)
    fixture.store.save_seal(fixture.initiative_id, candidate)
    return candidate


def save_accepted_review(fixture, candidate: dict, *, verdict: str = "pass") -> dict:
    _seal, target = review_target(
        fixture.store, fixture.initiative(), fixture.plan,
        fixture.store.read_node(fixture.initiative_id, "review-a"),
    )
    findings = [] if verdict == "pass" else [{
        "severity": "high", "location": "lib/file.py",
        "summary": "Repair the exact candidate before verification.",
    }]
    review = {
        "contract": REVIEW_CONTRACT,
        "review_id": str(uuid.uuid4()),
        "initiative_id": fixture.initiative_id,
        "node_id": "review-a",
        "attempt_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "state": "accepted-pass" if verdict == "pass" else "accepted-findings",
        "target": target,
        "verdict": verdict,
        "findings": findings,
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    fixture.store.save_review(fixture.initiative_id, review)
    return review


def _identity_digest(workspace: str, change: str, commit: str, tree: str) -> str:
    raw = json.dumps(
        [workspace, change, commit, tree],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def save_passed_verification(fixture, candidate, *, node_id: str = "verify-a") -> dict:
    """Retain a passed controller verification for one candidate or an ordered member set.

    With several candidates the commands run member by member (scope order) and
    each member binding is retained as `verification-member` evidence, exactly as
    `prepare_verification_intent` records it.
    """
    candidates = [candidate] if isinstance(candidate, dict) else list(candidate)
    gate = next(
        item for item in fixture.plan["declared_gates"]
        if item["kind"] == "verification"
    )
    verification_id = str(uuid.uuid4())
    workspace_name = "asha-materialization-test"
    change_id = "k" * 32
    commit_id = "f" * 40
    bundle_digest = candidate_bundle_digest(
        fixture.initiative(), fixture.plan,
        candidates[0] if len(candidates) == 1 else candidates, gate,
    )
    commands: list[dict] = []
    evidence_ids: list[str] = []
    member_materializations: list[str] = []
    for index, member in enumerate(candidates):
        materialization_id = str(uuid.uuid4())
        member_materializations.append(materialization_id)
        if len(candidates) > 1:
            summary = json.dumps({
                "index": index, "verification_id": verification_id,
                "repository_id": member["repository_id"], "seal_id": member["seal_id"],
                "jj_commit_id": member["jj_commit_id"], "tree_digest": member["tree_digest"],
                "materialization_id": materialization_id,
                "materialization_name": f"verify-{fixture.initiative_id}-{verification_id[:8]}"
                + ("" if index == 0 else f"-{index}"),
                "materialization_path": str((fixture.root / f"materialization-{index}").resolve()),
                "source_root": "/tmp/source",
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fixture.store.save_evidence(fixture.initiative_id, {
                "contract": EVIDENCE_CONTRACT, "evidence_id": str(uuid.uuid4()),
                "initiative_id": fixture.initiative_id, "kind": "verification-member",
                "subject_id": verification_id,
                "digest": hashlib.sha256(summary.encode()).hexdigest(),
                "summary": summary, "recorded_at": now_text(),
            })
        identity = _identity_digest(workspace_name, change_id, commit_id, member["tree_digest"])
        for command_spec in gate["commands"]:
            evidence_id = str(uuid.uuid4())
            output = b"fixture verification output\n"
            output_path = fixture.store.save_output(fixture.initiative_id, evidence_id, output)
            started_at = now_text()
            finished_at = now_text()
            command = {
                "command_id": str(uuid.uuid4()),
                "argv": list(command_spec["argv"]),
                "cwd": command_spec["cwd"],
                "environment_policy_id": gate["environment_policy"],
                "timeout_seconds": command_spec["timeout_seconds"],
                "process_identity": "controller:test-command",
                "started_at": started_at,
                "finished_at": finished_at,
                "exit_code": 0,
                "signal": None,
                "timed_out": False,
                "output_path": str(output_path),
                "output_digest": hashlib.sha256(output).hexdigest(),
                "output_truncated": False,
                "output_original_bytes": len(output),
                "pre_identity_status": "observed",
                "post_identity_status": "observed",
                "pre_identity_digest": identity,
                "post_identity_digest": identity,
                "pre_jj_commit_id": commit_id,
                "pre_tree_digest": member["tree_digest"],
                "post_jj_commit_id": commit_id,
                "post_tree_digest": member["tree_digest"],
            }
            summary = json.dumps({
                "verification_id": verification_id,
                "bundle_digest": bundle_digest,
                "repository_id": member["repository_id"],
                "seal_id": member["seal_id"],
                "argv": command["argv"], "cwd": command["cwd"],
                "environment_policy_id": command["environment_policy_id"],
                "process_identity": command["process_identity"],
                "started_at": started_at, "finished_at": finished_at,
                "exit_code": 0, "signal": None, "timed_out": False,
                "denied": False, "mutation": False,
                "pre_identity_status": "observed",
                "post_identity_status": "observed",
                "pre_jj_commit_id": commit_id,
                "pre_tree_digest": member["tree_digest"],
                "post_jj_commit_id": commit_id,
                "post_tree_digest": member["tree_digest"],
                "output_digest": command["output_digest"],
                "output_path": command["output_path"],
                "output_truncated": False,
                "output_original_bytes": len(output),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fixture.store.save_evidence(fixture.initiative_id, {
                "contract": EVIDENCE_CONTRACT,
                "evidence_id": evidence_id,
                "initiative_id": fixture.initiative_id,
                "kind": "verification-command",
                "subject_id": verification_id,
                "digest": hashlib.sha256(summary.encode()).hexdigest(),
                "summary": summary,
                "recorded_at": now_text(),
            })
            commands.append(command)
            evidence_ids.append(evidence_id)
    verification = {
        "contract": VERIFICATION_CONTRACT,
        "verification_id": verification_id,
        "initiative_id": fixture.initiative_id,
        "node_id": node_id,
        "bundle_digest": bundle_digest,
        "active_plan_digest": fixture.plan["digest"],
        "repository_id": candidates[0]["repository_id"],
        "seal_id": candidates[0]["seal_id"],
        "materialization_id": member_materializations[0],
        "materialization_path": str((fixture.root / "materialization").resolve()),
        "state": "passed",
        "commands": commands,
        "evidence_ids": evidence_ids,
        "outcome": "passed",
        "created_at": now_text(),
        "updated_at": now_text(),
    }
    fixture.store.save_verification(fixture.initiative_id, verification)
    return verification
