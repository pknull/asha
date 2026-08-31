"""Explicit operator attestations that sealed work may be reclaimed.

This module never performs integration, bookmark movement, merge, rebase, or
publication.  It does use read-only jj inspection to authenticate an exact
fallback candidate and the integration target before recording the operator's
attestation.  A compatible bundle attestation advances the durable lifecycle
only from ready-for-integration; abandonment never advances lifecycle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..context import read_published_snapshot
from ..jj import JjAdapter, JjError
from ..prepare import derive_repository_identity
from .model import (
    EVIDENCE_CONTRACT,
    MAX_BUNDLE_MEMBERS,
    canonical_uuid,
    record_digest,
    repository_by_id,
    validate_evidence,
    validate_fallback_integration,
    validate_initiative,
)
from .store import InitiativeStore, StoreError


INTEGRATION_EVENT_TYPE = "seal-integration-recorded"
FALLBACK_EVIDENCE_KIND = "fallback-integration-attestation"
_MAX_REASON_BYTES = 4096


class IntegrationError(ValueError):
    """An integration attestation is invalid, conflicting, or unreadable."""


@dataclass(frozen=True)
class IntegrationSnapshot:
    seals: dict[str, dict[str, Any]]
    bundles: dict[str, dict[str, Any]]
    facts: dict[str, dict[str, Any]]
    source_events: dict[tuple[str, str], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _reason(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_REASON_BYTES
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
    ):
        raise IntegrationError("abandonment reason must be bounded printable text")
    return value


def _member_facts(members: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {"seal_id": member["seal_id"], "jj_commit_id": member["jj_commit_id"]}
        for member in members
    ]


def _event_members(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    members = payload.get("members")
    if (
        not isinstance(members, list)
        or not members
        or len(members) > MAX_BUNDLE_MEMBERS
    ):
        raise IntegrationError("integration event members are invalid")
    checked: list[dict[str, str]] = []
    seen: set[str] = set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"seal_id", "jj_commit_id"}:
            raise IntegrationError("integration event member is invalid")
        try:
            seal_id = canonical_uuid(member["seal_id"], "integration event seal_id")
        except (TypeError, ValueError) as exc:
            raise IntegrationError(str(exc)) from exc
        commit_id = member["jj_commit_id"]
        if seal_id in seen or not isinstance(commit_id, str):
            raise IntegrationError("integration event members are invalid")
        seen.add(seal_id)
        checked.append({"seal_id": seal_id, "jj_commit_id": commit_id})
    return checked


def integration_snapshot(
    store: InitiativeStore, initiative_id: str,
) -> IntegrationSnapshot:
    """Read and semantically validate every retained integration fact.

    The operation is lock-free and mutation-free so Control prune can use it on
    its fail-closed read path.  Any partially readable or internally
    inconsistent fact raises instead of turning into permission to reclaim.
    """
    seals = {
        seal["seal_id"]: seal
        for seal in store.list_seals_snapshot(initiative_id)
    }
    bundles = {
        bundle["bundle_id"]: bundle
        for bundle in store.list_bundles_snapshot(initiative_id)
    }
    facts: dict[str, dict[str, Any]] = {}
    source_events: dict[tuple[str, str], dict[str, Any]] = {}
    for event in store.list_events_snapshot(initiative_id):
        if event["type"] != INTEGRATION_EVENT_TYPE:
            continue
        if event["actor_kind"] != "operator":
            raise IntegrationError("integration event actor must be operator")
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise IntegrationError("integration event payload is invalid")
        disposition = payload.get("disposition")
        members = _event_members(payload)
        seal_ids = [member["seal_id"] for member in members]
        if disposition == "integrated":
            if set(payload) != {"disposition", "members"}:
                raise IntegrationError("integrated event payload is invalid")
            if not event["subject_ids"]:
                raise IntegrationError("integrated event has no bundle subject")
            bundle = bundles.get(event["subject_ids"][0])
            if bundle is None:
                raise IntegrationError("integrated event bundle is unavailable")
            if bundle["state"] != "compatible" or bundle["outcome"] != "compatible":
                raise IntegrationError("integrated event does not name a compatible bundle")
            expected = _member_facts(bundle["members"])
            if members != expected or event["subject_ids"] != [
                bundle["bundle_id"], *seal_ids,
            ]:
                raise IntegrationError("integrated event does not match its bundle members")
        elif disposition == "abandoned":
            if set(payload) != {"disposition", "members", "reason"}:
                raise IntegrationError("abandoned event payload is invalid")
            _reason(payload["reason"])
            if len(members) != 1 or event["subject_ids"] != seal_ids:
                raise IntegrationError("abandoned event must name exactly one seal")
        elif disposition == "fallback-integrated":
            expected_keys = {
                "disposition", "attestation_id", "evidence_digest",
                "repository_id", "candidate_jj_commit_id",
                "candidate_tree_digest", "failure_seal_ids", "members",
                "source_state",
            }
            if set(payload) != expected_keys:
                raise IntegrationError("fallback integration event payload is invalid")
            try:
                attestation_id = canonical_uuid(
                    payload["attestation_id"], "fallback attestation ID",
                )
                repository_id = canonical_uuid(
                    payload["repository_id"], "fallback repository ID",
                )
            except (TypeError, ValueError) as exc:
                raise IntegrationError(str(exc)) from exc
            if (
                not isinstance(payload["failure_seal_ids"], list)
                or payload["failure_seal_ids"] != seal_ids
                or event["subject_ids"] != [attestation_id, *seal_ids]
            ):
                raise IntegrationError(
                    "fallback integration event subjects differ from its failure lineage"
                )
            retained_evidence = {
                item["evidence_id"]: item
                for item in store.list_evidence_snapshot(initiative_id)
            }.get(attestation_id)
            if (
                retained_evidence is None
                or retained_evidence["kind"] != FALLBACK_EVIDENCE_KIND
                or retained_evidence["subject_id"] != attestation_id
                or retained_evidence["digest"] != payload["evidence_digest"]
            ):
                raise IntegrationError(
                    "fallback integration attestation evidence is unavailable or changed"
                )
            try:
                attestation = validate_fallback_integration(
                    json.loads(retained_evidence["summary"]),
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise IntegrationError(
                    "fallback integration attestation evidence is unreadable"
                ) from exc
            if (
                attestation["attestation_id"] != attestation_id
                or attestation["initiative_id"] != initiative_id
                or attestation["repository_id"] != repository_id
                or attestation["failure_seal_ids"] != seal_ids
                or attestation["candidate"]["jj_commit_id"]
                != payload["candidate_jj_commit_id"]
                or attestation["candidate"]["tree_digest"]
                != payload["candidate_tree_digest"]
                or payload["source_state"] not in {
                    "running", "paused", "needs-input", "partial", "failed",
                }
            ):
                raise IntegrationError(
                    "fallback integration event differs from its attestation"
                )
            for member in members:
                seal = seals.get(member["seal_id"])
                if (
                    seal is None
                    or seal["outcome"] != "failure"
                    or seal["repository_id"] != repository_id
                    or seal["jj_commit_id"] != member["jj_commit_id"]
                ):
                    raise IntegrationError(
                        f"fallback failure seal {member['seal_id']} is unavailable or changed"
                    )
        else:
            raise IntegrationError("integration event disposition is invalid")
        source_key = (disposition, event["subject_ids"][0])
        if source_key in source_events:
            raise IntegrationError("integration source has multiple retained events")
        source_events[source_key] = event
        for member in members:
            seal = seals.get(member["seal_id"])
            if seal is None or seal["jj_commit_id"] != member["jj_commit_id"]:
                raise IntegrationError(
                    f"integration event seal {member['seal_id']} is unavailable or changed"
                )
            retained = facts.get(member["seal_id"])
            if retained is not None and retained["disposition"] != disposition:
                raise IntegrationError(
                    f"seal {member['seal_id']} has conflicting integration dispositions"
                )
            if retained is None:
                facts[member["seal_id"]] = {
                    "disposition": disposition,
                    "event": event,
                }
    return IntegrationSnapshot(
        seals=seals, bundles=bundles, facts=facts, source_events=source_events,
    )


def _canonical_attestation(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _matching_fallback_evidence(
    store: InitiativeStore,
    initiative_id: str,
    expected: Mapping[str, Any],
) -> bool:
    """Accept only an exact write-once evidence prefix from an interrupted call."""
    retained = {
        item["evidence_id"]: item
        for item in store.list_evidence_snapshot(initiative_id)
    }.get(expected["evidence_id"])
    if retained is None:
        return False
    binding_fields = {
        "contract", "evidence_id", "initiative_id", "kind", "subject_id",
        "digest", "summary",
    }
    if any(retained[field] != expected[field] for field in binding_fields):
        raise IntegrationError(
            "fallback attestation ID was already used for different evidence"
        )
    return True


def _fallback_static_preflight(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind the fallback document to retained plan, scope and failure facts."""
    initiative_id = initiative["initiative_id"]
    if attestation["initiative_id"] != initiative_id:
        raise IntegrationError("fallback attestation belongs to another initiative")
    active = initiative.get("active_plan")
    if (
        not isinstance(active, Mapping)
        or active.get("digest") != attestation["active_plan_digest"]
    ):
        raise IntegrationError("fallback attestation does not bind the active plan")
    plan = store.read_plan(initiative_id, active["revision"])
    if plan["digest"] != active["digest"]:
        raise IntegrationError("active plan digest differs from its retained revision")
    try:
        repository_by_id(initiative, attestation["repository_id"])
    except ValueError as exc:
        raise IntegrationError(str(exc)) from exc
    target = attestation["integration_target"]
    if target["repository_id"] != attestation["repository_id"]:
        raise IntegrationError("fallback integration target names another repository")
    by_node = {item["node_id"]: item for item in plan["nodes"]}
    retained_seals = {
        item["seal_id"]: item
        for item in store.list_seals_snapshot(initiative_id)
    }
    lineage: list[dict[str, Any]] = []
    for seal_id in attestation["failure_seal_ids"]:
        seal = retained_seals.get(seal_id)
        if (
            seal is None
            or seal["outcome"] != "failure"
            or seal["repository_id"] != attestation["repository_id"]
        ):
            raise IntegrationError(
                f"fallback lineage seal {seal_id} is not a retained failure seal"
            )
        node = by_node.get(seal["node_id"])
        if node is None or node["repository_id"] != seal["repository_id"]:
            raise IntegrationError(
                f"fallback lineage seal {seal_id} is outside the active plan"
            )
        if node["hard_write_scope"] != attestation["hard_write_scope"]:
            raise IntegrationError(
                f"fallback lineage seal {seal_id} hard scope differs from the attestation"
            )
        if seal["scope_origin"] != attestation["baseline"]:
            raise IntegrationError(
                f"fallback lineage seal {seal_id} baseline differs from the attestation"
            )
        # The failure record's process evidence must remain readable.  The
        # fallback adds evidence; it never repairs or reinterprets the seal.
        store.read_evidence(initiative_id, seal["process_evidence_id"])
        lineage.append(seal)
    candidate = attestation["candidate"]
    endpoint = lineage[-1]
    if (
        endpoint["jj_commit_id"] != candidate["jj_commit_id"]
        or endpoint["tree_digest"] != candidate["tree_digest"]
        or endpoint["cumulative_diff_digest"] != candidate["diff_digest"]
        or endpoint["cumulative_changed_paths_truncated"] != 0
        or endpoint["cumulative_changed_paths"] != candidate["changed_paths"]
    ):
        raise IntegrationError(
            "fallback candidate must exactly match the final retained failure seal"
        )
    if any(
        seal["outcome"] == "success"
        and seal["repository_id"] == attestation["repository_id"]
        and seal["jj_commit_id"] == candidate["jj_commit_id"]
        and seal["tree_digest"] == candidate["tree_digest"]
        for seal in retained_seals.values()
    ):
        raise IntegrationError(
            "fallback refuses a candidate that already has a success seal"
        )
    from .verification import command_denial

    for index, command in enumerate(attestation["verification"]):
        denial = command_denial(list(command["argv"]))
        if denial is not None:
            raise IntegrationError(
                f"fallback verification command index {index} is denied by "
                f"Control command policy: {denial}"
            )
    return plan, lineage


