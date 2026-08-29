"""Exact ordered composition bindings and terminal-candidate enforcement."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .store import InitiativeStore


_MEMBER_IDENTITY = ("repository_id", "seal_id", "jj_commit_id", "tree_digest", "diff_digest")


class CompositionError(ValueError):
    """A composition input or terminal-candidate binding changed."""


def composition_inputs(
    store: InitiativeStore,
    initiative_id: str,
    node: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a compose attempt's declared seal set without reordering it."""
    if node.get("type") != "compose":
        raise CompositionError("composition input resolution requires a compose node")
    base = attempt.get("base")
    if not isinstance(base, Mapping) or base.get("policy") != "upstream-seal":
        raise CompositionError("compose attempt must use upstream-seal inputs")
    declarations = base.get("seal_inputs")
    if not isinstance(declarations, list) or len(declarations) < 2:
        raise CompositionError("compose attempt requires at least two ordered success seals")
    origin = base.get("scope_origin")
    repository_id = node.get("repository_id")
    upstream_node_ids = base.get("upstream_node_ids")
    if (
        not isinstance(upstream_node_ids, list)
        or len(upstream_node_ids) != len(declarations)
    ):
        raise CompositionError(
            "compose inputs must map one-to-one to the ordered upstream nodes"
        )
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    resolved_node_ids: set[str] = set()
    for declaration in declarations:
        seal_id = declaration.get("seal_id")
        if seal_id in seen:
            raise CompositionError("compose input seal IDs must be unique")
        seen.add(seal_id)
        seal = store.read_seal(initiative_id, seal_id)
        if (
            declaration.get("outcome") != "success"
            or declaration.get("read_only") is not False
            or seal["outcome"] != "success"
        ):
            raise CompositionError("compose inputs must be inheritable success seals")
        if seal["repository_id"] != repository_id:
            raise CompositionError("compose inputs must belong to the compose repository")
        resolved_node_ids.add(seal["node_id"])
        if declaration.get("scope_origin") != origin or seal["scope_origin"] != origin:
            raise CompositionError("compose inputs must share the exact scope origin")
        resolved.append(seal)
    if resolved_node_ids != set(upstream_node_ids):
        raise CompositionError(
            "compose inputs must contain one seal from every declared upstream node"
        )
    return resolved


def bundle_composition_digest(bundle: Mapping[str, Any]) -> str:
    """Bind one bundle's exact ordered member identities.

    Both the composed gate and the integration attestation derive this from the
    retained bundle alone, so a verdict can never be replayed onto a different
    bundle or a bundle whose members differ by one digest.
    """
    return hashlib.sha256(json.dumps(
        [
            bundle["bundle_id"],
            [
                [member[field] for field in _MEMBER_IDENTITY]
                for member in bundle["members"]
            ],
        ],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def bundle_composition_inputs(
    store: InitiativeStore,
    initiative_id: str,
    bundle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a compatible bundle's member seals in exact bundle order.

    This is a sibling of `composition_inputs`, not a reuse of it: that resolver
    is compose-node shaped -- it needs a compose node plus an `upstream-seal`
    attempt and refuses inputs outside one repository -- while a bundle carries
    at most one member per repository and no node or attempt at all.
    """
    if bundle["state"] != "compatible" or bundle["outcome"] != "compatible":
        raise CompositionError("composed verification requires a compatible bundle")
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in bundle["members"]:
        seal_id = member["seal_id"]
        if seal_id in seen:
            raise CompositionError("bundle member seal IDs must be unique")
        seen.add(seal_id)
        seal = store.read_seal(initiative_id, seal_id)
        if seal["outcome"] != "success":
            raise CompositionError("bundle members must be success seals")
        if any(seal[field] != member[field] for field in _MEMBER_IDENTITY):
            raise CompositionError(
                f"bundle member seal {seal_id} is unavailable or changed"
            )
        resolved.append(seal)
    return resolved


def enforce_terminal_candidate(
    store: InitiativeStore,
    initiative_id: str,
    node: Mapping[str, Any],
) -> None:
    """Re-check P014 against retained plan and seals at publication time."""
    initiative = store.peek(initiative_id)
    active = initiative.get("active_plan")
    if active is None:
        raise CompositionError("candidate seal requires an active approved plan")
    plan = store.read_plan(initiative_id, active["revision"])
    producers = [
        item for item in plan["nodes"]
        if item.get("repository_id") == node.get("repository_id")
        and item.get("terminal_candidate") is True
    ]
    if len(producers) != 1 or producers[0]["node_id"] != node.get("node_id"):
        raise CompositionError(
            "repository must have exactly one active terminal candidate producer"
        )
    producer_ids: set[str] = set()
    for retained_plan in store.list_plans_snapshot(initiative_id):
        producer_ids.update(
            item["node_id"] for item in retained_plan["nodes"]
            if item.get("repository_id") == node.get("repository_id")
            and item.get("terminal_candidate") is True
        )
    for seal in store.list_seals_snapshot(initiative_id):
        if (
            seal["outcome"] == "success"
            and seal["repository_id"] == node.get("repository_id")
            and seal["node_id"] in producer_ids
            and seal["node_id"] != node.get("node_id")
        ):
            raise CompositionError(
                "a second terminal candidate seal for the repository is refused"
            )


__all__ = [
    "CompositionError", "bundle_composition_digest", "bundle_composition_inputs",
    "composition_inputs", "enforce_terminal_candidate",
]
