"""Pure initiative presentation state for the Control TUI. This module performs no I/O."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


class InitiativeTreeModel:
    """One initiative's tree rows, detail lookup, and bounded event tail."""

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




# Backward-compatible name for the tree projection.
TuiModel = InitiativeTreeModel

_STATE_ORDER = {
    "needs-input": 0, "awaiting-plan-approval": 1, "running": 2, "paused": 3, "approved": 4,
    "planning": 5, "draft": 6, "ready-for-integration": 7, "partial": 8, "failed": 9,
    "cancelled": 10, "archived": 11,
}
_INITIATIVE_FIXED_LINES = 16


@dataclass(frozen=True)
class InitiativeRow:
    kind: str
    depth: int
    id: str
    initiative_id: str
    label: str
    state: str
    type: str
    coordinator: str = "-"
    nodes: str = "-"
    attention: str = "-"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.initiative_id, self.id)


def _attention(view: dict[str, Any]) -> str:
    initiative = view["initiative"]
    state = initiative.get("state")
    if state == "awaiting-plan-approval":
        return "plan approval"
    if state == "needs-input":
        return "needs input"
    if any(item.get("state") == "requested" for item in view.get("approvals", [])):
        return "salvage approval"
    if state == "paused":
        return "paused"
    failed = sum(1 for node in view.get("nodes", []) if node.get("state") == "failed")
    if failed:
        return f"{failed} failed"
    return "-"


def _coordinator_text(view: dict[str, Any]) -> str:
    record = view.get("coordinator")
    if not record:
        return "-"
    state = record.get("state")
    if state in {"active", "waiting", "needs-input", "stopping", "starting"}:
        return f"{record.get('harness', '?')} g{record.get('generation', '?')}"
    return f"{state} g{record.get('generation', '?')}"


def _nodes_text(view: dict[str, Any]) -> str:
    nodes = view.get("nodes", [])
    done = sum(1 for node in nodes if node.get("state") == "succeeded")
    return f"{done}/{len(nodes)}"