def _fallback_repository_preflight(
    initiative: Mapping[str, Any],
    attestation: Mapping[str, Any],
    *,
    jj: JjAdapter,
) -> None:
    """Reauthenticate repository and exact immutable trees before acceptance."""
    from .seals import immutable_tree_diff, path_in_scope
    from .workspace_scope import ScopeError, verify_scope_identity

    try:
        verify_scope_identity(
            initiative, jj=jj, read_snapshot=read_published_snapshot,
            derive_identity=derive_repository_identity,
        )
        repository = repository_by_id(initiative, attestation["repository_id"])
        root = Path(repository["root"])
        baseline = attestation["baseline"]
        candidate = attestation["candidate"]
        target = attestation["integration_target"]
        for commit_id in {
            baseline["jj_commit_id"], candidate["jj_commit_id"],
            target["jj_commit_id"],
        }:
            jj.require_visible_commit(root, commit_id)
        baseline_tree = jj.immutable_tree(root, baseline["jj_commit_id"])
        candidate_tree = jj.immutable_tree(root, candidate["jj_commit_id"])
        target_tree = jj.immutable_tree(root, target["jj_commit_id"])
    except (JjError, ScopeError, OSError, ValueError) as exc:
        raise IntegrationError(
            f"fallback repository identity or immutable commit is unavailable: {exc}"
        ) from exc
    for label, observed, declared in (
        ("baseline", baseline_tree.digest, baseline["tree_digest"]),
        ("candidate", candidate_tree.digest, candidate["tree_digest"]),
        ("integration target", target_tree.digest, target["tree_digest"]),
    ):
        if observed != declared:
            raise IntegrationError(
                f"fallback {label} tree differs from the attested exact tree"
            )
    changed_paths, diff_digest = immutable_tree_diff(baseline_tree, candidate_tree)
    if (
        changed_paths != candidate["changed_paths"]
        or diff_digest != candidate["diff_digest"]
    ):
        raise IntegrationError(
            "fallback candidate hard-scope diff differs from immutable trees"
        )
    violations = [
        path for path in changed_paths
        if not path_in_scope(path, attestation["hard_write_scope"])
    ]
    if violations:
        raise IntegrationError(
            f"fallback candidate violates hard write scope at {violations[0]}"
        )
    candidate_entries = {
        path: (mode, blob_id)
        for path, mode, blob_id in candidate_tree.entries
    }
    target_entries = {
        path: (mode, blob_id)
        for path, mode, blob_id in target_tree.entries
    }
    if any(
        candidate_entries.get(path) != target_entries.get(path)
        for path in changed_paths
    ):
        raise IntegrationError(
            "fallback integration target does not contain the exact candidate changes"
        )


