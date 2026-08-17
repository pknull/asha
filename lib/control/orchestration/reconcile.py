"""Read-only joins between orchestration attempts and frozen Control records."""

from __future__ import annotations

from typing import Any, Callable

from ..jj import JjAdapter
from ..reconcile import LiveAdapters, reconcile_task
from ..store import StoreError, TaskStore, task_digest
from ..tmux import TmuxAdapter
from .store import InitiativeStore


NODE_RECONCILIATION_CONTRACT = "asha.orchestration-node-reconciliation.v1"


def _unlinked(node_id: str) -> dict[str, Any]:
    return {
        "contract": NODE_RECONCILIATION_CONTRACT,
        "node_id": node_id,
        "attempt_id": None,
        "control_task_id": None,
        "control_state": "unlinked",
        "control_lifecycle": None,
        "digest_match": None,
        "evidence": [],
    }


def reconcile_nodes(
    initiative_id: str,
    nodes: list[dict[str, Any]],
    *,
    store: InitiativeStore,
    control_store: TaskStore | None = None,
    adapters_factory: Callable[[dict[str, Any]], LiveAdapters] | None = None,
) -> list[dict[str, Any]]:
    """Join stored links to live Control evidence without updating either store."""
    links = store.list_links_snapshot(initiative_id)
    attempts = {
        item["attempt_id"]: item for item in store.list_attempts_snapshot(initiative_id)
    }
    by_node: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_node.setdefault(link["node_id"], []).append(link)
    if control_store is None:
        control_store = TaskStore(store.config.control)

    results: list[dict[str, Any]] = []
    for node in nodes:
        candidates = by_node.get(node["node_id"], [])
        if not candidates:
            results.append(_unlinked(node["node_id"]))
            continue
        link = max(
            candidates,
            key=lambda item: (
                attempts.get(item["attempt_id"], {}).get("ordinal", 0),
                item["attempt_id"],
            ),
        )
        base = {
            "contract": NODE_RECONCILIATION_CONTRACT,
            "node_id": node["node_id"],
            "attempt_id": link["attempt_id"],
            "control_task_id": link["control_task_id"],
        }
        try:
            task = control_store.peek(link["control_task_id"])
        except StoreError as exc:
            results.append({
                **base,
                "control_state": "stale",
                "control_lifecycle": None,
                "digest_match": False,
                "evidence": [{
                    "source": "control-task", "outcome": "missing",
                    "detail": str(exc), "state": None, "stale": False,
                }],
            })
            continue
        digest_match = task_digest(task) == link["control_task_record_digest"]
        if adapters_factory is None:
            socket = task["tmux"]["socket"]
            adapters = LiveAdapters(
                config=store.config.control,
                tmux=TmuxAdapter(socket=None if socket == "default" else socket),
                jj=JjAdapter(),
            )
        else:
            adapters = adapters_factory(task)
        reconciliation = reconcile_task(task, adapters)
        evidence = list(reconciliation["evidence"])
        if not digest_match:
            evidence.insert(0, {
                "source": "control-task", "outcome": "mismatch",
                "detail": "stored Control task digest differs from the live record",
                "state": None, "stale": False,
            })
        results.append({
            **base,
            "control_state": reconciliation["state"] if digest_match else "stale",
            "control_lifecycle": task["lifecycle"],
            "digest_match": digest_match,
            "evidence": evidence,
        })
    return results


__all__ = ["NODE_RECONCILIATION_CONTRACT", "reconcile_nodes"]
