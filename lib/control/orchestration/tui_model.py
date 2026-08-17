"""Pure initiative-tree presentation state. This module performs no I/O."""

from __future__ import annotations

import copy
from typing import Any


class TuiModel:
    def __init__(
        self,
        initiative: dict[str, Any],
        nodes: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        events: list[dict[str, Any]],
        *,
        event_limit: int = 50,
        superseded_nodes: list[dict[str, Any]] | None = None,
    ) -> None:
        if isinstance(event_limit, bool) or not isinstance(event_limit, int) or event_limit <= 0:
            raise ValueError("event_limit must be a positive integer")
        self.initiative = copy.deepcopy(initiative)
        copied_nodes = copy.deepcopy(nodes)
        if superseded_nodes is None:
            self.superseded_nodes = [
                node for node in copied_nodes if node.get("state") == "superseded"
            ]
            self.nodes = [
                node for node in copied_nodes if node.get("state") != "superseded"
            ]
        else:
            self.nodes = copied_nodes
            self.superseded_nodes = copy.deepcopy(superseded_nodes)
        self.attempts = copy.deepcopy(attempts)
        self.events = copy.deepcopy(events)
        self.event_limit = event_limit

    def rows(self, *, query: str | None = None, sort_by: str = "id") -> list[dict[str, Any]]:
        if sort_by not in {"id", "state", "type"}:
            raise ValueError("sort_by must be id, state, or type")
        needle = "" if query is None else query.casefold()
        initiative_row = {
            "kind": "initiative", "depth": 0,
            "id": self.initiative["initiative_id"],
            "label": self.initiative.get("label", self.initiative.get("slug", "")),
            "state": self.initiative.get("state"), "type": "initiative",
        }
        rows = [initiative_row]
        nodes = sorted(
            self.nodes,
            key=lambda item: (
                str(item.get(sort_by, item["node_id"])), item["node_id"]
            ),
        )
        for node in nodes:
            node_row = {
                "kind": "node", "depth": 1, "id": node["node_id"],
                "label": node.get("goal", node["node_id"]),
                "state": node.get("state"), "type": node.get("type"),
            }
            attempts = sorted(
                (item for item in self.attempts if item["node_id"] == node["node_id"]),
                key=lambda item: (item.get("ordinal", 0), item["attempt_id"]),
            )
            children = [{
                "kind": "attempt", "depth": 2, "id": item["attempt_id"],
                "label": f"attempt {item.get('ordinal', '?')}",
                "state": item.get("state"), "type": "attempt",
            } for item in attempts]
            searchable = " ".join(str(value) for value in node_row.values()).casefold()
            child_matches = [
                child for child in children
                if needle in " ".join(str(value) for value in child.values()).casefold()
            ]
            if not needle or needle in searchable or child_matches:
                rows.append(node_row)
                rows.extend(children if not needle or needle in searchable else child_matches)
        return rows

    def detail(self, identifier: str) -> dict[str, Any]:
        if identifier == self.initiative["initiative_id"]:
            return copy.deepcopy(self.initiative)
        records = [
            (node, "node_id") for node in self.nodes + self.superseded_nodes
        ]
        records.extend((attempt, "attempt_id") for attempt in self.attempts)
        for record, field in records:
            if record[field] == identifier:
                return copy.deepcopy(record)
        raise KeyError(identifier)

    def event_tail(self) -> list[dict[str, Any]]:
        return copy.deepcopy(sorted(self.events, key=lambda item: item["sequence"])[-self.event_limit:])

    def superseded_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(sorted(
            ({
                "kind": "node", "depth": 1, "id": node["node_id"],
                "label": node.get("goal", node["node_id"]),
                "state": node.get("state"), "type": node.get("type"),
            } for node in self.superseded_nodes),
            key=lambda row: row["id"],
        ))


__all__ = ["TuiModel"]