def _finish_fallback_transition(
    store: InitiativeStore,
    initiative_id: str,
    event: Mapping[str, Any],
) -> None:
    """Complete or replay the state/event tail after evidence was accepted."""
    attestation_id = event["payload"]["attestation_id"]
    source_state = event["payload"]["source_state"]
    with store.transaction_lock(initiative_id):
        current = store.peek(initiative_id)
        if current["state"] == source_state:
            changed = copy.deepcopy(current)
            changed.update({
                "state": "integrated",
                "state_revision": current["state_revision"] + 1,
                "updated_at": _now(),
            })
            validate_initiative(changed)
            store.save_initiative(
                changed, expected_digest=record_digest(current),
            )
        elif current["state"] != "integrated":
            raise IntegrationError(
                "fallback integration lifecycle changed after evidence acceptance"
            )
        if not any(
            item["type"] == "initiative-state-changed"
            and item["payload"].get("fallback_attestation_id") == attestation_id
            and item["payload"].get("from") == source_state
            and item["payload"].get("to") == "integrated"
            for item in store.list_events_snapshot(initiative_id)
        ):
            from .actions import append_event

            append_event(
                store, initiative_id, "initiative-state-changed",
                [initiative_id, attestation_id],
                {
                    "from": source_state, "to": "integrated",
                    "fallback_attestation_id": attestation_id,
                },
                actor_kind="operator", actor_id="cli",
            )


