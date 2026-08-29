"""Explicit operator attestations that sealed work may be reclaimed.

This module never performs integration: it has no jj, Git, bookmark, merge,
rebase, or publication adapter.  A compatible bundle attestation advances the
durable lifecycle only from ready-for-integration; in every other non-integrated
state it remains a state-neutral fact.  Abandonment never advances lifecycle.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .model import (
    MAX_BUNDLE_MEMBERS,
    canonical_uuid,
    record_digest,
    validate_initiative,
)
from .store import InitiativeStore, StoreError


INTEGRATION_EVENT_TYPE = "seal-integration-recorded"
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


def _require_composed_verification(
    store: InitiativeStore, initiative_id: str, bundle: Mapping[str, Any],
) -> None:
    """Refuse unless the exact composition has a retained passed verdict.

    Local import keeps the undemanded path -- and Control prune's read path --
    free of the verification runner's jj, containment and materialization
    machinery.
    """
    from .verification import VerificationError, composed_verification_verdict

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
) -> dict[str, Any]:
    """Append one explicit operator attestation, or replay its existing event.

    `composed_verification` is the operator's opt-in demand that this bundle's
    members were verified composed together.  It is off by default and every
    statement it guards is behind it, so an undemanded attestation runs exactly
    the checks, reads and effects it ran before the gate existed.
    """
    if (bundle_id is None) == (seal_id is None):
        raise IntegrationError("record-integration requires exactly one of --bundle or --seal")
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
    "INTEGRATION_EVENT_TYPE", "IntegrationError", "IntegrationSnapshot",
    "integration_snapshot", "record_integration",
]
