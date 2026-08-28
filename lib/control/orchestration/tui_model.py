"""Pure initiative presentation state for the Control TUI. This module performs no I/O."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from typing import Any, Iterable
from ..tui_style import display_state, rail_tiers


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


# The unified control tree: initiatives plus the unbound-task branch.
# (InitiativesScreen grew the branch in place; the alias names the intent.)

_STATE_ORDER = {
    "needs-input": 0, "awaiting-plan-approval": 1, "running": 2, "paused": 3, "approved": 4,
    "planning": 5, "draft": 6, "ready-for-integration": 7, "integrated": 8,
    "partial": 9, "failed": 10, "cancelled": 11, "archived": 12,
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
    worker: str = "-"
    task_id: str | None = None
    observed_at: str | None = None
    rail: tuple[str, ...] = ()
    display: tuple[str, str] | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.initiative_id, self.id)

    @property
    def needs_human(self) -> bool:
        return self.attention not in ("-", "")


def _attention(view: dict[str, Any]) -> str:
    initiative = view["initiative"]
    state = initiative.get("state")
    if state == "awaiting-plan-approval":
        return "plan approval"
    if state == "needs-input":
        return "needs input"
    if state == "ready-for-integration":
        return "integrate"
    if state == "approved":
        return "activate"
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


_TASKS_ROOT_KEY = ("tasks-root", "unbound", "unbound")
_WORKER_ATTENTION_STATES = {"needs-input"}


def _task_attention(task_row: Any) -> str:
    """The human-actionable state of one Control task row, or '-'.

    Duck-typed over the Tasks-side row (``task``, ``display_state``,
    ``reconciliation``) so this pure module never imports the Tasks TUI.
    """
    state = getattr(task_row, "display_state", "?")
    reconciliation = getattr(task_row, "reconciliation", {}) or {}
    if state in _WORKER_ATTENTION_STATES:
        detail = ""
        for item in reconciliation.get("evidence", []) or []:
            if isinstance(item, dict) and item.get("state") == "needs-input":
                detail = str(item.get("detail", ""))
                break
        return "at prompt" + (f": {detail[:60]}" if detail else "")
    blocker = reconciliation.get("blocker")
    if blocker:
        return str(blocker)[:70]
    return "-"


def _link_maps(views: list[dict[str, Any]]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """(control_task_id -> (initiative_id, attempt_id), attempt_id -> control_task_id)."""
    bound: dict[str, tuple[str, str]] = {}
    by_attempt: dict[str, str] = {}
    for view in views:
        initiative_id = view["initiative"]["initiative_id"]
        for link in view.get("links", []) or []:
            bound[link["control_task_id"]] = (initiative_id, link["attempt_id"])
            by_attempt[link["attempt_id"]] = link["control_task_id"]
    return bound, by_attempt


def _attempt_worker(
    attempt: dict[str, Any] | None,
    by_attempt: dict[str, str],
    task_index: dict[str, Any],
) -> tuple[str, str, str | None, str | None]:
    """(worker text, attention text, task_id, observed_at) for a node/attempt row."""
    if attempt is None:
        return "-", "-", None, None
    task_id = by_attempt.get(attempt["attempt_id"])
    task_row = None if task_id is None else task_index.get(task_id)
    if task_row is None:
        return "-", "-", task_id, None
    state = getattr(task_row, "display_state", "?")
    attention = _task_attention(task_row)
    if attempt.get("state") in {"reported", "awaiting-exit"} and state == "idle":
        attention = "awaiting exit (X closes)"
    observed = getattr(getattr(task_row, "observation", None), "observed_at", None)
    return state, attention, task_id, observed


def _latest_attempt(view: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    attempts = sorted(
        (item for item in view.get("attempts", []) if item["node_id"] == node_id),
        key=lambda item: (item.get("ordinal", 0), item["attempt_id"]),
    )
    return attempts[-1] if attempts else None


def _pending_directives(view: dict[str, Any]) -> list[dict[str, Any]]:
    pending = []
    for action in view.get("actions", []) or []:
        if action.get("action_class") != "directive":
            continue
        try:
            outcome = json.loads(action.get("outcome") or "{}")
        except ValueError:
            continue
        if outcome.get("delivery") == "pending":
            pending.append({"action_id": action["action_id"], "node_id": outcome.get("node_id")})
    return pending


def attention_items(
    views: list[dict[str, Any]], task_rows: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Everything currently waiting on a human, across initiatives and tasks.

    One assembler feeds both the tree's waiting-on-me filter and the
    ``asha initiative attention`` verb, so the two can never disagree.
    """
    items: list[dict[str, Any]] = []
    bound, by_attempt = _link_maps(views)
    task_index = {getattr(row, "task", {}).get("task_id"): row for row in task_rows}
    for view in views:
        initiative = view["initiative"]
        identity = {
            "initiative_id": initiative["initiative_id"],
            "slug": initiative.get("slug", ""),
        }
        state = initiative.get("state")
        if state == "awaiting-plan-approval":
            plan = view.get("plan") or {}
            items.append({
                **identity, "kind": "plan-approval",
                "detail": f"plan revision {plan.get('revision', '?')} awaits approval",
                "resolution": f"asha initiative approve {initiative['initiative_id']} --digest {plan.get('digest', '?')}",
            })
        for approval in view.get("approvals", []) or []:
            if approval.get("state") == "requested":
                items.append({
                    **identity, "kind": "salvage-approval",
                    "detail": f"salvage request {approval.get('request_id', '?')} awaits approval",
                    "resolution": f"asha initiative approve-salvage {initiative['initiative_id']} --request {approval.get('request_id', '?')}",
                })
        for node in view.get("nodes", []) or []:
            if node.get("state") == "needs-input":
                items.append({
                    **identity, "kind": "needs-input", "node_id": node["node_id"],
                    "detail": f"node {node['node_id']} needs a decision",
                    "resolution": "decide or repair, then resume",
                })
            attempt = _latest_attempt(view, node["node_id"])
            worker, attention, task_id, _seen = _attempt_worker(attempt, by_attempt, task_index)
            del worker
            if attention not in ("-", ""):
                items.append({
                    **identity, "kind": "worker", "node_id": node["node_id"],
                    "task_id": task_id, "detail": attention,
                    "resolution": "attach (Enter) or close (X) in asha control",
                })
        for directive in _pending_directives(view):
            items.append({
                **identity, "kind": "directive-pending", "node_id": directive.get("node_id"),
                "detail": f"directive {directive['action_id'][:8]} awaits delivery",
                "resolution": "relay to the attempt's pane or let the next attempt carry it",
            })
    for task_id, row in task_index.items():
        if task_id is None or task_id in bound:
            continue
        attention = _task_attention(row)
        if attention not in ("-", ""):
            items.append({
                "initiative_id": None, "slug": None, "kind": "task",
                "task_id": task_id,
                "detail": f"task {getattr(row, 'summary', {}).get('slug', task_id)}: {attention}",
                "resolution": "attach (Enter) in asha control",
            })
    return items


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
        task_rows: Iterable[Any] = (),
        attention_only: bool = False,
        orchestration_error: str | None = None,
    ) -> None:
        self.views = [copy.deepcopy(view) for view in views]
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        # The unbound-tasks branch starts open: plain Control tasks must stay
        # visible without a keystroke, exactly as the old Tasks view showed them.
        self.expanded: set[tuple[str, str]] = (
            {("tasks-root", "unbound")} if expanded is None else set(expanded)
        )
        self.selection = selection
        self.filter_string = filter_string
        self.task_rows: tuple[Any, ...] = tuple(task_rows)
        self.attention_only = bool(attention_only)
        self.orchestration_error = orchestration_error
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
        bound, by_attempt = _link_maps(self.views)
        task_index = {
            getattr(row, "task", {}).get("task_id"): row for row in self.task_rows
        }
        rows: list[InitiativeRow] = []
        for view in self._sorted_views():
            initiative = view["initiative"]
            initiative_id = initiative["initiative_id"]
            head = InitiativeRow(
                "initiative", 0, initiative_id, initiative_id,
                initiative.get("slug", initiative_id[:8]), initiative.get("state", "?"),
                "initiative", _coordinator_text(view), _nodes_text(view), _attention(view),
                rail=tuple(rail_tiers(view)), display=display_state(view),
            )
            children: list[InitiativeRow] = []
            if ("initiative", initiative_id) in self.expanded:
                for node in sorted(view.get("nodes", []), key=lambda item: item["node_id"]):
                    attempt = _latest_attempt(view, node["node_id"])
                    worker, worker_attention, task_id, observed = _attempt_worker(attempt, by_attempt, task_index)
                    node_row = InitiativeRow(
                        "node", 1, node["node_id"], initiative_id,
                        node.get("goal", node["node_id"]), node.get("state", "?"),
                        node.get("type", "?"),
                        attention=worker_attention if node.get("state") != "needs-input" else "needs input",
                        worker=worker, task_id=task_id, observed_at=observed,
                    )
                    children.append(node_row)
                    if ("node", f"{initiative_id}:{node['node_id']}") in self.expanded:
                        attempts = sorted(
                            (item for item in view.get("attempts", []) if item["node_id"] == node["node_id"]),
                            key=lambda item: (item.get("ordinal", 0), item["attempt_id"]),
                        )
                        for item in attempts:
                            a_worker, a_attention, a_task, a_seen = _attempt_worker(item, by_attempt, task_index)
                            children.append(InitiativeRow(
                                "attempt", 2, item["attempt_id"], initiative_id,
                                f"attempt {item.get('ordinal', '?')}", item.get("state", "?"), "attempt",
                                attention=a_attention, worker=a_worker, task_id=a_task, observed_at=a_seen,
                            ))
            candidates = [head, *children]
            candidates = self._narrow(candidates, head, needle)
            rows.extend(candidates)
        rows.extend(self._task_branch(bound, needle))
        return rows

    def _narrow(
        self, candidates: list[InitiativeRow], head: InitiativeRow, needle: str,
    ) -> list[InitiativeRow]:
        """Apply the text filter and the waiting-on-me filter; keep a matching head."""
        result = candidates
        if needle:
            matching = [
                row for row in result
                if needle in f"{row.label} {row.state} {row.type} {row.id} {row.attention}".casefold()
            ]
            if not matching:
                return []
            if head not in matching:
                matching.insert(0, head)
            result = matching
        if self.attention_only:
            matching = [row for row in result if row.needs_human]
            if not matching:
                return []
            if head not in matching:
                matching.insert(0, head)
            result = matching
        return result

    def _task_branch(
        self, bound: dict[str, tuple[str, str]], needle: str,
    ) -> list[InitiativeRow]:
        """Control tasks bound to no initiative, under one expandable root."""
        unbound = [
            row for row in self.task_rows
            if getattr(row, "task", {}).get("task_id") not in bound
        ]
        if not unbound:
            return []
        # With no initiatives on screen the branch flattens: the tree is then
        # exactly the task list, no header row stealing the first selection.
        flat = not self.views
        head = InitiativeRow(
            "tasks-root", 0, "unbound", "unbound",
            "Unbound tasks", str(len(unbound)), "tasks",
        )
        children: list[InitiativeRow] = []
        if flat or ("tasks-root", "unbound") in self.expanded:
            for row in unbound:
                task = getattr(row, "task", {})
                summary = getattr(row, "summary", {}) or {}
                children.append(InitiativeRow(
                    "task", 1, task.get("task_id", "?"), "unbound",
                    summary.get("slug", task.get("task_id", "?")),
                    getattr(row, "display_state", "?"),
                    (task.get("runs") or [{}])[0].get("harness", "task"),
                    attention=_task_attention(row),
                    worker=getattr(row, "display_state", "?"),
                    task_id=task.get("task_id"),
                    observed_at=getattr(getattr(row, "observation", None), "observed_at", None),
                ))
        if flat:
            children = [replace(child, depth=0) for child in children]
            if not children:
                return []
            return self._narrow(children, children[0], needle)
        return self._narrow([head, *children], head, needle)

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
        if row is None or row.kind in {"attempt", "task"}:
            return False
        if row.kind == "tasks-root":
            key = ("tasks-root", "unbound")
        elif row.kind == "initiative":
            key = ("initiative", row.id)
        else:
            key = ("node", f"{row.initiative_id}:{row.id}")
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
        elif row.kind == "tasks-root":
            key = ("tasks-root", "unbound")
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

    def replace_views(
        self, views: list[dict[str, Any]], task_rows: Iterable[Any] | None = None,
    ) -> None:
        selected = None if self.selected_row is None else self.selected_row.key
        self.views = [copy.deepcopy(view) for view in views]
        if task_rows is not None:
            self.task_rows = tuple(task_rows)
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

# The unified control tree is this screen; the alias names the role.
ControlTree = InitiativesScreen