class InitiativesScreen:
    """Selection, expansion, filter, and fact projection over several initiative views.

    A *view* is the loader's per-initiative bundle: ``initiative``, ``nodes``,
    ``attempts``, ``events``, ``coordinator`` (record or None), ``seals``,
    ``reviews``, ``verifications``, ``approvals``, ``links``, ``storage``
    (report or None), and ``plan`` (latest plan or None).
    """

    def __init__(
        self,
        views: list[dict[str, Any]],
        *,
        height: int = 24,
        width: int = 100,
        expanded: set[tuple[str, str]] | None = None,
        selection: int | None = 0,
        filter_string: str = "",
    ) -> None:
        self.views = [copy.deepcopy(view) for view in views]
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        self.expanded: set[tuple[str, str]] = set(expanded or ())
        self.selection = selection
        self.filter_string = filter_string
        self.scroll_offset = 0
        self.help_visible = False
        self.message: str | None = None
        self.pane: str = "summary"
        self._clamp_selection()

    # -- rows -------------------------------------------------------------

    def _sorted_views(self) -> list[dict[str, Any]]:
        return sorted(
            self.views,
            key=lambda view: (
                _STATE_ORDER.get(view["initiative"].get("state"), 99),
                view["initiative"].get("slug", ""),
            ),
        )

    def rows(self) -> list[InitiativeRow]:
        needle = self.filter_string.casefold()
        rows: list[InitiativeRow] = []
        for view in self._sorted_views():
            initiative = view["initiative"]
            initiative_id = initiative["initiative_id"]
            head = InitiativeRow(
                "initiative", 0, initiative_id, initiative_id,
                initiative.get("slug", initiative_id[:8]), initiative.get("state", "?"),
                "initiative", _coordinator_text(view), _nodes_text(view), _attention(view),
            )
            children: list[InitiativeRow] = []
            if ("initiative", initiative_id) in self.expanded:
                for node in sorted(view.get("nodes", []), key=lambda item: item["node_id"]):
                    node_row = InitiativeRow(
                        "node", 1, node["node_id"], initiative_id,
                        node.get("goal", node["node_id"]), node.get("state", "?"),
                        node.get("type", "?"),
                    )
                    children.append(node_row)
                    if ("node", f"{initiative_id}:{node['node_id']}") in self.expanded:
                        attempts = sorted(
                            (item for item in view.get("attempts", []) if item["node_id"] == node["node_id"]),
                            key=lambda item: (item.get("ordinal", 0), item["attempt_id"]),
                        )
                        children.extend(
                            InitiativeRow(
                                "attempt", 2, item["attempt_id"], initiative_id,
                                f"attempt {item.get('ordinal', '?')}", item.get("state", "?"), "attempt",
                            )
                            for item in attempts
                        )
            candidates = [head, *children]
            if needle:
                matching = [
                    row for row in candidates
                    if needle in f"{row.label} {row.state} {row.type} {row.id} {row.attention}".casefold()
                ]
                if not matching:
                    continue
                if head not in matching:
                    matching.insert(0, head)
                candidates = matching
            rows.extend(candidates)
        return rows

    @property
    def visible_capacity(self) -> int:
        return max(0, self.height - _INITIATIVE_FIXED_LINES)

    @property
    def visible_rows(self) -> list[InitiativeRow]:
        rows = self.rows()
        if self.visible_capacity == 0:
            return []
        return rows[self.scroll_offset:self.scroll_offset + self.visible_capacity]

    @property
    def selected_row(self) -> InitiativeRow | None:
        rows = self.rows()
        if self.selection is None or not rows:
            return None
        return rows[min(self.selection, len(rows) - 1)]

    def view_for(self, initiative_id: str) -> dict[str, Any] | None:
        return next((view for view in self.views if view["initiative"]["initiative_id"] == initiative_id), None)

    @property
    def selected_view(self) -> dict[str, Any] | None:
        row = self.selected_row
        return None if row is None else self.view_for(row.initiative_id)

    # -- mutation ---------------------------------------------------------

    def _clamp_selection(self) -> None:
        count = len(self.rows())
        if count == 0:
            self.selection = None
            self.scroll_offset = 0
            return
        if self.selection is None:
            self.selection = 0
        self.selection = min(max(int(self.selection), 0), count - 1)
        self._ensure_visible()

    def _ensure_visible(self) -> None:
        if self.selection is None:
            self.scroll_offset = 0
            return
        capacity = self.visible_capacity
        if capacity == 0:
            self.scroll_offset = self.selection
        elif self.selection < self.scroll_offset:
            self.scroll_offset = self.selection
        elif self.selection >= self.scroll_offset + capacity:
            self.scroll_offset = self.selection - capacity + 1
        maximum = max(0, len(self.rows()) - max(1, capacity))
        self.scroll_offset = min(max(0, self.scroll_offset), maximum)

    def move_selection(self, delta: int) -> InitiativeRow | None:
        rows = self.rows()
        if not rows:
            self.selection = None
            return None
        current = 0 if self.selection is None else self.selection
        self.selection = min(max(current + int(delta), 0), len(rows) - 1)
        self._ensure_visible()
        return self.selected_row

    def expand(self) -> bool:
        row = self.selected_row
        if row is None or row.kind == "attempt":
            return False
        key = ("initiative", row.id) if row.kind == "initiative" else ("node", f"{row.initiative_id}:{row.id}")
        if key in self.expanded:
            return False
        self.expanded.add(key)
        self._clamp_selection()
        return True

    def collapse(self) -> bool:
        row = self.selected_row
        if row is None:
            return False
        if row.kind == "initiative":
            key = ("initiative", row.id)
        elif row.kind == "node":
            key = ("node", f"{row.initiative_id}:{row.id}")
        else:
            key = None
        if key is not None and key in self.expanded:
            self.expanded.discard(key)
            self._clamp_selection()
            return True
        # Collapse of an unexpanded child returns to its parent row.
        rows = self.rows()
        if row.kind != "initiative" and self.selection is not None:
            for index in range(self.selection - 1, -1, -1):
                if rows[index].depth < row.depth:
                    self.selection = index
                    self._ensure_visible()
                    return True
        return False

    def set_filter(self, value: str) -> None:
        self.filter_string = value
        self.selection = 0
        self.scroll_offset = 0
        self._clamp_selection()

    def resize(self, height: int, width: int) -> None:
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        self._ensure_visible()

    def replace_views(self, views: list[dict[str, Any]]) -> None:
        selected = None if self.selected_row is None else self.selected_row.key
        self.views = [copy.deepcopy(view) for view in views]
        rows = self.rows()
        self.selection = next(
            (index for index, row in enumerate(rows) if row.key == selected),
            0 if rows else None,
        )
        self._clamp_selection()

    # -- facts --------------------------------------------------------------

    def detail_lines(self) -> list[str]:
        view = self.selected_view
        if view is None:
            return ["No initiative is selected."]
        initiative = view["initiative"]
        nodes = view.get("nodes", [])
        seals = view.get("seals", [])
        reviews = view.get("reviews", [])
        verifications = view.get("verifications", [])
        coordinator = view.get("coordinator")
        storage = view.get("storage") or {}
        plan = view.get("plan")
        lines = [f"{initiative.get('slug', '?')}  [{initiative.get('state', '?')}]  {initiative.get('label', '')}"]
        if coordinator:
            live = view.get("coordinator_live")
            liveness = "live" if live is True else ("unknown" if live is None else "gone")
            lines.append(
                f"Coordinator: {coordinator.get('harness', '?')} generation {coordinator.get('generation', '?')} "
                f"{coordinator.get('state', '?')} (anchor {liveness}, pane {coordinator.get('anchor', {}).get('pane_id', '?')})"
            )
        else:
            lines.append("Coordinator: -")
        if initiative.get("state") == "awaiting-plan-approval" and plan is not None:
            lines.append(f"Approval:   plan revision {plan.get('revision')} digest {str(plan.get('digest'))[:16]}… awaiting operator decision (a)")
        terminal = [seal for seal in seals if seal.get("outcome") in {"success", "failure", "paused"}]
        if terminal:
            latest = sorted(terminal, key=lambda item: (item.get("sealed_at", ""), item["seal_id"]))[-1]
            lines.append(f"Candidate:  seal {latest['seal_id'][:8]} {latest.get('outcome')} node {latest.get('node_id')}")
        else:
            lines.append("Candidate:  no terminal seal")
        if reviews:
            latest_review = sorted(reviews, key=lambda item: (item.get("updated_at", ""), item["review_id"]))[-1]
            lines.append(f"Review:     {latest_review.get('state')} verdict {latest_review.get('verdict') or 'pending'}")
        else:
            lines.append("Review:     pending")
        if verifications:
            latest_verification = sorted(verifications, key=lambda item: (item.get("updated_at", ""), item["verification_id"]))[-1]
            lines.append(f"Verify:     {latest_verification.get('state')} outcome {latest_verification.get('outcome') or 'pending'}")
        else:
            lines.append("Verify:     pending")
        limits = initiative.get("limits", {})
        running = sum(1 for item in view.get("attempts", []) if item.get("state") in {"dispatching", "running", "reported", "awaiting-exit"})
        lines.append(
            f"Limits:     parallel {running}/{limits.get('max_parallel', '?')} | "
            f"nodes {_nodes_text(view)} | tasks {len(view.get('links', []))}/{limits.get('max_total_tasks', '?')}"
        )
        totals = storage.get("totals") or {}
        thresholds = storage.get("thresholds") or {}
        if totals:
            lines.append(
                f"Storage:    retained {totals.get('bytes', 0)} B / pause at {thresholds.get('max_retained_bytes_before_pause', '?')} B"
                + (" (pause recommended)" if storage.get("pause_recommended") else "")
            )
        else:
            lines.append("Storage:    not sampled")
        failed = [node["node_id"] for node in nodes if node.get("state") == "failed"]
        if failed:
            lines.append("Failed:     " + ", ".join(sorted(failed)[:6]))
        for event in sorted(view.get("events", []), key=lambda item: item["sequence"])[-3:]:
            lines.append(f"Event:      #{event['sequence']} {event['type']} ({event.get('actor_kind', '?')})")
        return lines

    def pane_lines(self) -> list[str]:
        """Secondary pane requested by a key: events, candidates, verification, storage."""
        view = self.selected_view
        if view is None:
            return []
        if self.pane == "events":
            return [
                f"#{event['sequence']} {event['type']} {event.get('actor_kind', '?')} {event.get('recorded_at', '')}"
                for event in sorted(view.get("events", []), key=lambda item: item["sequence"])[-12:]
            ] or ["no events"]
        if self.pane == "candidates":
            return [
                f"seal {seal['seal_id'][:8]} {seal.get('outcome')} node {seal.get('node_id')} attempt {str(seal.get('attempt_id'))[:8]}"
                for seal in view.get("seals", [])
            ] or ["no seals"]
        if self.pane == "verification":
            lines = [
                f"review {item['review_id'][:8]} {item.get('state')} verdict {item.get('verdict') or 'pending'} seal {str(item.get('seal_id'))[:8]}"
                for item in view.get("reviews", [])
            ] + [
                f"verification {item['verification_id'][:8]} {item.get('state')} outcome {item.get('outcome') or 'pending'} seal {str(item.get('seal_id'))[:8]}"
                for item in view.get("verifications", [])
            ]
            return lines or ["no review or verification evidence"]
        if self.pane == "storage":
            storage = view.get("storage") or {}
            if not storage:
                return ["storage not sampled"]
            totals = storage.get("totals") or {}
            thresholds = storage.get("thresholds") or {}
            lines = [
                f"retained bytes {totals.get('bytes', 0)} / pause {thresholds.get('max_retained_bytes_before_pause', '?')}",
                f"retained inodes {totals.get('inodes', 0)} / pause {thresholds.get('max_retained_inodes_before_pause', '?')}",
            ]
            lines.extend(
                f"workspace {item.get('path', '?')} {item.get('bytes', 0)} B"
                for item in storage.get("workspaces", [])[:6]
            )
            return lines
        return []


__all__ = ["InitiativeRow", "InitiativeTreeModel", "InitiativesScreen", "TuiModel"]
