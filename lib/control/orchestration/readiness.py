"""Exact-seal bundle binding, terminal outcomes, and retained archive."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .actions import append_event
from .model import (
    ACTION_TRANSITIONS,
    ATTEMPT_TERMINAL_STATES,
    BUNDLE_CONTRACT,
    INITIATIVE_TERMINAL_STATES,
    NODE_TERMINAL_STATES,
    VERIFICATION_TRANSITIONS,
    new_uuid,
    record_digest,
    validate_bundle,
    validate_initiative,
)
from .review import specification_digest
from .storage import storage_report
from .store import InitiativeStore, ObservationOnlyPlanError, StoreError
from .verification import candidate_bundle_digest


class ReadinessError(ValueError):
    """The exact candidate does not satisfy a terminal Core gate."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _qualification(
    store: InitiativeStore, initiative_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    initiative = store.peek(initiative_id)
    if initiative["active_plan"] is None:
        raise ReadinessError("initiative has no approved active plan")
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    from .model import scope_repositories
    from .verification import VerificationError, terminal_seals

    members = scope_repositories(initiative)
    try:
        member_seals = terminal_seals(store, initiative_id, plan, initiative)
    except VerificationError as exc:
        message = str(exc)
        if "success seal" in message:
            raise ReadinessError("terminal candidate has no qualifying success seal") from exc
        raise ReadinessError(
            "Core readiness requires one terminal candidate producer"
            if len(members) == 1 else message
        ) from exc
    seal = member_seals[0]
    review_gates = [
        item for item in plan["declared_gates"]
        if item["kind"] == "review" and item["required"] is True
    ]
    if len(members) == 1 and len(review_gates) != 1:
        raise ReadinessError("Core readiness requires one required review gate")
    if len(review_gates) < 1:
        raise ReadinessError("readiness requires at least one required review gate")
    review_gate_nodes = {item["node_id"] for item in review_gates}
    all_reviews = store.list_reviews_snapshot(initiative_id)
    member_reviews: list[dict[str, Any]] = []
    for member_seal in member_seals:
        matching = [
            item for item in all_reviews
            if item["state"] == "accepted-pass"
            and item["node_id"] in review_gate_nodes
            and item["target"]["seal_id"] == member_seal["seal_id"]
            and item["target"]["active_plan_digest"] == plan["digest"]
            and item["target"]["specification_digest"] == specification_digest(initiative, plan)
            and item["target"]["repository_id"] == member_seal["repository_id"]
            and item["target"]["jj_commit_id"] == member_seal["jj_commit_id"]
            and item["target"]["base_seal_ids"] == member_seal["base"]["seal_ids"]
            and item["target"]["diff_digest"] == member_seal["diff_digest"]
        ]
        if len(matching) != 1:
            raise ReadinessError("exact terminal seal lacks one accepted-pass review")
        member_reviews.append(matching[0])
    review = member_reviews[0]
    gates = [
        item for item in plan["declared_gates"]
        if item["kind"] == "verification"
        and item["required"] is True
    ]
    if len(gates) != 1:
        raise ReadinessError("approved verification gate is missing")
    verification_node = next(
        (
            item for item in plan["nodes"]
            if item["node_id"] == gates[0]["node_id"] and item["type"] == "verify"
        ),
        None,
    )
    if verification_node is None:
        raise ReadinessError("required verification gate has no verify node")
    expected_bundle_digest = candidate_bundle_digest(
        initiative, plan, member_seals, gates[0],
    )
    verifications = [
        item for item in store.list_verifications_snapshot(initiative_id)
        if item["state"] == "passed" and item["outcome"] == "passed"
        and item["node_id"] == verification_node["node_id"]
        and item["seal_id"] == seal["seal_id"]
        and item["repository_id"] == seal["repository_id"]
        and item["active_plan_digest"] == plan["digest"]
        and item["bundle_digest"] == expected_bundle_digest
    ]
    if len(verifications) != 1:
        raise ReadinessError("exact terminal seal lacks one passed controller verification")
    verification = verifications[0]
    commands = verification["commands"]
    if (
        len(commands) != len(gates[0]["commands"]) * len(member_seals)
        or len(verification["evidence_ids"]) != len(commands)
    ):
        raise ReadinessError("passed verification lacks complete per-command evidence")
    # Commands run member by member in scope order; each member's commands
    # bind that member's seal identity.
    expected_specs = [
        (member_seal, specification)
        for member_seal in member_seals
        for specification in gates[0]["commands"]
    ]
    for (seal, specification), command, evidence_id in zip(
        expected_specs, commands, verification["evidence_ids"],
    ):
        if (
            command["argv"] != specification["argv"]
            or command["cwd"] != specification["cwd"]
            or command["timeout_seconds"] != specification["timeout_seconds"]
            or command["environment_policy_id"] != gates[0]["environment_policy"]
            or command["exit_code"] != 0
            or command["signal"] is not None
            or command["timed_out"]
            or command["pre_identity_status"] != "observed"
            or command["post_identity_status"] != "observed"
            or command["pre_identity_digest"] != command["post_identity_digest"]
            or command["pre_jj_commit_id"] != command["post_jj_commit_id"]
            or command["pre_tree_digest"] != seal["tree_digest"]
            or command["post_tree_digest"] != seal["tree_digest"]
        ):
            raise ReadinessError("passed verification command evidence is not exact")
        try:
            evidence = store.read_evidence(initiative_id, evidence_id)
            summary = json.loads(evidence["summary"])
        except (StoreError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ReadinessError(
                f"verification command evidence cannot be read: {exc}"
            ) from exc
        expected_output_path = (
            store.config.initiatives_dir / initiative_id / "outputs"
            / f"{evidence_id}.bin"
        )
        if (
            evidence["kind"] != "verification-command"
            or evidence["subject_id"] != verification["verification_id"]
            or not isinstance(summary, dict)
            or summary.get("verification_id") != verification["verification_id"]
            or summary.get("bundle_digest") != verification["bundle_digest"]
            or summary.get("repository_id") != seal["repository_id"]
            or summary.get("seal_id") != seal["seal_id"]
            or summary.get("argv") != command["argv"]
            or summary.get("cwd") != command["cwd"]
            or summary.get("environment_policy_id") != command["environment_policy_id"]
            or summary.get("process_identity") != command["process_identity"]
            or summary.get("started_at") != command["started_at"]
            or summary.get("finished_at") != command["finished_at"]
            or summary.get("exit_code") != command["exit_code"]
            or summary.get("signal") != command["signal"]
            or summary.get("timed_out") != command["timed_out"]
            or summary.get("denied") is not False
            or summary.get("mutation") is not False
            or summary.get("pre_identity_status") != command["pre_identity_status"]
            or summary.get("post_identity_status") != command["post_identity_status"]
            or summary.get("pre_jj_commit_id") != command["pre_jj_commit_id"]
            or summary.get("pre_tree_digest") != command["pre_tree_digest"]
            or summary.get("post_jj_commit_id") != command["post_jj_commit_id"]
            or summary.get("post_tree_digest") != command["post_tree_digest"]
            or summary.get("output_digest") != command["output_digest"]
            or summary.get("output_path") != command["output_path"]
            or summary.get("output_truncated") != command["output_truncated"]
            or summary.get("output_original_bytes") != command["output_original_bytes"]
            or Path(command["output_path"]) != expected_output_path
        ):
            raise ReadinessError("immutable command evidence differs from the verification record")
        try:
            output_metadata = expected_output_path.lstat()
            if (
                not stat.S_ISREG(output_metadata.st_mode)
                or stat.S_ISLNK(output_metadata.st_mode)
                or output_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(output_metadata.st_mode) != 0o600
                or output_metadata.st_size > 1024 * 1024
            ):
                raise ReadinessError("verification output artifact is not private and bounded")
            output = expected_output_path.read_bytes()
        except (OSError, ValueError, TypeError) as exc:
            raise ReadinessError(
                f"verification output artifact cannot be read: {exc}"
            ) from exc
        if (
            hashlib.sha256(output).hexdigest() != command["output_digest"]
            or command["output_original_bytes"] < len(output)
            or (command["output_truncated"] and b"truncated" not in output)
        ):
            raise ReadinessError("verification output artifact digest differs from evidence")
    return initiative, plan, seal, review, verification


def _qualified_members(
    store: InitiativeStore, initiative_id: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """All member seals and their reviews, after the full single-member qualification."""
    initiative, plan, _seal, _review, verification = _qualification(store, initiative_id)
    from .verification import terminal_seals, verification_members

    member_seals = terminal_seals(store, initiative_id, plan, initiative)
    bound = verification_members(store, initiative_id, verification["verification_id"])
    if not bound:
        # Increment 3 single-member verifications carry the binding on the record itself.
        if len(member_seals) != 1 or verification["seal_id"] != member_seals[0]["seal_id"]:
            raise ReadinessError("verification member bindings differ from the terminal seal set")
    elif [item["seal_id"] for item in bound] != [item["seal_id"] for item in member_seals]:
        raise ReadinessError("verification member bindings differ from the terminal seal set")
    review_gate_nodes = {
        item["node_id"] for item in plan["declared_gates"]
        if item["kind"] == "review" and item["required"] is True
    }
    reviews = []
    for member_seal in member_seals:
        reviews.append(next(
            item for item in store.list_reviews_snapshot(initiative_id)
            if item["state"] == "accepted-pass" and item["node_id"] in review_gate_nodes
            and item["target"]["seal_id"] == member_seal["seal_id"]
        ))
    return initiative, plan, member_seals, reviews, verification


def bind_readiness(
    store: InitiativeStore, initiative_id: str,
) -> dict[str, Any]:
    """Bind a one-member compatible bundle and stop before integration."""
    initiative, plan, member_seals, reviews, verification = _qualified_members(store, initiative_id)
    seal = member_seals[0]
    if initiative["state"] not in {"running", "ready-for-integration"}:
        raise ReadinessError("only a running initiative may become ready-for-integration")
    from .verification import verification_members

    bound_members = verification_members(store, initiative_id, verification["verification_id"])
    materialization_ids = {item["seal_id"]: item["materialization_id"] for item in bound_members}
    members = [
        {
            "repository_id": member_seal["repository_id"],
            "seal_id": member_seal["seal_id"],
            "jj_commit_id": member_seal["jj_commit_id"],
            "tree_digest": member_seal["tree_digest"],
            "diff_digest": member_seal["diff_digest"],
            "materialization_id": materialization_ids.get(
                member_seal["seal_id"], verification["materialization_id"],
            ),
            "review_id": review["review_id"],
            "verification_id": verification["verification_id"],
        }
        for member_seal, review in zip(member_seals, reviews)
    ]
    seal_ids = {item["seal_id"] for item in members}
    aggregate_spec_digest = specification_digest(initiative, plan)
    existing = [
        item for item in store.list_bundles_snapshot(initiative_id)
        if {entry["seal_id"] for entry in item["members"]} == seal_ids
    ]
    if len(existing) > 1:
        raise ReadinessError("candidate seal has multiple retained bundle proposals")
    if existing:
        binding = existing[0]
        if (
            binding["initiative_id"] != initiative_id
            or binding["aggregate_spec_digest"] != aggregate_spec_digest
            or binding["active_plan_digest"] != plan["digest"]
            or binding["members"] != members
            or binding["state"] not in {"binding", "compatible"}
        ):
            raise ReadinessError("retained candidate bundle binding changed")
    else:
        binding = validate_bundle({
            "contract": BUNDLE_CONTRACT,
            "bundle_id": new_uuid(),
            "initiative_id": initiative_id,
            "aggregate_spec_digest": aggregate_spec_digest,
            "active_plan_digest": plan["digest"],
            "state": "binding",
            "members": members,
            "controller_evidence_ids": [],
            "outcome": None,
            "bound_at": None,
        })
        store.save_bundle(initiative_id, binding)
    if binding["state"] == "binding":
        bundle = copy.deepcopy(binding)
        bundle.update({
            "state": "compatible",
            "controller_evidence_ids": list(verification["evidence_ids"]),
            "outcome": "compatible",
            "bound_at": _now(),
        })
        validate_bundle(bundle)
        store.save_bundle(
            initiative_id, bundle, expected_digest=record_digest(binding),
        )
    else:
        bundle = binding
        if (
            bundle["outcome"] != "compatible"
            or bundle["controller_evidence_ids"] != verification["evidence_ids"]
            or bundle["bound_at"] is None
        ):
            raise ReadinessError("retained compatible bundle evidence changed")
    current = store.peek(initiative_id)
    if current["state"] == "running":
        changed = copy.deepcopy(current)
        changed.update({
            "state": "ready-for-integration",
            "state_revision": current["state_revision"] + 1,
            "updated_at": _now(),
        })
        validate_initiative(changed)
        store.save_initiative(changed, expected_digest=record_digest(current))
    elif current["state"] != "ready-for-integration":
        raise ReadinessError("initiative changed while binding readiness")
    if not any(
        item["type"] == "initiative-state-changed"
        and item["payload"].get("to") == "ready-for-integration"
        and item["payload"].get("bundle_id") == bundle["bundle_id"]
        for item in store.list_events_snapshot(initiative_id)
    ):
        append_event(
            store, initiative_id, "initiative-state-changed",
            [initiative_id, bundle["bundle_id"]],
            {
                "from": "running", "to": "ready-for-integration",
                "bundle_id": bundle["bundle_id"], "seal_id": seal["seal_id"],
            },
            actor_kind="controller", actor_id="readiness-gate",
        )
    return bundle


def prevalidate_finalization(
    store: InitiativeStore,
    initiative_id: str,
    outcome: str,
    reason: str,
    *,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Check every deterministic finalization condition without mutation."""
    if outcome not in {"partial", "failed"}:
        raise ReadinessError("finalize outcome must be partial or failed")
    if not isinstance(reason, str) or not reason or len(reason.encode("utf-8")) > 4096:
        raise ReadinessError("finalize reason must contain 1-4096 UTF-8 bytes")
    initiative = store.peek(initiative_id)
    attempts = store.list_attempts_snapshot(initiative_id)
    preactivation_abandonment = (
        initiative["state"] in {"awaiting-plan-approval", "approved"}
        and not attempts
    )
    # Abandonment proves the absence of live work, not a terminal plan graph.
    if (
        (
            initiative["active_plan"] is None
            and initiative["state"] in {"draft", "planning"}
        )
        or preactivation_abandonment
    ):
        for node in store.list_nodes_snapshot(initiative_id):
            if (
                node["state"] not in NODE_TERMINAL_STATES
                and not (
                    preactivation_abandonment
                    and node["state"] in {"proposed", "approved"}
                )
            ):
                raise ReadinessError(
                    f"abandonment blocked by non-terminal node {node['node_id']} "
                    f"in state {node['state']}"
                )
        for attempt in attempts:
            if attempt["state"] not in ATTEMPT_TERMINAL_STATES:
                raise ReadinessError(
                    f"abandonment blocked by non-terminal attempt {attempt['attempt_id']} "
                    f"in state {attempt['state']}"
                )
        for action in store.list_actions_snapshot(initiative_id):
            if (
                action["action_id"] != action_id
                and ACTION_TRANSITIONS[action["state"]]
            ):
                raise ReadinessError(
                    f"abandonment blocked by non-terminal action {action['action_id']} "
                    f"in state {action['state']}"
                )
        for verification in store.list_verifications_snapshot(initiative_id):
            if VERIFICATION_TRANSITIONS[verification["state"]]:
                raise ReadinessError(
                    "abandonment blocked by non-terminal verification "
                    f"{verification['verification_id']} in state {verification['state']}"
                )
        for approval in store.list_approvals_snapshot(initiative_id):
            if approval["action_class"] == "salvage" and approval["state"] == "approved":
                raise ReadinessError(
                    "abandonment blocked by approved salvage request "
                    f"{approval['request_id']}"
                )
        return initiative
    if initiative["state"] != "running":
        raise ReadinessError("only a running initiative may be finalized")
    nodes = store.list_nodes_snapshot(initiative_id)
    if any(item["state"] not in NODE_TERMINAL_STATES for item in nodes):
        raise ReadinessError("finalize requires a terminal graph")
    try:
        _qualification(store, initiative_id)
    except (ReadinessError, ObservationOnlyPlanError):
        pass
    else:
        raise ReadinessError("qualifying exact-seal evidence must bind readiness, not finalize")
    if outcome == "partial" and not any(
        seal["outcome"] == "success"
        for seal in store.list_seals_snapshot(initiative_id)
    ):
        raise ReadinessError("partial outcome requires useful retained success seal evidence")
    return initiative


def _latest_rejected_plan(
    store: InitiativeStore, initiative_id: str,
) -> dict[str, str]:
    plans = {
        plan["digest"]: plan
        for plan in store.list_plans_snapshot(initiative_id)
    }
    rejected = [
        event for event in store.list_events_snapshot(initiative_id)
        if event["type"] == "plan-rejected"
        and event["payload"].get("digest") in plans
    ]
    if not rejected:
        return {}
    latest = max(
        rejected,
        key=lambda event: (
            plans[event["payload"]["digest"]]["revision"], event["sequence"],
        ),
    )
    return {
        "rejected_plan_digest": latest["payload"]["digest"],
        "plan_rejection_reason": latest["payload"]["reason"],
    }


def _finalization_payload(
    store: InitiativeStore,
    initiative_id: str,
    source_state: str,
    outcome: str,
    reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "from": source_state,
        "to": outcome,
        "reason": reason,
        "retained_seal_ids": [
            item["seal_id"] for item in store.list_seals_snapshot(initiative_id)
        ],
    }
    if source_state in {"draft", "planning"}:
        payload.update(_latest_rejected_plan(store, initiative_id))
    if source_state == "approved":
        payload["abandoned_plan_digest"] = store.peek(initiative_id)["active_plan"][
            "digest"
        ]
    return payload


def _cancel_abandoned_plan_nodes(
    store: InitiativeStore,
    initiative_id: str,
    source_state: str,
    action_id: str | None,
) -> None:
    """Cancel pre-activation plan echoes and recover any missing edge events."""
    recovered_from = (
        "proposed" if source_state == "awaiting-plan-approval" else "approved"
    )
    for node in store.list_nodes_snapshot(initiative_id):
        events = store.list_events_snapshot(initiative_id)
        if node["state"] in {"proposed", "approved"}:
            from_state = node["state"]
            changed = copy.deepcopy(node)
            changed["state"] = "cancelled"
            store.save_node(
                initiative_id, changed, expected_digest=record_digest(node),
            )
        elif node["state"] == "cancelled":
            if any(
                event["type"] == "node-state-changed"
                and node["node_id"] in event["subject_ids"]
                and event["payload"].get("to") == "cancelled"
                for event in events
            ):
                continue
            # A crash can land after the CAS and before its event. The source
            # initiative state identifies the plan-echo state being recovered.
            from_state = recovered_from
        else:
            continue
        if any(
            event["type"] == "node-state-changed"
            and node["node_id"] in event["subject_ids"]
            and (action_id is None or action_id in event["subject_ids"])
            and event["payload"].get("from") == from_state
            and event["payload"].get("to") == "cancelled"
            and event["actor_kind"] == "controller"
            and event["actor_id"] == "finalization-gate"
            for event in events
        ):
            continue
        append_event(
            store, initiative_id, "node-state-changed",
            [node["node_id"], *([] if action_id is None else [action_id])],
            {"from": from_state, "to": "cancelled"},
            actor_kind="controller", actor_id="finalization-gate",
        )


def finalize_initiative(
    store: InitiativeStore,
    initiative_id: str,
    outcome: str,
    reason: str,
    *,
    action_id: str | None = None,
    source_state: str | None = None,
) -> dict[str, Any]:
    """Acknowledge a terminal graph that cannot produce a qualifying bundle."""
    if outcome not in {"partial", "failed"}:
        raise ReadinessError("finalize outcome must be partial or failed")
    if not isinstance(reason, str) or not reason or len(reason.encode("utf-8")) > 4096:
        raise ReadinessError("finalize reason must contain 1-4096 UTF-8 bytes")
    initiative = store.peek(initiative_id)
    if initiative["state"] == outcome:
        matching = [
            item["type"] == "initiative-state-changed"
            and item["payload"].get("from") in {
                "running", "draft", "planning", "awaiting-plan-approval", "approved",
            }
            and (
                source_state is None
                or item["payload"].get("from") == source_state
            )
            and item["payload"].get("to") == outcome
            and item["payload"].get("reason") == reason
            and (action_id is None or action_id in item["subject_ids"])
            for item in store.list_events_snapshot(initiative_id)
        ]
        if any(matching):
            return initiative
        if action_id is None or source_state not in {
            "running", "draft", "planning", "awaiting-plan-approval", "approved",
        }:
            raise ReadinessError("terminal initiative cannot be finalized again")
        if not any(
            item["action_id"] == action_id
            and item["action_class"] == "finalize"
            and item["state"] in {"dispatching", "indeterminate"}
            for item in store.list_actions_snapshot(initiative_id)
        ):
            raise ReadinessError("finalize recovery is not bound to an interrupted action")
        if not any(matching):
            append_event(
                store, initiative_id, "initiative-state-changed",
                [initiative_id, action_id],
                _finalization_payload(
                    store, initiative_id, source_state, outcome, reason,
                ),
                actor_kind="controller", actor_id="finalization-gate",
            )
        return store.peek(initiative_id)
    initiative = prevalidate_finalization(
        store, initiative_id, outcome, reason, action_id=action_id,
    )
    source_state = initiative["state"]
    if source_state in {"awaiting-plan-approval", "approved"}:
        _cancel_abandoned_plan_nodes(
            store, initiative_id, source_state, action_id,
        )
        initiative = store.peek(initiative_id)
    terminal_payload = _finalization_payload(
        store, initiative_id, source_state, outcome, reason,
    )
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": outcome,
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    validate_initiative(changed)
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed",
        [initiative_id, *([] if action_id is None else [action_id])],
        terminal_payload,
        actor_kind="controller", actor_id="finalization-gate",
    )
    return store.peek(initiative_id)


def _archive_inventory(
    store: InitiativeStore, initiative: Mapping[str, Any],
) -> dict[str, Any]:
    report = storage_report(initiative, store=store)
    return {
        "records": report["inventory"],
        "task_workspace_count": len(report["workspaces"]),
        "materialization_count": len(report["materializations"]),
        "workspace_count": len(report["workspaces"]) + len(report["materializations"]),
        "workspace_bytes": sum(
            item["bytes"] for item in [*report["workspaces"], *report["materializations"]]
        ),
        "workspace_inodes": sum(
            item["inodes"] for item in [*report["workspaces"], *report["materializations"]]
        ),
        "totals": copy.deepcopy(report["totals"]),
    }


def archive_initiative(
    store: InitiativeStore, initiative_id: str, *, source_state: str | None = None,
    action_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Archive a terminal outcome while retaining every record and workspace."""
    initiative = store.peek(initiative_id)
    if initiative["state"] == "archived":
        matching = [
            item for item in store.list_events_snapshot(initiative_id)
            if item["type"] == "initiative-state-changed"
            and item["payload"].get("to") == "archived"
            and (action_id is None or action_id in item["subject_ids"])
        ]
        if matching:
            inventory = matching[-1]["payload"].get("retained_inventory")
            if not isinstance(inventory, dict):
                raise ReadinessError("archive event lacks its retained inventory")
            return initiative, inventory
        if source_state not in {
            "ready-for-integration", "integrated", "partial", "failed", "cancelled",
        }:
            raise ReadinessError("archived initiative lacks a recoverable source outcome")
        inventory = _archive_inventory(store, initiative)
        append_event(
            store, initiative_id, "initiative-state-changed",
            [initiative_id, *([] if action_id is None else [action_id])],
            {"from": source_state, "to": "archived", "retained_inventory": inventory},
            actor_kind="operator", actor_id="cli",
        )
        return store.peek(initiative_id), inventory
    if initiative["state"] not in INITIATIVE_TERMINAL_STATES - {"archived"}:
        raise ReadinessError("only a terminal initiative outcome may be archived")
    source_state = initiative["state"]
    inventory = _archive_inventory(store, initiative)
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": "archived",
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    validate_initiative(changed)
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed",
        [initiative_id, *([] if action_id is None else [action_id])],
        {"from": source_state, "to": "archived", "retained_inventory": inventory},
        actor_kind="operator", actor_id="cli",
    )
    return store.peek(initiative_id), inventory


def unarchive_initiative(
    store: InitiativeStore, initiative_id: str, *, action_id: str | None = None,
    recovery_state: str | None = None, recover: bool = False,
) -> dict[str, Any]:
    """Restore the terminal outcome retained by the latest archive event."""
    initiative = store.peek(initiative_id)
    if initiative["state"] != "archived":
        if (
            not recover
            or action_id is None
            or initiative["state"] != recovery_state
            or recovery_state not in {
                "ready-for-integration", "integrated", "partial", "failed", "cancelled",
            }
        ):
            raise ReadinessError("only an archived initiative may be unarchived")
        matching = any(
            item["type"] == "initiative-state-changed"
            and item["payload"].get("from") == "archived"
            and item["payload"].get("to") == recovery_state
            and action_id in item["subject_ids"]
            for item in store.list_events_snapshot(initiative_id)
        )
        if not matching:
            append_event(
                store, initiative_id, "initiative-state-changed",
                [initiative_id, action_id],
                {"from": "archived", "to": recovery_state},
                actor_kind="operator", actor_id="cli",
            )
        return store.peek(initiative_id)
    archive_events = [
        item for item in store.list_events_snapshot(initiative_id)
        if item["type"] == "initiative-state-changed"
        and item["payload"].get("to") == "archived"
    ]
    if not archive_events:
        raise ReadinessError("archived initiative has no retained archive transition")
    restored = archive_events[-1]["payload"].get("from")
    if restored not in {
        "ready-for-integration", "integrated", "partial", "failed", "cancelled",
    }:
        raise ReadinessError("archive transition does not retain a terminal outcome")
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": restored,
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    validate_initiative(changed)
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed",
        [initiative_id, *([] if action_id is None else [action_id])],
        {"from": "archived", "to": restored},
        actor_kind="operator", actor_id="cli",
    )
    return store.peek(initiative_id)


__all__ = [
    "ReadinessError", "archive_initiative", "bind_readiness",
    "finalize_initiative", "unarchive_initiative",
]