def record_fallback_integration(
    store: InitiativeStore,
    initiative_id: str,
    document: Mapping[str, Any],
    *,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Accept one replay-safe operator fallback and advance the validated rail."""
    try:
        attestation = validate_fallback_integration(document)
    except ValueError as exc:
        raise IntegrationError(str(exc)) from exc
    summary = _canonical_attestation(attestation)
    evidence_digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    initiative = store.peek(initiative_id)
    _fallback_static_preflight(store, initiative, attestation)

    snapshot = integration_snapshot(store, initiative_id)
    existing = snapshot.source_events.get((
        "fallback-integrated", attestation["attestation_id"],
    ))
    if existing is not None:
        retained = store.read_evidence(initiative_id, attestation["attestation_id"])
        if retained["digest"] != evidence_digest or retained["summary"] != summary:
            raise IntegrationError(
                "fallback attestation ID was already used for different evidence"
            )
        if store.peek(initiative_id)["state"] != "integrated":
            # An integration event is durable before the lifecycle edge.  A
            # replay after interruption must reauthenticate the live exact
            # repository facts before it performs that remaining mutation.
            _fallback_repository_preflight(
                initiative, attestation, jj=jj or JjAdapter(),
            )
        _finish_fallback_transition(store, initiative_id, existing)
        return copy.deepcopy(existing)
    if initiative["state"] == "integrated":
        raise IntegrationError(
            "initiative is already integrated without this fallback attestation"
        )
    if initiative["state"] not in {
        "running", "paused", "needs-input", "partial", "failed",
    }:
        raise IntegrationError(
            "fallback integration requires preserved work on an incomplete rail"
        )
    for seal_id in attestation["failure_seal_ids"]:
        fact = snapshot.facts.get(seal_id)
        if fact is not None:
            raise IntegrationError(
                f"fallback lineage seal {seal_id} already has an integration disposition"
            )
    _fallback_repository_preflight(
        initiative, attestation, jj=jj or JjAdapter(),
    )
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": attestation["attestation_id"],
        "initiative_id": initiative_id,
        "kind": FALLBACK_EVIDENCE_KIND,
        "subject_id": attestation["attestation_id"],
        "digest": evidence_digest,
        "summary": summary,
        "recorded_at": _now(),
    })
    # Evidence is write-once and intentionally precedes the acceptance event.
    # Recognize an exact orphan as the replayable prefix of an interrupted call.
    _matching_fallback_evidence(store, initiative_id, evidence)
    members = [
        {
            "seal_id": seal["seal_id"],
            "jj_commit_id": seal["jj_commit_id"],
        }
        for seal in (
            snapshot.seals[seal_id]
            for seal_id in attestation["failure_seal_ids"]
        )
    ]
    payload = {
        "disposition": "fallback-integrated",
        "attestation_id": attestation["attestation_id"],
        "evidence_digest": evidence_digest,
        "repository_id": attestation["repository_id"],
        "candidate_jj_commit_id": attestation["candidate"]["jj_commit_id"],
        "candidate_tree_digest": attestation["candidate"]["tree_digest"],
        "failure_seal_ids": list(attestation["failure_seal_ids"]),
        "members": members,
        "source_state": initiative["state"],
    }
    from .actions import append_event

    with store.transaction_lock(initiative_id):
        current = store.peek(initiative_id)
        if record_digest(current) != record_digest(initiative):
            raise StoreError(
                "initiative changed; reload before recording fallback integration"
            )
        if not _matching_fallback_evidence(store, initiative_id, evidence):
            store.save_evidence(initiative_id, evidence)
        event = append_event(
            store, initiative_id, INTEGRATION_EVENT_TYPE,
            [attestation["attestation_id"], *attestation["failure_seal_ids"]],
            payload, actor_kind="operator", actor_id="cli",
        )
        _finish_fallback_transition(store, initiative_id, event)
    return copy.deepcopy(event)


def _require_composed_verification(
    store: InitiativeStore, initiative_id: str, bundle: Mapping[str, Any],
) -> None:
    """Refuse unless this bundle's members were proven composed.

    Two proofs satisfy the demand.  The exact bundle composition is the direct
    one.  A cross-initiative composition that *covers* every member seal is the
    other, and it is the proof that matters here: the operator merges the
    same-repository seals from different initiatives that are landing together,
    then attests each bundle within that one proven composition.

    Covering is not subset-implication and must not be read as one.  A wider
    composition passing does not prove a narrower one builds -- two seals each
    dropping a use of a shared helper pass beside a third that adds one, and
    fail without it.  What it proves is that the tree the operator verified is
    the tree they are landing, which holds exactly when the verified
    composition is what reaches mainline.  Verifying more than is landed
    therefore proves the wrong tree, and the plane cannot detect that: it never
    observes mainline.

    Only the exact composition can condemn -- a wider composition's failure may
    belong entirely to a seal this bundle does not name.

    Local import keeps the undemanded path -- and Control prune's read path --
    free of the verification runner's jj, containment and materialization
    machinery.
    """
    from .verification import (
        VerificationError, composed_verification_verdict,
        covering_cross_composed_verdict,
    )

    try:
        outcome, _summary = composed_verification_verdict(store, initiative_id, bundle)
    except (StoreError, VerificationError) as exc:
        raise IntegrationError(f"composed verification evidence is unreadable: {exc}") from exc
    if outcome == "failed":
        raise IntegrationError(
            f"bundle {bundle['bundle_id']} composed verification failed on this "
            "exact member composition"
        )
    if outcome != "passed":
        try:
            covering, _covering_summary = covering_cross_composed_verdict(
                store, initiative_id, bundle["members"],
            )
        except (StoreError, VerificationError) as exc:
            raise IntegrationError(
                f"composed verification evidence is unreadable: {exc}"
            ) from exc
        if covering != "passed":
            # An environment-class outcome defers; it never becomes a verdict.
            raise IntegrationError(
                f"bundle {bundle['bundle_id']} has no passed composed verification "
                f"for this exact member composition (retained outcome: {outcome})"
            )


def record_integration(
    store: InitiativeStore,
    initiative_id: str,
    *,
    bundle_id: str | None = None,
    seal_id: str | None = None,
    abandoned: bool = False,
    reason: str | None = None,
    composed_verification: bool = False,
    fallback: Mapping[str, Any] | None = None,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Append one explicit operator attestation, or replay its existing event.

    `composed_verification` is the operator's opt-in demand that this bundle's
    members were verified composed together.  It is off by default and every
    statement it guards is behind it, so an undemanded attestation runs exactly
    the checks, reads and effects it ran before the gate existed.
    """
    forms = sum(value is not None for value in (bundle_id, seal_id, fallback))
    if forms != 1:
        raise IntegrationError(
            "record-integration requires exactly one of --bundle or --seal, "
            "or use --fallback alone"
        )
    if fallback is not None:
        if abandoned or reason is not None or composed_verification:
            raise IntegrationError(
                "fallback integration does not accept bundle, seal, abandonment, "
                "reason, or composed-verification options"
            )
        return record_fallback_integration(
            store, initiative_id, fallback, jj=jj,
        )
    with store.transaction_lock(initiative_id):
        snapshot = integration_snapshot(store, initiative_id)
        existing: dict[str, Any] | None = None
        if bundle_id is not None:
            try:
                bundle_id = canonical_uuid(bundle_id, "bundle ID")
            except (TypeError, ValueError) as exc:
                raise IntegrationError(str(exc)) from exc
            if abandoned or reason is not None:
                raise IntegrationError("bundle integration does not accept abandonment options")
            bundle = snapshot.bundles.get(bundle_id)
            if bundle is None:
                raise IntegrationError(
                    f"bundle {bundle_id} does not belong to initiative {initiative_id}"
                )
            if bundle["state"] != "compatible" or bundle["outcome"] != "compatible":
                raise IntegrationError(f"bundle {bundle_id} is not a compatible bundle")
            members = _member_facts(bundle["members"])
            for member in members:
                seal = snapshot.seals.get(member["seal_id"])
                if seal is None or seal["jj_commit_id"] != member["jj_commit_id"]:
                    raise IntegrationError(
                        f"compatible bundle member seal {member['seal_id']} is unavailable or changed"
                    )
                fact = snapshot.facts.get(member["seal_id"])
                if fact is not None and fact["disposition"] == "abandoned":
                    raise IntegrationError(
                        f"seal {member['seal_id']} is already recorded as abandoned"
                    )
            if composed_verification:
                _require_composed_verification(store, initiative_id, bundle)
            existing = snapshot.source_events.get(("integrated", bundle_id))
            subject_ids = [bundle_id, *(member["seal_id"] for member in members)]
            payload: dict[str, Any] = {
                "disposition": "integrated", "members": members,
            }
        else:
            assert seal_id is not None
            if composed_verification:
                raise IntegrationError(
                    "composed verification applies to the --bundle form only"
                )
            try:
                seal_id = canonical_uuid(seal_id, "seal ID")
            except (TypeError, ValueError) as exc:
                raise IntegrationError(str(exc)) from exc
            if not abandoned:
                raise IntegrationError("--seal form requires --abandoned")
            if reason is None:
                raise IntegrationError("--seal form requires --reason")
            reason = _reason(reason)
            seal = snapshot.seals.get(seal_id)
            if seal is None:
                raise IntegrationError(
                    f"seal {seal_id} does not belong to initiative {initiative_id}"
                )
            retained = snapshot.facts.get(seal_id)
            if retained is not None:
                if retained["disposition"] != "abandoned":
                    raise IntegrationError(f"seal {seal_id} is already recorded as integrated")
                return copy.deepcopy(retained["event"])
            subject_ids = [seal_id]
            payload = {
                "disposition": "abandoned",
                "members": [{"seal_id": seal_id, "jj_commit_id": seal["jj_commit_id"]}],
                "reason": reason,
            }
        # Local import keeps the read-only prune path free of action machinery.
        from .actions import append_event

        event = existing
        if event is None:
            event = append_event(
                store, initiative_id, INTEGRATION_EVENT_TYPE, subject_ids, payload,
                actor_kind="operator", actor_id="cli",
            )
        if bundle_id is None:
            return copy.deepcopy(event)

        current = store.peek(initiative_id)
        if current["state"] == "ready-for-integration":
            changed = copy.deepcopy(current)
            changed.update({
                "state": "integrated",
                "state_revision": current["state_revision"] + 1,
                "updated_at": _now(),
            })
            validate_initiative(changed)
            store.save_initiative(changed, expected_digest=record_digest(current))
        elif current["state"] != "integrated":
            return copy.deepcopy(event)
        if not any(
            item["type"] == "initiative-state-changed"
            and item["payload"].get("from") == "ready-for-integration"
            and item["payload"].get("to") == "integrated"
            for item in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "initiative-state-changed",
                [initiative_id, bundle_id],
                {"from": "ready-for-integration", "to": "integrated"},
                actor_kind="operator", actor_id="cli",
            )
        return copy.deepcopy(event)


__all__ = [
    "FALLBACK_EVIDENCE_KIND", "INTEGRATION_EVENT_TYPE", "IntegrationError",
    "IntegrationSnapshot", "integration_snapshot", "record_fallback_integration",
    "record_integration",
]
