"""Explicit operator attestations that sealed work may be reclaimed.

This module records facts only.  It deliberately has no jj, Git, bookmark,
merge, rebase, or publication adapter.
"""

from __future__ import annotations

import copy
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from .model import MAX_BUNDLE_MEMBERS, canonical_uuid
from .store import InitiativeStore


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


def record_integration(
    store: InitiativeStore,
    initiative_id: str,
    *,
    bundle_id: str | None = None,
    seal_id: str | None = None,
    abandoned: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Append one explicit operator attestation, or replay its existing event."""
    if (bundle_id is None) == (seal_id is None):
        raise IntegrationError("record-integration requires exactly one of --bundle or --seal")
    with store.transaction_lock(initiative_id):
        snapshot = integration_snapshot(store, initiative_id)
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
            existing = snapshot.source_events.get(("integrated", bundle_id))
            if existing is not None:
                return copy.deepcopy(existing)
            subject_ids = [bundle_id, *(member["seal_id"] for member in members)]
            payload: dict[str, Any] = {
                "disposition": "integrated", "members": members,
            }
        else:
            assert seal_id is not None
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

        return append_event(
            store, initiative_id, INTEGRATION_EVENT_TYPE, subject_ids, payload,
            actor_kind="operator", actor_id="cli",
        )


__all__ = [
    "INTEGRATION_EVENT_TYPE", "IntegrationError", "IntegrationSnapshot",
    "integration_snapshot", "record_integration",
]
